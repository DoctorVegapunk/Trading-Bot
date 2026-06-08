# main.py
import os
import logging
from datetime import datetime, timezone
from flask import Flask, jsonify

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

app = Flask(__name__)


def run_once():
    from broker.factory import get_broker, init_broker, load_dotenv
    import live_trader as lt

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    # Skip weekends — forex market is closed (defense in depth)
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        log.info("Weekend — forex closed, skipping run")
        return

    instruments    = lt.INSTRUMENT_MAP
    broker_symbols = list(instruments.values())
    broker         = init_broker(broker_symbols, lt.FIX_CACHE_DIR)

    if not broker.connect():
        raise RuntimeError("Could not connect to broker (FIX). Check FIX_PASSWORD env var.")

    log.info(f"Broker connected: {broker.name}")

    models = {}
    for key in instruments:
        payload = lt.load_model(key)
        if payload:
            models[key] = payload

    if not models:
        broker.shutdown()
        raise RuntimeError("No models loaded — make sure models/ folder is present.")

    # Fresh state each run — last_candle_time=None means it always processes the latest candle
    states = {key: lt.InstrumentState(key, instruments[key]) for key in models}

    results = []
    for key, state in states.items():
        try:
            log.info(f"Running signal check for {key}...")
            lt.run_instrument_loop(state, models[key], dry_run=False)
            results.append({"instrument": key, "status": "ok"})
        except Exception as exc:
            log.error(f"Error on {key}: {exc}", exc_info=True)
            results.append({"instrument": key, "status": "error", "error": str(exc)})

    # Push account summary to Firestore
    if lt._firestore_logger is not None:
        try:
            from integrations.firestore_logger import build_account_summary

            broker.refresh_positions()
            positions = broker.get_bot_positions()
            balance = broker.get_account_balance()
            currency = broker.get_account_currency()
            log.info("Account: %.2f %s (%d open positions)", balance, currency, len(positions))
            summary = build_account_summary(
                broker_name=broker.name,
                broker_backend="fix",
                balance=balance,
                currency=currency,
                open_positions=len(positions),
                instruments=list(instruments.keys()),
                dry_run=False,
            )
            lt._firestore_logger.update_account(summary)
            log.info("Firestore account summary pushed")
        except Exception as exc:
            log.debug("Firestore summary skipped: %s", exc)

    broker.shutdown()
    log.info("Run complete.")
    return results


@app.route("/run", methods=["POST"])
def run():
    try:
        result = run_once()
        return jsonify({"status": "success", "result": result}), 200
    except Exception as exc:
        log.error(f"Fatal error: {exc}", exc_info=True)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)