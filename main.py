# main.py
import os
import logging
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
    from live_trader import (
        INSTRUMENT_MAP, FIX_CACHE_DIR,
        InstrumentState, load_model, run_instrument_loop
    )

    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    instruments    = INSTRUMENT_MAP
    broker_symbols = list(instruments.values())
    broker         = init_broker(broker_symbols, FIX_CACHE_DIR)

    if not broker.connect():
        raise RuntimeError("Could not connect to broker (FIX). Check FIX_PASSWORD env var.")

    log.info(f"Broker connected: {broker.name}")

    models = {}
    for key in instruments:
        payload = load_model(key)
        if payload:
            models[key] = payload

    if not models:
        broker.shutdown()
        raise RuntimeError("No models loaded — make sure models/ folder is present.")

    # Fresh state each run — last_candle_time=None means it always processes the latest candle
    states = {key: InstrumentState(key, instruments[key]) for key in models}

    results = []
    for key, state in states.items():
        try:
            log.info(f"Running signal check for {key}...")
            run_instrument_loop(state, models[key], dry_run=False)
            results.append({"instrument": key, "status": "ok"})
        except Exception as exc:
            log.error(f"Error on {key}: {exc}", exc_info=True)
            results.append({"instrument": key, "status": "error", "error": str(exc)})

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