# RSI Binance bot

Spot bot RSI multi-pair Binance. Audit/safety patch is read-only with respect to the account: no exchange operation is performed by the dashboard.

## Run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env.example .env
# edit .env; keep DRY_RUN=true for initial validation
python bot.py
```

Live trading is high risk. Test in dry-run and Binance testnet first. If an order submission or OCO response is ambiguous, the bot intentionally persists a pending intent and freezes that pair instead of retrying automatically. Check the Binance account and reconcile the state before manually clearing that intent. Never delete a pending order marker just to resume trading.

## Read-only web dashboard (port 8060)

Start separately from the bot:

```bash
python dashboard.py
```

Open `http://127.0.0.1:8060`. The dashboard has no trading controls and reads only `STATE_FILE`. It binds to localhost by default. To access remotely, use an SSH tunnel, for example `ssh -L 8060:127.0.0.1:8060 user@your-vps`. Do not bind it to `0.0.0.0` or expose it publicly without adding authentication and TLS. Set `DASHBOARD_PORT` or `DASHBOARD_HOST` only if you understand the exposure.

## Tests

```bash
pytest -q
```

Tests do not send orders or connect to Binance. A passing test suite does not establish that exchange execution, OCO behavior, or a strategy is profitable. Do not use live funds without testnet validation and manual reconciliation procedures.
