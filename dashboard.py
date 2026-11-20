#!/usr/bin/env python3
"""Read-only RSI Binance dashboard. No order endpoints or trading mutations."""
from __future__ import annotations
import json, logging, os, subprocess, time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
ENV = dotenv_values(ROOT / ".env")
HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8060"))
QUOTE = str(ENV.get("QUOTE_ASSET") or "USDT").upper()
SYMBOLS = [x.strip().upper() for x in str(ENV.get("SYMBOLS") or "").replace(";", ",").split(",") if x.strip()]
DRY_RUN = str(ENV.get("DRY_RUN", "true")).lower() in ("1", "true", "yes", "on")
TESTNET = str(ENV.get("BINANCE_TESTNET", "false")).lower() in ("1", "true", "yes", "on")
STATE_FILE = Path(os.getenv("STATE_FILE", str(ROOT / str(ENV.get("STATE_FILE") or "state.json"))))
LOG_FILE = Path(os.getenv("LOG_FILE", str(ROOT / str(ENV.get("LOG_FILE") or "bot.log"))))
CACHE_TTL = max(3, int(os.getenv("DASHBOARD_CACHE_SECONDS", "10")))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("rsi-dashboard")
lock = Lock()
cache = {"at": 0.0, "data": None}
ex = None
ex_error = None

def num(v, default=0.0):
    try: return float(v) if v is not None and v != "" else default
    except (TypeError, ValueError): return default

def read_state():
    try:
        with STATE_FILE.open(encoding="utf-8") as f: return json.load(f)
    except FileNotFoundError: return {}
    except Exception:
        log.exception("Cannot read state file")
        return {}

