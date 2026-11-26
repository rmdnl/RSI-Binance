"""Read-only local dashboard. Defaults to 127.0.0.1:8060."""
from __future__ import annotations
import json, math, os, time
from pathlib import Path
from typing import Any, TypeGuard
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HOST=os.getenv("DASHBOARD_HOST","127.0.0.1");PORT=int(os.getenv("DASHBOARD_PORT","8060"));STATE_FILE=os.getenv("STATE_FILE","state.json")
STATIC_DIR = Path(__file__).parent / "static"
RSI_TF_COLORS = {"1h": "#fbbf24"}
RSI_PAIR_DASHES = ["", "6 3", "2 3", "8 3 2 3"]

def _fallback_oversold() -> float:
    """Default parameter oversold di sisi dashboard, dipakai hanya bila state belum memuatnya."""
    try:
        value = float(os.getenv("DASHBOARD_RSI_OVERSOLD", "18"))
    except (TypeError, ValueError):
        return 18.0
    return value if math.isfinite(value) and 0 <= value <= 100 else 18.0

OVERSOLD_FALLBACK = _fallback_oversold()


def _number(value: Any) -> TypeGuard[float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def oversold_threshold(state, fallback=None):
    """RSI_OVERSOLD efektif + sumbernya ("state" dari bot, "default" dari dashboard)."""
    if fallback is None:
        fallback = OVERSOLD_FALLBACK
    symbols = state.get("symbols") if isinstance(state, dict) else None
    if not isinstance(symbols, dict):
        return float(fallback), "default"
    for item in symbols.values():
        diag = item.get("diagnostics") if isinstance(item, dict) else None
        value = diag.get("oversold") if isinstance(diag, dict) else None
        if _number(value) and 0 <= float(value) <= 100:
            return float(value), "state"
    return float(fallback), "default"


def rsi_oversold_alerts(state, fallback=None):
    """Pair dengan RSI 1h terkini di bawah parameter oversold (strict <), terurut paling oversold."""
    threshold, source = oversold_threshold(state, fallback)
    symbols = state.get("symbols") if isinstance(state, dict) else None
    rows = []
    for symbol, item in (symbols if isinstance(symbols, dict) else {}).items():
        diag = item.get("diagnostics") if isinstance(item, dict) else None
        diag = diag if isinstance(diag, dict) else {}
        value = diag.get("rsi")
        if not _number(value) or float(value) >= threshold:
            continue
        rows.append({
            "symbol": symbol,
            "rsi": float(value),
            "oversold": threshold,
            "trigger": float(diag["trigger"]) if _number(diag.get("trigger")) else None,
            "candle_ts": int(diag["candle_ts"]) if _number(diag.get("candle_ts")) else None,
            "evaluated_at": float(diag["evaluated_at"]) if _number(diag.get("evaluated_at")) else None,
            "armed": bool(diag.get("oversold_seen")),
            "candle_live": bool(diag.get("candle_live")),
            "source": source,
        })
    rows.sort(key=lambda row: (row["rsi"], row["symbol"]))
    return rows


def status_payload(state):
    """Bentuk respons /api/status, termasuk alert RSI 1h di bawah oversold."""
    threshold, source = oversold_threshold(state)
    return {
        "mode": state.get("bot_mode", "unknown"),
        "portfolio": state.get("portfolio_snapshot"),
        "realized_tracking_since": state.get("realized_tracking_since"),
        "symbols": [{"symbol": symbol, "status": item.get("status", "unknown"),
                     "diagnostics": item.get("diagnostics"), "rsi_history": item.get("rsi_history", {})}
                    for symbol, item in state.get("symbols", {}).items()],
        "events": list(state.get("events", []))[-100:],
        "oversold": threshold,
        "oversold_source": source,
        "alerts": rsi_oversold_alerts(state),
        "read_at": time.time(),
    }


PAGE='''<!doctype html><html lang="id"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>RSI Bot Operations</title><script src="/static/react.min.js"></script><script src="/static/react-dom.min.js"></script><script src="/static/prop-types.min.js"></script><script src="/static/recharts.min.js"></script><style>
body{font:15px system-ui;background:#10151d;color:#e8eef6;margin:2rem}h1{color:#7dd3fc}.muted{color:#aab8ca}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}.card{background:#1b2532;padding:1rem;border-radius:10px;overflow:auto}.value{font-size:1.35rem;font-weight:700;margin-top:.4rem}table{width:100%;border-collapse:collapse;min-width:650px}td,th{text-align:left;padding:.6rem;border-bottom:1px solid #344256}.warn{color:#fbbf24}.error{color:#fca5a5}.info{color:#93c5fd}.pos{color:#86efac}.neg{color:#fca5a5}section{margin-top:1.4rem}.event{padding:.55rem;border-bottom:1px solid #344256;white-space:pre-wrap;overflow-wrap:anywhere}.chart-wrap{height:360px;min-width:0}.pair-picks{display:flex;flex-wrap:wrap;gap:.8rem;margin:.8rem 0}.pair-picks label{display:flex;gap:.35rem;align-items:center;color:#cbd5e1}.chart-empty{padding:2rem;text-align:center;color:#aab8ca}.alert-card{border-left:4px solid #f59e0b}.alert-card h2{margin-top:0;font-size:1.05rem}
</style><h1>RSI Binance — Portfolio &amp; health</h1><p id="meta" class="muted">Memuat snapshot…</p><div id="content"></div><script>
const TF_ORDER=['1h'],TF_COLORS=__TF_COLORS__,DASHES=__DASHES__,OVERSOLD_FALLBACK=__OVERSOLD_FALLBACK__;const fmt=(x,n=4)=>x==null?'—':Number(x).toLocaleString(undefined,{maximumFractionDigits:n});const safe=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let selectedRsiPairs=[];
function renderRsiChart(d){
 const available=(d.symbols||[]).filter(x=>x.rsi_history&&Object.values(x.rsi_history).some(a=>Array.isArray(a)&&a.length));
 if(!selectedRsiPairs.length)selectedRsiPairs=available.map(x=>x.symbol);
 selectedRsiPairs=selectedRsiPairs.filter(s=>available.some(x=>x.symbol===s));
 const picks=document.querySelector('#rsi-pair-picks'),mount=document.querySelector('#rsi-chart');if(!picks||!mount)return;
 picks.innerHTML=available.map(x=>'<label><input type="checkbox" value="'+safe(x.symbol)+'" '+(selectedRsiPairs.includes(x.symbol)?'checked':'')+'> '+safe(x.symbol)+'</label>').join('')||'<span class="muted">Belum ada histori RSI.</span>';
 picks.querySelectorAll('input').forEach(el=>el.addEventListener('change',()=>{selectedRsiPairs=Array.from(picks.querySelectorAll('input:checked')).map(x=>x.value);renderRsiChart(d)}));
 if(!window.Recharts||!window.React||!window.ReactDOM){mount.innerHTML='<div class="chart-empty">Library Recharts gagal dimuat. Periksa koneksi internet browser.</div>';return}
 const R=window.Recharts,React=window.React,create=React.createElement;
 const ov=(d.oversold==null||!Number.isFinite(Number(d.oversold)))?OVERSOLD_FALLBACK:Number(d.oversold);
 const selected=available.filter(x=>selectedRsiPairs.includes(x.symbol));
 if(!selected.length){mount.innerHTML='<div class="chart-empty">Pilih minimal satu pair untuk menampilkan grafik.</div>';return}
 const merged=new Map(),lines=[];
 selected.forEach((item,i)=>TF_ORDER.forEach(tf=>{
  const key=item.symbol.replace(/[^a-zA-Z0-9]/g,'_')+'_'+tf;
  ((item.rsi_history||{})[tf]||[]).forEach(point=>{const t=Number(point.timestamp);if(!merged.has(t))merged.set(t,{timestamp:t});merged.get(t)[key]=Number(point.value)});
  lines.push({key,name:item.symbol+' RSI '+tf,stroke:TF_COLORS[tf]||'#94a3b8',dash:selected.length>1?DASHES[i%DASHES.length]:''});
 }));
 const data=Array.from(merged.values()).sort((a,b)=>a.timestamp-b.timestamp);
 const children=[
  create(R.CartesianGrid,{key:'grid',strokeDasharray:'3 3',stroke:'#344256'}),
  create(R.XAxis,{key:'x',dataKey:'timestamp',type:'number',domain:['dataMin','dataMax'],tickFormatter:v=>new Date(v).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),stroke:'#aab8ca',minTickGap:35}),
  create(R.YAxis,{key:'y',domain:[0,100],ticks:[0,20,40,60,80,100],stroke:'#aab8ca'}),
  create(R.Tooltip,{key:'tooltip',labelFormatter:v=>new Date(Number(v)).toLocaleString(),formatter:(v,n)=>[Number(v).toFixed(2),n],contentStyle:{background:'#1b2532',border:'1px solid #475569',borderRadius:8},labelStyle:{color:'#e8eef6'}}),
  create(R.Legend,{key:'legend',wrapperStyle:{color:'#e8eef6'}}),
  create(R.ReferenceLine,{key:'oversold',y:ov,stroke:'#f59e0b',strokeDasharray:'5 5',label:{value:'Oversold '+ov,fill:'#fbbf24',position:'insideTopRight'}})
 ];
 lines.forEach(line=>children.push(create(R.Line,{key:line.key,type:'monotone',dataKey:line.key,name:line.name,stroke:line.stroke,strokeDasharray:line.dash||undefined,strokeWidth:2,dot:false,connectNulls:true,isAnimationActive:false})));
 ReactDOM.render(create(R.ResponsiveContainer,{width:'100%',height:'100%'},create(R.LineChart,{data,margin:{top:16,right:24,left:0,bottom:8}},...children)),mount);
}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||r.status);const p=d.portfolio;if(!p){document.querySelector('#meta').textContent='Belum ada snapshot; pastikan bot aktif.';return}const age=Math.max(0,Date.now()/1000-p.updated_at);document.querySelector('#meta').textContent=`Mode ${safe(p.mode)} · quote ${safe(p.quote)} · snapshot ${new Date(p.updated_at*1000).toLocaleString()} (${Math.round(age)}s) · ${safe(p.valuation)}`;
const alerts=d.alerts||[],alertSet=new Set(alerts.map(a=>a.symbol));let out="";if(alerts.length){out+="<section><div class=\"card alert-card\"><h2 class=\"warn\">Alert: RSI 1h di bawah oversold "+fmt(d.oversold,2)+"</h2><table><thead><tr><th>Pair</th><th>RSI 1h</th><th>Oversold</th><th>Candle 1h</th><th>Status gate bot</th></tr></thead><tbody>"+alerts.map(a=>"<tr><td>"+safe(a.symbol)+"</td><td class=\"warn\">"+fmt(a.rsi,2)+"</td><td>"+fmt(a.oversold,2)+"</td><td>"+(a.candle_ts?new Date(a.candle_ts).toLocaleString():"—")+(a.candle_live?" (berjalan)":" (tertutup)")+"</td><td>"+(a.armed?"lookback oversold terpenuhi, menunggu cross-up"+(a.trigger==null?"":" ke "+fmt(a.trigger,2)):"belum ada cross-up trigger")+"</td></tr>").join("")+"</tbody></table><p class=\"muted\">Ambang = "+(d.oversold_source==="state"?"RSI_OVERSOLD yang dilaporkan bot di state":"default dashboard DASHBOARD_RSI_OVERSOLD karena state belum memuat parameter bot")+" · hanya pemberitahuan, tidak mengirim order.</p></div></section>"}out+='<div class="grid">'+cards.map((x,i)=>`<div class="card"><div class="muted">${x[0]}</div><div class="value ${i===1?(p.unrealized_pnl>=0?'pos':'neg'):i===2?(p.realized_pnl>=0?'pos':'neg'):''}">${x[1]}</div></div>`).join('')+'</div>';
const symbolRows=(d.symbols||[]).map(x=>{const z=x.diagnostics||{};return `<tr><td>${safe(x.symbol)}</td><td>${safe(x.status)}</td><td class="${alertSet.has(x.symbol)?'warn':''}">${fmt(z.rsi,2)}</td><td>${z.candle_live===true?'berjalan':(z.candle_live===false?'tertutup':'—')}</td><td>${z.candle_ts?new Date(z.candle_ts).toLocaleString():'Belum dievaluasi'}</td><td>${safe(z.reason||'Menunggu evaluasi candle 1h')}</td></tr>`}).join('');
out+='<section><h2>Tren RSI 24 Jam</h2><div class="card"><p class="muted">Pilih pair aktif untuk menampilkan tren RSI 1h 24 jam terakhir; titik terakhir adalah candle 1 jam yang sedang berjalan dan nilainya diperbarui setiap <code>HISTORY_REFRESH_SECONDS</code> detik (belum final sampai candle itu close). Bila lebih dari satu pair dipilih, pola garis putus-putus membedakan pair.</p><div id="rsi-pair-picks" class="pair-picks"></div><div id="rsi-chart" class="chart-wrap"></div></div></section>';
out+='<section><h2>Diagnostik RSI &amp; sinyal</h2><div class="card"><table><thead><tr><th>Pair</th><th>Status</th><th>RSI 1h</th><th>Candle</th><th>Evaluasi terakhir</th><th>Alasan / hasil</th></tr></thead><tbody>'+(symbolRows||'<tr><td colspan="6">Belum ada data pair</td></tr>')+'</tbody></table></div><p class="muted">Kolom RSI 1h memakai nilai terakhir yang dilaporkan bot: candle 1 jam berjalan (realtime) bila <code>SIGNAL_USE_CLOSED=false</code>, atau candle tertutup bila <code>true</code> - kolom Candle menandai modenya. Alert oversold muncul bila RSI terkini lebih kecil dari ambang oversold (perbandingan ketat, RSI sama persis dengan ambang tidak memicu alert). Titik terakhir grafik adalah candle 1 jam berjalan yang diperbarui tiap <code>HISTORY_REFRESH_SECONDS</code> detik; titik sebelumnya adalah candle tertutup yang tidak berubah lagi.</p></section>';
if(p.unpriced_assets?.length)out+=`<p class="warn">Aset tanpa nilai ${safe(p.quote)} (dikecualikan dari equity): ${p.unpriced_assets.map(safe).join(', ')}</p>`;
out+='<section><h2>Posisi</h2><div class="card"><table><thead><tr><th>Pair</th><th>Qty</th><th>Entry</th><th>Mark</th><th>Nilai</th><th>Unrealized PnL</th><th>TP / SL</th></tr></thead><tbody>'+((p.positions||[]).map(x=>`<tr><td>${safe(x.symbol)}</td><td>${fmt(x.qty,8)}</td><td>${fmt(x.entry,8)}</td><td>${fmt(x.mark,8)}</td><td>${fmt(x.value,4)} ${safe(p.quote)}</td><td class="${x.unrealized_pnl>=0?'pos':'neg'}">${fmt(x.unrealized_pnl,4)} ${safe(p.quote)}</td><td>${fmt(x.tp,8)} / ${fmt(x.sl,8)}</td></tr>`).join('')||'<tr><td colspan="7">Tidak ada posisi tercatat</td></tr>')+'</tbody></table></div></section>';
out+='<section><h2>Saldo koin</h2><div class="card"><table><thead><tr><th>Aset</th><th>Total</th><th>Free</th><th>Used</th><th>Nilai ('+safe(p.quote)+')</th></tr></thead><tbody>'+((p.assets||[]).map(x=>`<tr><td>${safe(x.asset)}</td><td>${fmt(x.total,8)}</td><td>${fmt(x.free,8)}</td><td>${fmt(x.used,8)}</td><td>${x.value_quote==null?'Tidak tersedia':fmt(x.value_quote,4)}</td></tr>`).join('')||'<tr><td colspan="5">Belum ada saldo</td></tr>')+'</tbody></table></div></section>';
const events=d.events||[];out+='<section><h2>Log event &amp; error (terbaru)</h2><div class="card">'+(events.map(e=>`<div class="event ${safe((e.level||'INFO').toLowerCase())}"><b>${safe(e.level)}</b> · ${new Date(e.time*1000).toLocaleString()} · ${safe(e.message)}</div>`).join('')||'<p class="muted">Belum ada event.</p>')+'</div></section>';
out+=`<p class="muted">Realized PnL hanya dihitung dari transaksi bot sejak ${p.realized_tracking_since?new Date(p.realized_tracking_since*1000).toLocaleString():'tracking dimulai'}; transaksi manual dan riwayat lama tidak termasuk. Equity live estimasi, aset tanpa harga dikecualikan.</p>`;document.querySelector('#content').innerHTML=out;renderRsiChart(d);
}catch(e){document.querySelector('#meta').textContent='Dashboard/API status error: '+e}}
refresh();setInterval(refresh,5000);
</script></html>'''
PAGE = (PAGE.replace('__TF_COLORS__', json.dumps(RSI_TF_COLORS))
           .replace('__DASHES__', json.dumps(RSI_PAIR_DASHES))
           .replace('__OVERSOLD_FALLBACK__', json.dumps(OVERSOLD_FALLBACK)))
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  # Serve static files
  if self.path.startswith("/static/"):
    file_path = STATIC_DIR / self.path[len("/static/"):]
    try:
      with open(file_path, "rb") as f:
        body = f.read()
      status = 200
      if file_path.suffix == ".js":
        ctype = "application/javascript; charset=utf-8"
      elif file_path.suffix == ".css":
        ctype = "text/css; charset=utf-8"
      else:
        ctype = "application/octet-stream"
    except (FileNotFoundError, OSError):
      body = b"not found"; status = 404; ctype = "text/plain"
  elif self.path == "/":
    body = PAGE.encode("utf-8"); status = 200; ctype = "text/html; charset=utf-8"
  elif self.path == "/api/status":
    try:
      with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
      body = json.dumps(status_payload(state), allow_nan=False).encode()
      status = 200
    except FileNotFoundError:
      body = b'{"error":"state file not found"}'; status = 404
    except (OSError, ValueError, TypeError):
      body = b'{"error":"state file unreadable"}'; status = 503
    ctype = "application/json; charset=utf-8"
  else:
    body = b"not found"; status = 404; ctype = "text/plain"
  self.send_response(status)
  self.send_header("Content-Type", ctype)
  self.send_header("Cache-Control", "no-store")
  self.send_header("X-Content-Type-Options", "nosniff")
  self.send_header("Content-Length", str(len(body)))
  self.end_headers()
  self.wfile.write(body)
 def log_message(self,*args):pass


if __name__ == "__main__":
    print(f"Read-only dashboard: http://{HOST}:{PORT} (state={STATE_FILE})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
