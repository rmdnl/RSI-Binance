"""Read-only local dashboard. Run: python dashboard.py (default 127.0.0.1:8060)."""
from __future__ import annotations
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT = int(os.getenv("DASHBOARD_PORT", "8060"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")

PAGE = b'''<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>RSI Bot Status</title><style>
body{font:16px system-ui;background:#10151d;color:#e8eef6;margin:2rem}h1{color:#7dd3fc}#meta{color:#aab8ca}.card{background:#1b2532;padding:1rem;margin:1rem 0;border-radius:10px;overflow:auto}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:.6rem;border-bottom:1px solid #344256} .warn{color:#fbbf24}code{color:#93c5fd}
</style><h1>RSI Binance — status</h1><p id="meta">Memuat state…</p><div id="content"></div><script>
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();document.querySelector('#meta').textContent='Mode: '+d.mode+' | diperbarui: '+new Date(d.read_at).toLocaleString();const rows=d.symbols.map(s=>`<tr><td>${s.symbol}</td><td>${s.status}</td><td>${s.qty??'—'}</td><td>${s.entry??'—'}</td><td>${s.tp??'—'}</td><td>${s.sl??'—'}</td><td class="warn">${s.alert??''}</td></tr>`).join('');document.querySelector('#content').innerHTML=`<div class="card"><table><thead><tr><th>Pair</th><th>Status</th><th>Qty</th><th>Entry</th><th>TP</th><th>SL</th><th>Perhatian</th></tr></thead><tbody>${rows||'<tr><td colspan="7">Belum ada data</td></tr>'}</tbody></table></div>`}catch(e){document.querySelector('#meta').textContent='State tidak dapat dibaca: '+e}}refresh();setInterval(refresh,5000);
</script></html>'''

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            body = PAGE
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/api/status":
            try:
                with open(STATE_FILE, encoding="utf-8") as f: state = json.load(f)
                symbols = []
                for name, item in (state.get("symbols") or {}).items():
                    pos = item.get("position") or {}
                    pending = item.get("pending_order")
                    alert = "Order belum pasti; periksa Binance/manual" if pending else ("OCO belum pasti; periksa Binance/manual" if pos.get("oco_pending") else "")
                    symbols.append({"symbol": name, "status": item.get("status", "unknown"), "qty": pos.get("qty"),
                        "entry": pos.get("entry"), "tp": pos.get("tp"), "sl": pos.get("sl"), "alert": alert})
                body = json.dumps({"mode": state.get("bot_mode", "unknown"), "symbols": symbols,
                    "read_at": __import__("time").time()}, allow_nan=False).encode()
            except FileNotFoundError:
                body = json.dumps({"mode":"unknown","symbols":[],"read_at":__import__("time").time(),"error":"state file not found"}).encode()
            except (OSError, ValueError, TypeError):
                body = b'{"error":"state file unreadable","symbols":[]}'
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        else:
            body = b"not found"; self.send_response(404); self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body))); self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers(); self.wfile.write(body)
    def log_message(self, fmt, *args): pass

if __name__ == "__main__":
    print(f"Read-only dashboard: http://{HOST}:{PORT} (state={STATE_FILE})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