def exchange():
    global ex, ex_error
    if ex is not None: return ex
    try:
        import ccxt
        ex = ccxt.binance({"apiKey": "" if DRY_RUN else str(ENV.get("BINANCE_API_KEY") or ""),
            "secret": "" if DRY_RUN else str(ENV.get("BINANCE_API_SECRET") or ""), "enableRateLimit": True,
            "timeout": 12000, "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
        if TESTNET: ex.set_sandbox_mode(True)
        ex.load_markets()
        ex_error = None
        return ex
    except Exception as e:
        ex_error = type(e).__name__ + ": " + str(e)[:160]
        log.warning("Exchange init failed: %s", ex_error)
        ex = None
        return None

def bot_status():
    try:
        r = subprocess.run(["systemctl", "is-active", "rsi-binance.service"],
            capture_output=True, text=True, timeout=2, check=False)
        return r.stdout.strip() or "unknown"
    except Exception: return "unknown"

def read_logs():
    try:
        with LOG_FILE.open(encoding="utf-8", errors="replace") as f:
            logs = list(deque((x.rstrip() for x in f), maxlen=35))
            return list(reversed(logs))
    except FileNotFoundError: return []
    except Exception as e: return ["Log unavailable: " + type(e).__name__]

def realized_fifo(client):
    total = wins = losses = closed = 0
    rows, warnings = [], []
    since = int((time.time() - 90 * 86400) * 1000)
    fee_prices = {}

    def fee_quote(amount, currency, base, quote):
        if not amount or not currency or currency == base:
            return 0.0
        if currency == quote:
            return amount
        pair = currency + "/" + quote
        if pair not in client.markets:
            warnings.append("Fee asset tidak dapat dinilai: " + currency)
            return 0.0
        if pair not in fee_prices:
            fee_prices[pair] = num(client.fetch_ticker(pair).get("last"))
        return amount * fee_prices[pair]

    for symbol in SYMBOLS:
        try:
            fills, params, cursor_id = [], {}, None
            for page_no in range(50):
                page = client.fetch_my_trades(
                    symbol, since=since if cursor_id is None else None,
                    limit=1000, params=params,
                )
                if not page:
                    break
                fills.extend(page)
                if len(page) < 1000:
                    break
                try:
                    next_id = int(page[-1]["id"]) + 1
                except (KeyError, TypeError, ValueError):
                    next_id = None
                if next_id is None or next_id <= (cursor_id or -1):
                    warnings.append(symbol + ": pagination trade tidak dapat dilanjutkan")
                    break
                cursor_id = next_id
                params = {"fromId": cursor_id}
            else:
                warnings.append(symbol + ": histori dibatasi 50 halaman")
            fills = sorted({str(x.get("id")): x for x in fills}.values(),
                           key=lambda x: (x.get("timestamp") or 0, str(x.get("id") or "")))
            lots = deque()
            base, quote = symbol.split("/")
            for t in fills:
                side, qty = str(t.get("side") or "").lower(), num(t.get("amount"))
                price, cost = num(t.get("price")), num(t.get("cost"), qty * num(t.get("price")))
                fee = t.get("fee") or {}
                fc, currency = num(fee.get("cost")), str(fee.get("currency") or "")
                fee_value = fee_quote(fc, currency, base, quote)
                if side == "buy":
                    net_qty = qty - (fc if currency == base else 0)
                    buy_cost = cost + fee_value
                    if net_qty > 0: lots.append([net_qty, buy_cost])
                elif side == "sell":
                    sell_qty = max(0, qty - (fc if currency == base else 0))
                    proceeds = cost - fee_value
                    remain = sell_qty
                    matched_qty = matched_cost = 0.0
                    while remain > 1e-12 and lots:
                        lq, lc = lots[0]
                        take = min(remain, lq)
                        unit = lc / lq if lq else 0
                        matched_qty += take
                        matched_cost += take * unit
                        remain -= take
                        lq -= take
                        lc -= take * unit
                        if lq <= 1e-12: lots.popleft()
                        else: lots[0] = [lq, lc]
                    if matched_qty > 0 and sell_qty > 0:
                        pnl = proceeds * matched_qty / sell_qty - matched_cost
                        total += pnl; closed += 1
                        if pnl > 0: wins += 1
                        elif pnl < 0: losses += 1
                        rows.append({"symbol": symbol, "time": t.get("datetime") or "",
                            "qty": matched_qty, "price": price, "pnl": pnl})
        except Exception as e: warnings.append(symbol + ": " + type(e).__name__)
    rows.sort(key=lambda x: x["time"], reverse=True)
    return {"pnl": total, "closed_trades": closed, "wins": wins, "losses": losses,
        "win_rate": wins / closed * 100 if closed else None, "rows": rows[:30],
        "period_days": 90, "warnings": warnings,
        "method": "FIFO trade history 90 hari (termasuk transaksi manual); fee base/quote/asset ketiga dikonversi bila tersedia."}

def build_snapshot():
    state = read_state()
    states = state.get("symbols") or {}
    client = exchange()
    warnings, prices = [], {}
    def price(symbol):
        if symbol not in prices:
            try: prices[symbol] = num(client.fetch_ticker(symbol).get("last")) if client else 0
            except Exception as e:
                warnings.append("Harga " + symbol + ": " + type(e).__name__)
                prices[symbol] = 0
        return prices[symbol]

    positions, position_value, unrealized = [], 0.0, 0.0
    for symbol, item in states.items():
        pos = item.get("position")
        position_status = str(item.get("status", "idle"))
        if not pos or position_status not in ("in_position", "dust", "reconciliation_required"):
            continue
        qty, entry = num(pos.get("qty")), num(pos.get("entry"))
        cost, mark = num(pos.get("cost"), qty * entry), price(symbol)
        value, pnl = qty * mark, qty * mark - cost
        position_value += value; unrealized += pnl
        opened = pos.get("opened_at")
        oco_active, oco_known = False, True
        oco = pos.get("oco")
        if oco and client:
            try:
                active_ids = {str(x.get("id")) for x in client.fetch_open_orders(symbol)}
                oco_active = {str(oco.get("tp_id")), str(oco.get("sl_id"))}.issubset(active_ids)
            except Exception as exc:
                oco_known = False
                warnings.append("Status OCO " + symbol + ": " + type(exc).__name__)
        positions.append({"symbol": symbol, "status": position_status,
            "qty": qty, "entry": entry, "price": mark, "value": value, "cost": cost,
            "pnl": pnl, "pnl_pct": pnl / cost * 100 if cost else 0,
            "tp": num(pos.get("tp")), "sl": num(pos.get("sl")),
            "oco_active": oco_active, "oco_known": oco_known,
            "opened_at": datetime.fromtimestamp(opened, timezone.utc).astimezone().isoformat(timespec="seconds") if opened else ""})

    account = {"free_quote": 0.0, "used_quote": 0.0, "equity": 0.0, "assets": []}
    realized = {"pnl": 0, "closed_trades": 0, "wins": 0, "losses": 0, "win_rate": None,
        "rows": [], "warnings": [], "method": "Belum tersedia"}
    if DRY_RUN:
        free = num(state.get("paper_quote"), num(ENV.get("DRY_RUN_BALANCE"), 1000))
        account.update(free_quote=free, equity=free + position_value, used_quote=position_value)
        realized["method"] = "DRY_RUN tidak menyimpan ledger realized PnL."
    elif client:
        try:
            bal = client.fetch_balance()
            free_map, used_map, total_map = bal.get("free") or {}, bal.get("used") or {}, bal.get("total") or {}
            equity, assets = 0.0, []
            stable = {"USDT", "USDC", "FDUSD", "BUSD", "TUSD", "DAI"}
            for asset, raw in total_map.items():
                amount = num(raw)
                if amount <= 0: continue
                pair = asset + "/" + QUOTE
                value = amount if asset == QUOTE or asset in stable else (amount * price(pair) if pair in client.markets else 0)
                equity += value
                if value > .01:
                    assets.append({"asset": asset, "total": amount, "free": num(free_map.get(asset)),
                        "used": num(used_map.get(asset)), "value": value})
            assets.sort(key=lambda x: x["value"], reverse=True)
            account.update(free_quote=num(free_map.get(QUOTE)), used_quote=num(used_map.get(QUOTE)),
                equity=equity, assets=assets[:20])
            realized = realized_fifo(client)
        except Exception as e: warnings.append("Saldo akun: " + type(e).__name__ + ": " + str(e)[:100])
    elif ex_error: warnings.append("Binance API: " + ex_error)

    return {"updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "bot_status": bot_status(), "mode": "DRY RUN" if DRY_RUN else ("TESTNET" if TESTNET else "LIVE"),
        "account": account, "positions": positions, "position_count": len(positions),
        "unrealized_pnl": unrealized, "realized": realized,
        "symbols": [{"symbol": s, "status": v.get("status", "idle")} for s, v in states.items()],
        "logs": read_logs(), "warnings": warnings,
        "data_note": "Equity mencakup aset akun yang dapat dinilai terhadap " + QUOTE +
          ". Unrealized PnL hanya untuk posisi bot; belum mengurangi estimasi fee keluar."}

def snapshot():
    with lock:
        if cache["data"] is None or time.time() - cache["at"] >= CACHE_TTL:
            cache["data"] = build_snapshot()
            cache["at"] = time.time()
        return cache["data"]

HTML = r"""<!doctype html><html lang="id"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSI Binance | Command Center</title><style>
:root{color-scheme:dark;--bg:#090e1b;--p:#121c30;--line:#273650;--muted:#91a2bd;--fg:#edf3ff;--g:#37d6a0;--r:#ff7185;--a:#f7c66a}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 15% -15%,#20365c,transparent 43%),var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif}main{max-width:1450px;margin:auto;padding:26px 20px 45px}.top,.right,.brand,.section{display:flex;align-items:center;justify-content:space-between;gap:14px}.brand{justify-content:flex-start}.logo{width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,#5c8dff,#9069f4);display:grid;place-items:center;font-weight:900;font-size:20px}.eyebrow{color:var(--muted);font-size:10px;letter-spacing:.15em;text-transform:uppercase}.title{font-size:22px;font-weight:800}.right{color:var(--muted);font-size:12px}.pill,.tag{border:1px solid var(--line);border-radius:99px;padding:5px 10px;background:#101a2c}.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--g);margin-right:6px}.bad{background:var(--r)}button{background:#1b2b49;border:1px solid #354b70;color:var(--fg);padding:8px 12px;border-radius:9px;cursor:pointer}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px;margin-top:24px}.card,.panel{background:linear-gradient(145deg,#141f35,#0f1729);border:1px solid var(--line);border-radius:15px;overflow:hidden}.card{padding:17px;min-width:0}.label{color:var(--muted);font-size:12px}.metric{font-size:26px;font-weight:800;letter-spacing:-.04em;margin-top:8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.sub,.section span{color:var(--muted);font-size:11px}.g{color:var(--g)}.r{color:var(--r)}.a{color:var(--a)}.section{margin:24px 2px 10px}.section h2{font-size:15px;margin:0}.wrap{overflow:auto}table{width:100%;border-collapse:collapse;white-space:nowrap}th,td{text-align:right;padding:12px 13px;border-bottom:1px solid #202d43;font-size:12px}th{color:var(--muted);font-size:10px;letter-spacing:.1em;text-transform:uppercase;background:#10182a}td:first-child,th:first-child{text-align:left;padding-left:16px}tr:last-child td{border:0}.pair{font-weight:750}.empty{padding:25px;text-align:center;color:var(--muted)}.split{display:grid;grid-template-columns:1.1fr .9fr;gap:14px}.list{padding:5px 16px}.item{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #202d43;font-size:12px}.item:last-child{border:0}.muted{color:var(--muted)}.logs{padding:12px 15px;max-height:290px;overflow:auto;font:11px/1.7 ui-monospace,monospace;color:#b4c0d4;overflow-wrap:anywhere}.log{border-bottom:1px solid #202d43;padding:3px 0}.warn{color:var(--a);padding:9px 14px;border-bottom:1px solid var(--line);font-size:12px}.foot{font-size:11px;color:var(--muted);margin-top:16px}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.split{grid-template-columns:1fr}}@media(max-width:560px){main{padding:17px 11px}.top{align-items:flex-start;flex-direction:column}.right{width:100%;justify-content:space-between}.metric{font-size:21px}.card{padding:13px}}
</style></head><body><main><header class="top"><div class="brand"><div class="logo">R</div><div><div class="eyebrow">BINANCE SPOT · TRADING MONITOR</div><div class="title">RSI Command Center</div></div></div><div class="right"><span id="mode" class="pill">Connecting</span><span><i id="dot" class="dot"></i><b id="bot">Checking bot</b></span><button onclick="loadData()">↻ Refresh</button></div></header>
<div id="warnings"></div><section class="grid">
<div class="card"><div class="label">Total Equity</div><div class="metric" id="equity">—</div><div class="sub" id="asset-note">Account valuation</div></div>
<div class="card"><div class="label">Free USDT</div><div class="metric" id="free">—</div><div class="sub" id="used">Used / locked: —</div></div>
<div class="card"><div class="label">Unrealized PnL</div><div class="metric" id="unrealized">—</div><div class="sub" id="open-count">0 positions</div></div>
<div class="card"><div class="label">Realized PnL · 90d</div><div class="metric" id="realized">—</div><div class="sub" id="winrate">Trade history</div></div></section>
<div class="section"><h2>Open positions</h2><span>Mark price · bot state + Binance</span></div><div class="panel wrap"><table><thead><tr><th>Pair</th><th>Qty</th><th>Entry</th><th>Mark</th><th>Value</th><th>Unrealized</th><th>TP</th><th>SL</th><th>State</th><th>OCO</th></tr></thead><tbody id="positions"></tbody></table></div>
<div class="split"><div><div class="section"><h2>Realized trade history</h2><span>FIFO · 90 days</span></div><div class="panel wrap"><table><thead><tr><th>Pair</th><th>Time</th><th>Qty matched</th><th>Exit</th><th>Realized PnL</th></tr></thead><tbody id="trades"></tbody></table></div></div>
<div><div class="section"><h2>Account assets</h2><span>Top assets by value</span></div><div class="panel"><div id="assets" class="list"></div></div><div class="section"><h2>Pair status</h2><span>Strategy state</span></div><div class="panel"><div id="symbols" class="list"></div></div></div></div>
<div class="section"><h2>Bot activity</h2><span>Newest first · latest 35 entries</span><span id="updated">Waiting for data</span></div><div class="panel"><div id="logs" class="logs">Waiting for bot.log…</div></div>
<div class="foot" id="note">Read-only dashboard. Tidak menyediakan fungsi membuat, mengubah, atau membatalkan order.</div></main>
<script>
const el=id=>document.getElementById(id),fmt=(v,d=2)=>Number(v||0).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d}),money=v=>fmt(v)+' USDT',signed=v=>(v>=0?'+':'')+money(v),tone=v=>v>=0?'g':'r';
function safe(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function loadData(){try{const res=await fetch('/api/overview',{cache:'no-store'});if(!res.ok)throw Error('HTTP '+res.status);const d=await res.json(),a=d.account,z=d.realized;
el('mode').textContent=d.mode;el('bot').textContent='Bot '+d.bot_status;el('dot').className='dot'+(d.bot_status==='active'?'':' bad');
el('equity').textContent=money(a.equity);el('free').textContent=money(a.free_quote);el('used').textContent='Used / locked: '+money(a.used_quote);el('asset-note').textContent=d.data_note;
el('unrealized').textContent=signed(d.unrealized_pnl);el('unrealized').className='metric '+tone(d.unrealized_pnl);el('open-count').textContent=d.position_count+' open positions';
el('realized').textContent=signed(z.pnl);el('realized').className='metric '+tone(z.pnl);el('winrate').textContent=z.closed_trades+' matched exits · '+(z.win_rate==null?'Win rate n/a':fmt(z.win_rate,1)+'% win rate');
el('positions').innerHTML=d.positions.length?d.positions.map(p=>'<tr><td class="pair">'+safe(p.symbol)+'</td><td>'+fmt(p.qty,6)+'</td><td>'+fmt(p.entry,6)+'</td><td>'+fmt(p.price,6)+'</td><td>'+money(p.value)+'</td><td class="'+tone(p.pnl)+'">'+signed(p.pnl)+'<br><span class="muted">'+fmt(p.pnl_pct)+'%</span></td><td>'+fmt(p.tp,6)+'</td><td>'+fmt(p.sl,6)+'</td><td><span class="tag">'+safe(p.status.toUpperCase())+'</span></td><td><span class="tag">'+(p.oco_active?'ACTIVE':(p.oco_known?'NOT ACTIVE':'UNKNOWN'))+'</span></td></tr>').join(''):'<tr><td colspan="10" class="empty">No open positions in bot state.</td></tr>';
el('trades').innerHTML=z.rows.length?z.rows.map(t=>'<tr><td class="pair">'+safe(t.symbol)+'</td><td>'+safe(t.time?new Date(t.time).toLocaleString():'—')+'</td><td>'+fmt(t.qty,6)+'</td><td>'+fmt(t.price,6)+'</td><td class="'+tone(t.pnl)+'">'+signed(t.pnl)+'</td></tr>').join(''):'<tr><td colspan="5" class="empty">No matched realized trades found.</td></tr>';
el('assets').innerHTML=(a.assets||[]).map(x=>'<div class="item"><span><b>'+safe(x.asset)+'</b><br><small class="muted">'+fmt(x.total,6)+' total · '+fmt(x.free,6)+' free</small></span><b>'+money(x.value)+'</b></div>').join('')||'<div class="empty">Asset breakdown unavailable.</div>';
el('symbols').innerHTML=d.symbols.map(x=>'<div class="item"><b>'+safe(x.symbol)+'</b><span class="tag">'+safe(x.status.toUpperCase())+'</span></div>').join('')||'<div class="empty">No configured pairs</div>';
el('logs').innerHTML=d.logs.map(x=>'<div class="log">'+safe(x)+'</div>').join('')||'No logs yet';el('updated').textContent='Updated '+new Date(d.updated_at).toLocaleString();
el('warnings').innerHTML=[...d.warnings,...z.warnings].map(x=>'<div class="warn">⚠ '+safe(x)+'</div>').join('');el('note').textContent=d.data_note+' · '+z.method+' · Auto-refresh 10s.';
}catch(e){el('warnings').innerHTML='<div class="warn">Dashboard API error: '+safe(e.message)+'</div>'}}
loadData();setInterval(loadData,10000);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if path == "/api/overview":
            try: body = json.dumps(snapshot(), ensure_ascii=False, separators=(",", ":")).encode(); status = 200
            except Exception as e:
                log.exception("Snapshot failed"); body = json.dumps({"error": type(e).__name__}).encode(); status = 503
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def log_message(self, fmt, *args): log.info("%s - %s", self.address_string(), fmt % args)

if __name__ == "__main__":
    log.info("Read-only dashboard listening on %s:%s", HOST, PORT)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
