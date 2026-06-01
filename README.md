# Forex XGBoost Trading Bot

Trades EUR-USD and GBP-USD via cTrader FIX using XGBoost models trained with rolling walk-forward cross-validation.

## Quick start

```bash
cp .env.example .env    # set FIX_PASSWORD
docker build -t forex-bot .
docker run -d --restart unless-stopped -v ./.env:/app/.env forex-bot
```

## Deploy

Push to GitHub → connect as Render Blueprint → set `FIX_PASSWORD` in dashboard → deploy.

## Candle fallback

1. Yahoo Finance (retries 3× with 60s delay)
2. FIX broker market data
