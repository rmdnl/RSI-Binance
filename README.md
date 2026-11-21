# RSI Binance bot

Spot bot RSI multi-pair Binance. The dashboard reads a portfolio snapshot written by the bot; it never submits orders.

## Run

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp env.example .env
# edit .env; keep DRY_RUN=true for initial validation
python bot.py
```

Live trading is high risk. Test dry-run and Binance testnet first. Ambiguous order outcomes are persisted and freeze the affected pair; reconcile with Binance before resuming. Realized PnL is tracked by this bot from the time tracking begins and excludes manual trades or earlier history. Live equity is an estimate in QUOTE_ASSET; assets without a matching quote market are shown but excluded from the valuation.

## Read-only portfolio dashboard (port 8060)

Run the bot, then separately run `python dashboard.py`. Open `http://127.0.0.1:8060`. Dashboard displays estimated equity, bot positions, wallet coin balances, unrealized PnL, and bot-tracked realized PnL. It refreshes every five seconds from the portfolio snapshot in `state.json`. Its default bind is localhost; use SSH tunneling for remote viewing. Do not expose it publicly without authentication and TLS.

```bash
ssh -L 8060:127.0.0.1:8060 user@your-vps
```

## Tests

```bash
pytest -q
```

Tests do not send orders or connect to Binance. Passing tests do not establish exchange execution safety or profitability. Verify state and figures against Binance/testnet before live use.
