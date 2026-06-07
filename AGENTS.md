# Forex XGBoost Trading Bot — AGENTS.md

## Entrypoints

- `python main.py` — Flask server on port `$PORT` (default 8080). Routes: `POST /run` (one signal pass), `GET /health`.
- `python live_trader.py` — Continuous polling loop. Flags: `--dry-run`, `--instrument EUR-USD`.

## Project structure

| Path | Role |
|---|---|
| `main.py` | Flask entrypoint (deployed on Cloud Run) |
| `live_trader.py` | CLI trading loop, feature engineering, position sizing |
| `broker/` | Abstract `Broker` base + `CTraderFixBroker` (FIX 4.4, active) + `MT5Broker` (legacy) |
| `broker/factory.py` | Singleton broker via `init_broker()` / `get_broker()`. Also contains custom `load_dotenv()` (no `python-dotenv`) |
| `broker/history.py` | Yahoo Finance candle fetcher (primary data source) |
| `broker/bar_store.py` | FIX tick stream → 1H OHLC bars, cached as pickle in `data/fix_cache/` |
| `models/{INSTRUMENT}/live_models.pkl` | Active pickled XGBoost model (hash-prefixed files are archived runs) |
| `integrations/` | Optional Firebase Firestore logging (service account JSON in `secrets/`) |
| `gcloud-trading-bot-info.md` | Cloud Run deployment reference (gcloud commands, URLs, secrets) |

## Data flow

1. Fetch 600 1H candles from Yahoo Finance (3 retries, 60s delay) — FIX broker fallback if exhausted
2. Build features in `add_features()` (same code as training/backtest)
3. Load pickled XGBoost model from `models/{key}/live_models.pkl`
4. Get buy/sell probabilities, pass through 5-layer regime gate (ADX, ATR, EMA, ranging, duration)
5. If signal fires: compute lot size (0.5% risk), place FIX market order with SL/TP

## cTrader Open API (live balance + deal history)

cTrader FIX cannot provide account balance (Collateral Inquiry) or trade history
(Trade Capture Report). Open API fills both gaps.

- `broker/open_api_client.py` — synchronous wrapper around the async Twisted-based SDK
- Connect/di connect happens in `broker/factory.py:_try_init_open_api()` at broker init time
- Requires 4 env vars: `OPEN_API_CLIENT_ID`, `OPEN_API_CLIENT_SECRET`,
  `OPEN_API_ACCESS_TOKEN`, `OPEN_API_CTID_TRADER_ACCOUNT_ID`
- If vars are missing (e.g. old .env), the bot falls back to `FIX_BALANCE_USD` silently
- Uses `demo.ctraderapi.com` / `live.ctraderapi.com` on port 5035 (TLS)
- SDK uses Twisted SelectReactor running in a daemon thread; synchronous bridge
  via `reactor.callFromThread` + `threading.Event`
- Balance is cached in `CTraderFixBroker._balance_usd` and refreshed on each
  `get_account_balance()` call
- Deal history is pulled from Open API after each successful trade execution in
  `live_trader.py`; appears as a `"Deal confirmed"` log line

### OAuth 2.0 token lifecycle

- `broker/open_api_auth.py` handles the full OAuth 2.0 authorization code flow
- Initial setup: `python -m broker.open_api_auth` — prints auth URL, user pastes
  the redirect URL back, tokens are saved to `secrets/open_api_token.json`
- On every bot start, `OpenApiAuth.get_valid_token()` checks in-memory cache → disk
  → refresh (via refresh token, which never expires)
- Access tokens are refreshed proactively when < 24h from expiry
- Tokens are NOT stored in `.env` — runtime state lives in `secrets/open_api_token.json`
- The `OPEN_API_REFRESH_TOKEN` env var is optional and only used as fallback if the
  token file is missing (e.g. first deploy on a new machine)

## Key gotchas

- `.gitignore` excludes `*.json` except `models/**/*.json`. Do not add JSON files outside models/.
- `data/fix_cache/*.pkl` is gitignored but rebuilt at runtime. Delete to force fresh Yahoo download.
- Model files use hash prefixes. `live_models.pkl` is the active model — create/update this after training.
- `.env` is loaded via custom `load_dotenv()` in `broker/factory.py` — not `python-dotenv`.
- No linter, formatter, typechecker, or test framework present.
- FIX uses dual TCP sessions: quote port 5211 (market data), trade port 5212 (orders), both TLS.
- Account balance for position sizing comes from Open API when available, otherwise `FIX_BALANCE_USD` (or `FIX_BALANCE` + `FIX_CURRENCY`).
