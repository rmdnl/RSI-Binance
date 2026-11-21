# RSI Binance bot — audit & operations notes

## Run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env.example .env
# Keep DRY_RUN=true for initial validation
python bot.py
```

## Dashboard

Run `python dashboard.py` separately, then open `http://127.0.0.1:8060`. Dashboard is read-only and shows estimated equity, coin balances, bot positions, unrealized/realized PnL, rate-limit pauses, and recent application events/errors. It reads state only; it does not fetch private account data itself. Default bind is localhost; use SSH forwarding remotely. Do not expose publicly without authentication and TLS.

## API request discipline

CCXT's built-in `enableRateLimit` remains enabled. The bot also caches mark prices briefly, updates portfolio snapshots at most every 30 seconds, and polls each OCO no more frequently than every 30 seconds. Binance 429/418/rate-limit exceptions trigger a global pause using `Retry-After` when CCXT exposes it (otherwise conservative fallback); the bot does not hammer the endpoint or automatically retry ambiguous order submissions. These controls reduce request pressure but cannot guarantee avoiding exchange limits: endpoint weights, account limits, IP sharing, and Binance policies can change. Inspect current official Spot API documentation and response headers; use WebSocket market streams for higher-frequency market data rather than increasing REST polling.

## Audit caveats

- This is not an institutional execution system and no profit is assured. Dry-run/testnet validation is required before any live use.
- Realized PnL is bot-tracked only from the time tracking starts; manual trades, deposits, withdrawals, past trades, and fees in third-party assets are not fully attributed.
- Live equity is a mark-to-market estimate in `QUOTE_ASSET`; holdings with no direct quote market are listed but excluded from the total. Position unrealized PnL only covers positions recorded by this bot.
- Ambiguous order outcomes intentionally freeze a pair. Reconcile open orders, fills, balances, and state with Binance before resuming; do not clear pending intent blindly.
- The strategy/backtest do not model all spread, latency, market impact, exchange filters, API outages, or fee-asset effects.

## Tests

```bash
pytest -q
```

Tests do not send orders or connect to Binance. Passing tests do not establish live API compatibility or profitability.
