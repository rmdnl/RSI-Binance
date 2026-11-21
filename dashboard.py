"""Read-only local portfolio dashboard (default 127.0.0.1:8060)."""
from __future__ import annotations
import html, json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HOST=os.getenv("DASHBOARD_HOST","127.0.0.1");PORT=int(os.getenv("DASHBOARD_PORT","8060"));STATE_FILE=os.getenv("STATE_FILE","state.json")
PAGE=b'''<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>RSI Bot Portfolio</title><style>
body{font:15px system-ui;background:#10151d;color:#e8eef6;margin:2rem}h1{color:#7dd3fc}.muted{color:#aab8ca}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card{background:#1b2532;padding:1rem;border-radius:10px;overflow:auto}.value{font-size:1.45rem;font-weight:700;margin-top:.4rem}table{width:100%;border-collapse:collapse;min-width:650px}td,th{text-align:left;padding:.6rem;border-bottom:1px solid #344256}.warn{color:#fbbf24}.pos{color:#86efac}.neg{color:#fca5a5}section{margin-top:1.4rem}
</style><h1>RSI Binance — Portfolio</h1><p id="meta" class="muted">Memuat snapshot…</p><div id="content"></div><script>
const fmt=(x,n=4)=>x==null?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:n});const safe=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||r.status);const p=d.portfolio;if(!p){document.querySelector('#meta').textContent='Belum ada snapshot portfolio; pastikan bot aktif dan tunggu satu siklus.';return}const age=Math.max(0,Date.now()/1000-p.updated_at);document.querySelector('#meta').textContent=`Mode ${safe(p.mode)} · quote ${safe(p.quote)} · diperbarui ${new Date(p.updated_at*1000).toLocaleString()} · usia ${Math.round(age)} dtk · ${safe(p.valuation)}`;
const cards=[['Equity',fmt(p.equity,4)+' '+safe(p.quote)],['Unrealized PnL',fmt(p.unrealized_pnl,4)+' '+safe(p.quote)],['Realized PnL',fmt(p.realized_pnl,4)+' '+safe(p.quote)],['Posisi terbuka',String((p.positions||[]).length)]];let out='<div class="grid">'+cards.map((x,i)=>`<div class="card"><div class="muted">${x[0]}</div><div class="value ${i>0&&i<3?(Number(i===1?p.unrealized_pnl:p.realized_pnl)>=0?'pos':'neg'):''}">${x[1]}</div></div>`).join('')+'</div>';
if(p.unpriced_assets?.length)out+=`<p class="warn">Aset tanpa harga ${safe(p.quote)}: ${p.unpriced_assets.map(safe).join(', ')}. Equity hanya menjumlah aset yang berhasil dinilai.</p>`;
out+='<section><h2>Posisi bot</h2><div class="card"><table><thead><tr><th>Pair</th><th>Kuantitas</th><th>Entry</th><th>Mark</th><th>Nilai</th><th>Unrealized PnL</th><th>TP / SL</th></tr></thead><tbody>'+(p.positions||[]).map(x=>`<tr><td>${safe(x.symbol)}</td><td>${fmt(x.qty,8)}</td><td>${fmt(x.entry,8)}</td><td>${fmt(x.mark,8)}</td><td>${fmt(x.value,4)} ${safe(p.quote)}</td><td class="${x.unrealized_pnl>=0?'pos':'neg'}">${fmt(x.unrealized_pnl,4)} ${safe(p.quote)}</td><td>${fmt(x.tp,8)} / ${fmt(x.sl,8)}</td></tr>`).join('')||'<tr><td colspan="7">Tidak ada posisi tercatat</td></tr>')+'</tbody></table></div></section>';
out+='<section><h2>Koin / saldo akun</h2><div class="card"><table><thead><tr><th>Aset</th><th>Total</th><th>Free</th><th>Used</th><th>Nilai ('+safe(p.quote)+')</th></tr></thead><tbody>'+(p.assets||[]).map(x=>`<tr><td>${safe(x.asset)}</td><td>${fmt(x.total,8)}</td><td>${fmt(x.free,8)}</td><td>${fmt(x.used,8)}</td><td>${x.value_quote==null?'Tidak tersedia':fmt(x.value_quote,4)}</td></tr>`).join('')||'<tr><td colspan="5">Belum ada saldo</td></tr>')+'</tbody></table></div></section>';
out+=`<p class="muted">Realized PnL dihitung oleh bot sejak ${p.realized_tracking_since?new Date(p.realized_tracking_since*1000).toLocaleString():'tracking dimulai'}; nilai tidak mencakup transaksi manual atau transaksi sebelum tracking dimulai. Nilai live merupakan estimasi mark-to-market, bukan saldo tersedia untuk penarikan.</p>`;document.querySelector('#content').innerHTML=out;
}catch(e){document.querySelector('#meta').textContent='Dashboard error: '+e}}
refresh();setInterval(refresh,5000);
</script></html>'''
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/':body=PAGE;status=200;ctype='text/html; charset=utf-8'
  elif self.path=='/api/status':
   try:
    with open(STATE_FILE,encoding='utf-8') as f:state=json.load(f)
    data={'mode':state.get('bot_mode','unknown'),'portfolio':state.get('portfolio_snapshot'),'realized_tracking_since':state.get('realized_tracking_since'),'read_at':time.time()};body=json.dumps(data,allow_nan=False).encode();status=200
   except FileNotFoundError:body=b'{"error":"state file not found"}';status=404
   except (OSError,ValueError,TypeError):body=b'{"error":"state file unreadable"}';status=503
   ctype='application/json; charset=utf-8'
  else:body=b'not found';status=404;ctype='text/plain'
  self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
 def log_message(self,*args):pass
if __name__=='__main__':print(f'Read-only dashboard: http://{HOST}:{PORT} (state={STATE_FILE})');ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
