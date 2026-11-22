#!/usr/bin/env python3
"""Binance spot RSI bot with conservative order handling and bounded REST polling."""
from __future__ import annotations
import json, logging, math, os, re, signal, sys, time, uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any, Optional
import ccxt
from dotenv import load_dotenv

log = logging.getLogger("bot")
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
TERMINAL = {"closed", "canceled", "cancelled", "expired", "rejected"}

def env(name, default=""):
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()

def finite_float(name, default, minimum, maximum):
    try: value = float(env(name, str(default)))
    except ValueError as exc: raise ValueError(f"{name} harus berupa angka") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum: raise ValueError(f"{name} harus antara {minimum:g} dan {maximum:g}")
    return value

def finite_int(name, default, minimum, maximum):
    try: value = int(env(name, str(default)))
    except ValueError as exc: raise ValueError(f"{name} harus bilangan bulat") from exc
    if not minimum <= value <= maximum: raise ValueError(f"{name} harus antara {minimum} dan {maximum}")
    return value

def boolean(name, default):
    raw = env(name, "true" if default else "false").lower()
    if raw not in {"1","true","yes","y","on","0","false","no","n","off"}: raise ValueError(f"{name} harus true/false")
    return raw in {"1","true","yes","y","on"}

@dataclass
class Config:
    key:str; secret:str; testnet:bool; dry_run:bool; quote:str; symbols:list[str]
    period:int; oversold:float; trigger:float; max4:float; max1:float; arm:int; rearm:float; context_closed:bool
    position_pct:float; max_open:int; tp_min:float; tp_max:float; tp_atr:float; sl_pct:float; use_oco:bool
    cooldown:float; lockout:float; max_entries:int; window_hours:float; loop_seconds:float; dry_balance:float; paper_fee:float
    tg_token:str; tg_chat:str; state_file:str; log_file:str
    @classmethod
    def from_env(cls):
        symbols=[s for s in re.split(r"[,\s;]+",env("SYMBOLS")) if s]
        cfg=cls(env("BINANCE_API_KEY"),env("BINANCE_API_SECRET"),boolean("BINANCE_TESTNET",False),boolean("DRY_RUN",True),env("QUOTE_ASSET","USDT").upper(),symbols,
            finite_int("RSI_PERIOD",14,2,200),finite_float("RSI_OVERSOLD",30,0,100),finite_float("RSI_TRIGGER",32,0,100),finite_float("RSI_4H_MAX",35,0,100),finite_float("RSI_1H_MAX",35,0,100),finite_int("ARM_LOOKBACK",12,1,500),finite_float("REARM_LEVEL",50,0,100),boolean("CONTEXT_USE_CLOSED",False),
            finite_float("POSITION_PCT",30,.0001,100),finite_int("MAX_OPEN_POSITIONS",3,1,100),finite_float("TP_MIN_PCT",1,.0001,100),finite_float("TP_MAX_PCT",3,.0001,100),finite_float("TP_ATR_MULT",1.5,0,100),finite_float("SL_PCT",2,.0001,100),boolean("USE_OCO",True),finite_float("COOLDOWN_MINUTES",240,0,525600),finite_float("REENTRY_LOCKOUT_MINUTES",120,0,525600),finite_int("MAX_ENTRIES_PER_WINDOW",2,1,1000),finite_float("ENTRY_WINDOW_HOURS",24,.0001,876000),finite_float("LOOP_SECONDS",10,1,3600),finite_float("DRY_RUN_BALANCE",1000,.0001,1e15),finite_float("PAPER_FEE_PCT",.1,0,100),env("TELEGRAM_BOT_TOKEN"),env("TELEGRAM_CHAT_ID"),env("STATE_FILE","state.json"),env("LOG_FILE","bot.log"))
        errors=[]
        if not symbols: errors.append("SYMBOLS kosong")
        if not cfg.dry_run and not (cfg.key and cfg.secret): errors.append("live mode perlu API key dan secret")
        if cfg.trigger<=cfg.oversold: errors.append("RSI_TRIGGER harus lebih besar dari RSI_OVERSOLD")
        if cfg.tp_max<cfg.tp_min: errors.append("TP_MAX_PCT harus >= TP_MIN_PCT")
        if errors: raise ValueError("Konfigurasi tidak valid:\n  - "+"\n  - ".join(errors))
        return cfg

def _rsi_value(g,l): return 50.0 if l==0 and g==0 else (100.0 if l==0 else 100.0-100.0/(1.0+g/l))
def rsi_series(closes,period=14):
    out=[None]*len(closes)
    if len(closes)<=period:return out
    gains=losses=0.0
    for i in range(1,period+1):
        d=closes[i]-closes[i-1];gains+=max(d,0);losses+=max(-d,0)
    ag,al=gains/period,losses/period;out[period]=_rsi_value(ag,al)
    for i in range(period+1,len(closes)):
        d=closes[i]-closes[i-1];ag=(ag*(period-1)+max(d,0))/period;al=(al*(period-1)+max(-d,0))/period;out[i]=_rsi_value(ag,al)
    return out

def update_rsi_history(history,timeframe,candles,period,now_ms):
    """Keep closed-candle RSI samples from the trailing 24 hours."""
    tf_ms=TF_MS[timeframe];cutoff=now_ms-24*60*60*1000
    rows={int(x["timestamp"]):float(x["value"]) for x in history.get(timeframe,[]) if x.get("timestamp") is not None and x.get("value") is not None and int(x["timestamp"])>=cutoff}
    values=rsi_series([x[4] for x in candles],period)
    for candle,value in zip(candles,values):
        ts=int(candle[0])
        if ts>=cutoff and ts+tf_ms<=now_ms and value is not None and math.isfinite(float(value)):
            rows[ts]=float(value)
    history[timeframe]=[{"timestamp":ts,"value":rows[ts]} for ts in sorted(rows)][-100:]
    return history[timeframe]

def closed(candles,tf_ms,now_ms): return [c for c in candles if int(c[0])+tf_ms<=now_ms]
def atr_pct(candles,period=14):
    if len(candles)<period+1:return None
    tr=[max(c[2]-c[3],abs(c[2]-p[4]),abs(c[3]-p[4])) for p,c in zip(candles,candles[1:])];atr=sum(tr[:period])/period
    for x in tr[period:]:atr=(atr*(period-1)+x)/period
    return atr/candles[-1][4]*100 if candles[-1][4]>0 else None

class Notifier:
    def __init__(self,token,chat): self.token,self.chat=token,chat
    def send(self,text):
        if not(self.token and self.chat):return
        try:
            import requests
            r=requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",json={"chat_id":self.chat,"text":text,"disable_web_page_preview":True},timeout=10)
            if not r.ok:log.warning("Telegram gagal: HTTP %s",r.status_code)
        except Exception:log.warning("Telegram gagal")

class PaperBroker:
    def __init__(self,state,fee): self.state,self.fee=state,fee/100
    def quote_free(self): return float(self.state["paper_quote"])
    def base_free(self,s):
        p=self.state["symbols"].get(s,{}).get("position");return float(p["qty"] if p else 0)
    def buy(self,s,cost,price,cid):
        qty=cost/price*(1-self.fee);self.state["paper_quote"]-=cost;return {"id":cid,"qty":qty,"price":price,"cost":cost,"status":"closed"}
    def sell(self,s,qty,price,cid):
        proceeds=qty*price*(1-self.fee);self.state["paper_quote"]+=proceeds;return {"id":cid,"qty":qty,"price":price,"proceeds":proceeds,"status":"closed"}

class LiveBroker:
    def __init__(self,ex,quote): self.ex,self.quote=ex,quote
    def fetch_balance(self): return self.ex.fetch_balance()
    def quote_free(self): return float((self.fetch_balance().get("free") or {}).get(self.quote) or 0)
    def base_free(self,s): return float((self.fetch_balance().get("free") or {}).get(s.split("/")[0]) or 0)
    def buy(self,s,cost,price,cid):
        cost=float(self.ex.cost_to_precision(s,cost));o=self.ex.create_market_buy_order_with_cost(s,cost,{"newClientOrderId":cid})
        if o.get("average") is None or o.get("filled") is None:o=self.ex.fetch_order(o["id"],s)
        filled=float(o.get("filled") or 0)
        if filled<=0 or str(o.get("status","")).lower() not in {"closed","filled"}:raise RuntimeError(f"buy status belum final; order={o.get('id')} filled={filled}")
        qty=filled
        for f in o.get("fees") or ([o["fee"]] if o.get("fee") else []):
            if f and f.get("currency")==s.split("/")[0]:qty-=float(f.get("cost") or 0)
        avg=float(o.get("average") or price);return {"id":str(o["id"]),"qty":qty,"price":avg,"cost":float(o.get("cost") or filled*avg)}
    def sell(self,s,qty,price,cid):
        qty=float(self.ex.amount_to_precision(s,min(qty,self.base_free(s))));mn=((self.ex.market(s).get("limits") or {}).get("amount") or {}).get("min") or 0
        if qty<=0 or qty<float(mn):return None
        o=self.ex.create_market_sell_order(s,qty,{"newClientOrderId":cid})
        if o.get("average") is None or o.get("filled") is None:o=self.ex.fetch_order(o["id"],s)
        status=str(o.get("status") or "").lower()
        if status not in TERMINAL:raise RuntimeError(f"sell masih berstatus {status or 'unknown'}; jangan retry otomatis")
        filled=float(o.get("filled") or 0);avg=float(o.get("average") or price);proceeds=float(o.get("cost") or filled*avg)
        for f in o.get("fees") or ([o["fee"]] if o.get("fee") else []):
            if f and f.get("currency")==self.quote:proceeds-=float(f.get("cost") or 0)
        return {"id":str(o["id"]),"qty":filled,"price":avg,"proceeds":proceeds,"status":status}
    def place_oco(self,s,qty,tp,sl,cid):
        qty=float(self.ex.amount_to_precision(s,min(qty,self.base_free(s))))
        if qty<=0:raise RuntimeError("qty OCO nol")
        params={"symbol":self.ex.market_id(s),"side":"SELL","quantity":qty,"aboveType":"LIMIT_MAKER","abovePrice":self.ex.price_to_precision(s,tp),"belowType":"STOP_LOSS","belowStopPrice":self.ex.price_to_precision(s,sl),"listClientOrderId":cid}
        res=self.ex.request("orderList/oco","private","POST",params);tp_id=sl_id=None
        for x in res.get("orderReports") or []:
            if str(x.get("type","")).upper().startswith("STOP_LOSS"):sl_id=str(x["orderId"])
            else:tp_id=str(x["orderId"])
        if not(tp_id and sl_id):raise RuntimeError("respons OCO tidak lengkap; perlu rekonsiliasi manual")
        return {"list_id":str(res.get("orderListId")),"client_list_id":cid,"tp_id":tp_id,"sl_id":sl_id}
    def poll_oco(self,s,oco):
        opened={str(x["id"]) for x in self.ex.fetch_open_orders(s)};legs=[]
        for name,oid in (("TP",oco["tp_id"]),("SL",oco["sl_id"])):
            if str(oid) in opened:return "active",None
            o=self.ex.fetch_order(str(oid),s);status=str(o.get("status") or "").lower();filled=float(o.get("filled") or 0);amount=float(o.get("amount") or 0)
            if status not in TERMINAL:return "active",None
            if filled:
                avg=float(o.get("average") or o.get("price") or 0);proceeds=float(o.get("cost") or filled*avg)
                for f in o.get("fees") or ([o["fee"]] if o.get("fee") else []):
                    if f and f.get("currency")==self.quote:proceeds-=float(f.get("cost") or 0)
                legs.append({"leg":name,"qty":filled,"price":avg,"proceeds":proceeds,"full":amount>0 and filled>=amount*(1-1e-8)})
        if not legs:return "gone",None
        leg=next((x for x in legs if x["full"]),legs[0]);return ("filled" if leg["full"] else "partial"),leg

class Bot:
    def __init__(self,cfg,ex,broker,notifier,state):
        self.cfg,self.ex,self.broker,self.notifier,self.state=cfg,ex,broker,notifier,state;self.running=True
        self.price_cache={};self.last_oco_poll={};self.last_portfolio_update=0.0;self.pause_until=0.0;self.rate_limit_events=0
    def save(self):
        tmp=self.cfg.state_file+".tmp"
        with open(tmp,"w",encoding="utf-8") as f:json.dump(self.state,f,indent=2,allow_nan=False)
        os.replace(tmp,self.cfg.state_file)
    def event(self,level,message):
        clean=str(message)[:500];events=self.state.setdefault("events",[]);events.append({"time":time.time(),"level":level.upper(),"message":clean});del events[:-300]
        getattr(log,level.lower(),log.info)(clean)
    def notify(self,text):self.event("INFO",text.replace("\n"," | "));self.notifier.send(text)
    def price(self,s,max_age=8):
        now=time.time();cached=self.price_cache.get(s)
        if cached and now-cached[0]<max_age:return cached[1]
        raw=self.ex.fetch_ticker(s).get("last")
        if raw is None or not math.isfinite(float(raw)) or float(raw)<=0:raise ValueError(f"harga {s} tidak valid")
        value=float(raw);self.price_cache[s]=(now,value);return value
    def add_realized(self,s,qty,proceeds,pos):
        oldq=float(pos.get("qty") or 0);cost=float(pos.get("cost") or 0);part=cost*min(qty,oldq)/oldq if oldq else 0;pnl=float(proceeds)-part
        self.state["realized_pnl"]=float(self.state.get("realized_pnl",0))+pnl
        records=self.state.setdefault("realized_trades",[]);records.append({"time":time.time(),"symbol":s,"qty":qty,"proceeds":proceeds,"cost":part,"pnl":pnl});del records[:-500]
        return pnl
    def update_portfolio(self):
        assets=[];unpriced=[];positions=[];now=time.time()
        if self.cfg.dry_run:
            quote=float(self.state.get("paper_quote",0));equity=quote;assets.append({"asset":self.cfg.quote,"total":quote,"free":quote,"used":0,"value_quote":quote})
            for s,st in self.state["symbols"].items():
                p=st.get("position")
                if not p:continue
                base=s.split("/")[0];qty=float(p.get("qty") or 0);mark=self.price(s,15);value=qty*mark;equity+=value
                assets.append({"asset":base,"total":qty,"free":qty,"used":0,"value_quote":value})
                positions.append({"symbol":s,"qty":qty,"entry":float(p["entry"]),"mark":mark,"value":value,"unrealized_pnl":value-float(p["cost"]),"tp":p.get("tp"),"sl":p.get("sl")})
            valuation="paper portfolio"
        else:
            bal=self.broker.fetch_balance();total=bal.get("total") or {};free=bal.get("free") or {};used=bal.get("used") or {};equity=0.0
            for asset,raw in total.items():
                amount=float(raw or 0)
                if amount<=1e-12:continue
                value=amount if asset==self.cfg.quote else None
                if value is None:
                    pairs=[s for s,m in self.ex.markets.items() if m.get("base")==asset and m.get("quote")==self.cfg.quote and m.get("spot")]
                    for pair in pairs:
                        try:value=amount*self.price(pair,15);break
                        except Exception:continue
                if value is None:unpriced.append(asset)
                else:equity+=value
                assets.append({"asset":asset,"total":amount,"free":float(free.get(asset) or 0),"used":float(used.get(asset) or 0),"value_quote":value})
            for s,st in self.state["symbols"].items():
                p=st.get("position")
                if not p:continue
                mark=self.price(s,15);qty=float(p.get("qty") or 0);value=qty*mark
                positions.append({"symbol":s,"qty":qty,"entry":float(p["entry"]),"mark":mark,"value":value,"unrealized_pnl":value-float(p["cost"]),"tp":p.get("tp"),"sl":p.get("sl")})
            valuation="account mark-to-market"+("; incomplete: unpriced assets excluded" if unpriced else "; all assets valued")
        headers=getattr(self.ex,"last_response_headers",None) or {};weights={k:v for k,v in headers.items() if str(k).lower().startswith("x-mbx-used-weight-")}
        self.state["portfolio_snapshot"]={"updated_at":now,"quote":self.cfg.quote,"equity":equity,"realized_pnl":float(self.state.get("realized_pnl",0)),"unrealized_pnl":sum(x["unrealized_pnl"] for x in positions),"assets":assets,"positions":positions,"unpriced_assets":unpriced,"valuation":valuation,"mode":self.state.get("bot_mode","unknown"),"api_weight_headers":weights,"rate_limit_events":self.rate_limit_events,"paused_until":self.pause_until}
        self.last_portfolio_update=now
    def process(self,s):
        st=self.state["symbols"][s];now=time.time()
        if now<self.pause_until:return
        if st.get("pending_order") or (st.get("position") or {}).get("oco_pending"):return
        if st["status"]=="cooldown":
            if now<st["cooldown_until"]:return
            st["status"]="idle";st["cooldown_until"]=0;self.save()
        if st["status"]=="in_position":self.check_exit(s,st,now)
        else:self.scan_entry(s,st,now)
    def check_exit(self,s,st,now):
        p=st["position"]
        if p.get("oco"):
            # OCO status polling is deliberately slower than the main loop.
            if now-self.last_oco_poll.get(s,0)<30:return
            self.last_oco_poll[s]=now;status,res=self.broker.poll_oco(s,p["oco"])
            if status=="active":return
            if status=="gone":p["oco"]=None;self.save();self.event("WARNING",f"OCO {s} hilang tanpa fill; pemantauan lokal aktif");return
            oldq=float(p["qty"]);oldcost=float(p["cost"]);sold=min(float(res["qty"]),oldq);self.add_realized(s,sold,res["proceeds"],p)
            if status=="partial" or sold<oldq*(1-1e-8):
                rem=max(0,oldq-sold);p["qty"]=rem;p["cost"]=oldcost*rem/oldq if oldq else 0;p["oco"]=None;self.save();self.event("WARNING",f"{s}: OCO fill parsial {sold:g}; sisa {rem:g} dipantau");return
            self.record_exit(s,st,res["leg"],res,now,already_realized=True);return
        mark=self.price(s)
        if mark<=p["sl"]:reason="SL"
        elif mark>=p["tp"]:reason="TP"
        else:
            if not self.cfg.dry_run and self.cfg.use_oco and now>=p.get("oco_retry_at",0):self.arm_oco(s,st,p)
            return
        self.exit_market(s,st,reason,mark,now)
    def arm_oco(self,s,st,p):
        cid=p.get("oco_client_id") or "rsi-oco-"+uuid.uuid4().hex[:24];p["oco_client_id"]=cid;p["oco_pending"]=True;self.save()
        try:o=self.broker.place_oco(s,p["qty"],p["tp"],p["sl"],cid)
        except Exception as e:p["oco_error"]=type(e).__name__;self.event("ERROR",f"OCO {s} belum dapat dipastikan ({type(e).__name__}); periksa Binance, retry otomatis dihentikan");self.save();return False
        p["oco"]=o;p["oco_pending"]=False;p.pop("oco_error",None);self.save();return True
    def exit_market(self,s,st,reason,price,now):
        p=st["position"];cid="rsi-s-"+uuid.uuid4().hex[:24];st["pending_order"]={"type":"sell","client_id":cid,"reason":reason,"created_at":now};self.save()
        try:r=self.broker.sell(s,p["qty"],price,cid)
        except Exception:self.event("ERROR",f"Sell {s} ambiguous; pair dibekukan untuk rekonsiliasi manual");self.save();raise
        if not r:st["pending_order"]=None;self.save();self.event("ERROR",f"{s}: sell tidak terkirim/qty di bawah minimum; posisi tetap tercatat");return
        st["pending_order"]=None;oldq=float(p["qty"]);oldcost=float(p["cost"]);sold=min(float(r["qty"]),oldq);self.add_realized(s,sold,r["proceeds"],p)
        if sold<oldq*(1-1e-8):
            rem=oldq-sold;p["qty"]=rem;p["cost"]=oldcost*rem/oldq;p["oco"]=None;self.save();self.event("WARNING",f"{s}: market sell parsial; sisa {rem:g} tercatat");return
        self.record_exit(s,st,reason,r,now,already_realized=True)
    def record_exit(self,s,st,reason,res,now,already_realized=False):
        p=st["position"]
        if not already_realized:self.add_realized(s,float(res["qty"]),res["proceeds"],p)
        st["position"]=None;st["last_trade_ts"]=now;st["need_reset"]=True
        if reason=="SL":st["status"]="cooldown";st["cooldown_until"]=now+self.cfg.cooldown*60
        else:st["status"]="idle"
        self.save();self.notify(f"{reason} {s}: exit {res['price']:g}; realized PnL dicatat")
    def scan_entry(self,s,st,now):
        now_ms=int(now*1000);expected=now_ms//TF_MS["15m"]*TF_MS["15m"]-TF_MS["15m"]
        if st["last_eval_ts"]>=expected:return
        candles=closed(self.ex.fetch_ohlcv(s,"15m",limit=300),TF_MS["15m"],now_ms)
        if len(candles)<self.cfg.period+self.cfg.arm+2 or candles[-1][0]<=st["last_eval_ts"]:return
        st["last_eval_ts"]=candles[-1][0]
        values=rsi_series([x[4] for x in candles],self.cfg.period);prev,last=values[-2:]
        # Persist closed-candle RSI history for all chart timeframes once per new 15m candle.
        c4raw=self.ex.fetch_ohlcv(s,"4h",limit=100);c1raw=self.ex.fetch_ohlcv(s,"1h",limit=100)
        c4closed=closed(c4raw,TF_MS["4h"],now_ms);c1closed=closed(c1raw,TF_MS["1h"],now_ms)
        history=st.setdefault("rsi_history",{})
        update_rsi_history(history,"15m",candles,self.cfg.period,now_ms)
        update_rsi_history(history,"1h",c1closed,self.cfg.period,now_ms)
        update_rsi_history(history,"4h",c4closed,self.cfg.period,now_ms)
        r1_chart=rsi_series([x[4] for x in c1closed],self.cfg.period)
        r4_chart=rsi_series([x[4] for x in c4closed],self.cfg.period)
        rsi1_chart=r1_chart[-1] if r1_chart and r1_chart[-1] is not None else None
        rsi4_chart=r4_chart[-1] if r4_chart and r4_chart[-1] is not None else None
        diag={"evaluated_at":now,"candle_ts":candles[-1][0],"rsi_15m_previous":prev,"rsi_15m":last,"rsi_1h":rsi1_chart,"rsi_4h":rsi4_chart,"oversold_seen":False,"trigger_cross":False,"reason":""}
        st["diagnostics"]=diag
        def finish(reason):
            diag["reason"]=reason
            self.save()
        if prev is None or last is None:
            finish("Data belum cukup untuk menghitung RSI 15m");return
        if st["need_reset"] and any(x[0]>=st["last_trade_ts"]*1000 and y is not None and y>=self.cfg.rearm for x,y in zip(candles,values)):st["need_reset"]=False
        look=[x for x in values[-1-self.cfg.arm:-1] if x is not None]
        diag["oversold_seen"]=bool(look and min(look)<self.cfg.oversold)
        diag["trigger_cross"]=bool(prev<=self.cfg.trigger<last)
        if st["need_reset"]:
            finish(f"Menunggu RSI re-arm ke {self.cfg.rearm:g}");return
        if not diag["oversold_seen"]:
            finish(f"Belum ada RSI 15m di bawah oversold {self.cfg.oversold:g} pada lookback");return
        if not diag["trigger_cross"]:
            finish(f"Belum ada crossing RSI 15m ke atas trigger {self.cfg.trigger:g}");return
        if now-st["last_trade_ts"]<self.cfg.lockout*60:
            finish("Menunggu re-entry lockout");return
        st["entries"]=[t for t in st["entries"] if now-t<self.cfg.window_hours*3600]
        if len(st["entries"])>=self.cfg.max_entries:
            finish("Batas entry per window sudah tercapai");return
        if sum(x["status"]=="in_position" for x in self.state["symbols"].values())>=self.cfg.max_open:
            finish("Batas posisi terbuka sudah tercapai");return
        c4=closed(c4raw,TF_MS["4h"],now_ms) if self.cfg.context_closed else c4raw;c1=closed(c1raw,TF_MS["1h"],now_ms) if self.cfg.context_closed else c1raw
        r4,r1=rsi_series([x[4] for x in c4],self.cfg.period),rsi_series([x[4] for x in c1],self.cfg.period)
        if diag["rsi_4h"] is None or diag["rsi_1h"] is None:
            finish("Data candle belum cukup untuk RSI konteks 1h/4h");return
        if not(diag["rsi_4h"]<self.cfg.max4 and diag["rsi_1h"]<self.cfg.max1):
            finish(f"Filter konteks gagal: RSI 4h harus < {self.cfg.max4:g} dan RSI 1h harus < {self.cfg.max1:g}");return
        atr=atr_pct(closed(c1raw,TF_MS["1h"],now_ms));tp=round(min(self.cfg.tp_max,max(self.cfg.tp_min,(atr or 0)*self.cfg.tp_atr)),2)
        price=self.price(s);free=self.broker.quote_free();equity=free
        for pair,x in self.state["symbols"].items():
            if x.get("position"):equity+=float(x["position"]["qty"])*self.price(pair)
        cost=equity*self.cfg.position_pct/100;minimum=((self.ex.market(s).get("limits") or {}).get("cost") or {}).get("min") or 0
        if cost>free:
            finish("Saldo quote tidak cukup untuk ukuran posisi");return
        if minimum and cost<float(minimum):
            finish("Nilai order di bawah minimum exchange");return
        cid="rsi-b-"+uuid.uuid4().hex[:24];st["pending_order"]={"type":"buy","client_id":cid,"cost":cost,"created_at":now};diag["reason"]="Kondisi entry lolos; order BUY sedang diproses";self.save();result=self.broker.buy(s,cost,price,cid)
        p={"qty":result["qty"],"entry":result["price"],"cost":result["cost"],"sl":result["price"]*(1-self.cfg.sl_pct/100),"tp":result["price"]*(1+tp/100),"tp_pct":tp,"opened_at":now,"oco":None,"buy_order_id":result["id"]}
        st["pending_order"]=None;st.update(status="in_position",position=p,last_trade_ts=now);st["entries"].append(now);diag["reason"]="BUY berhasil dibuka"+(" (DRY RUN)" if self.cfg.dry_run else "");self.save()
        if not self.cfg.dry_run and self.cfg.use_oco:self.arm_oco(s,st,p)
        self.notify(f"BUY {s}{' [DRY RUN]' if self.cfg.dry_run else ''} @ {result['price']:g}; TP {p['tp']:g}, SL {p['sl']:g}")
    def handle_exception(self,s,exc):
        text=f"{type(exc).__name__}: {str(exc)[:240]}";name=type(exc).__name__.lower();status=str(getattr(exc,"http_status","") or "")
        headers=getattr(self.ex,"last_response_headers",None) or {};retry=headers.get("Retry-After") or headers.get("retry-after")
        if "ratelimit" in name or "ddos" in name or status in {"418","429"} or " 429" in str(exc) or " 418" in str(exc):
            self.rate_limit_events+=1
            try:delay=max(1,float(retry)) if retry is not None else (300 if status=="418" or "418" in str(exc) else 60)
            except (TypeError,ValueError):delay=60
            self.pause_until=max(self.pause_until,time.time()+delay)
            self.state["api_rate_limit"]={"time":time.time(),"delay_seconds":delay,"count":self.rate_limit_events,"error":text}
            self.event("ERROR",f"Binance rate limit/protection: REST calls paused {delay:g}s; no aggressive retry. {text}")
        else:self.event("ERROR",f"{s}: {text}")
        self.save()
    def run(self):
        self.notify("Bot dimulai"+(" (DRY RUN)" if self.cfg.dry_run else " (LIVE)"))
        while self.running:
            if time.time()>=self.pause_until:
                for s in self.cfg.symbols:
                    if not self.running:break
                    try:self.process(s)
                    except Exception as exc:self.handle_exception(s,exc)
            if time.time()-self.last_portfolio_update>=30:
                try:self.update_portfolio()
                except Exception as exc:self.event("ERROR",f"Portfolio/API snapshot gagal: {type(exc).__name__}: {str(exc)[:200]}")
            try:self.save()
            except Exception:log.exception("Gagal menyimpan state")
            time.sleep(self.cfg.loop_seconds)
        self.save();self.notify("Bot dihentikan")

def new_symbol_state():return {"status":"idle","cooldown_until":0.0,"last_eval_ts":0,"need_reset":False,"last_trade_ts":0.0,"entries":[],"position":None,"pending_order":None}
def new_state(path,cfg):
    state={}
    if os.path.exists(path):
        with open(path,encoding="utf-8") as f:state=json.load(f)
    mode="dry" if cfg.dry_run else("testnet" if cfg.testnet else "live");old=state.get("bot_mode")
    if old and old!=mode:raise RuntimeError(f"State mode {old} tidak cocok dengan {mode}; rekonsiliasi manual, jangan reset otomatis")
    state["bot_mode"]=mode;state.setdefault("paper_quote",cfg.dry_balance);state.setdefault("symbols",{});state.setdefault("realized_pnl",0.0);state.setdefault("realized_trades",[]);state.setdefault("realized_tracking_since",time.time());state.setdefault("events",[])
    for s in cfg.symbols:
        base=new_symbol_state();base.update(state["symbols"].get(s,{}));state["symbols"][s]=base
    return state
def resolve(ex,symbols,quote):
    out=[]
    for raw in symbols:
        key=raw.upper().replace("-","/").replace("_","/");sym=key if key in ex.markets else None
        if not sym:
            for m in ex.markets_by_id.get(key.replace("/",""),[]):
                if m.get("spot"):sym=m["symbol"];break
        if not sym or not ex.markets[sym].get("spot") or ex.markets[sym].get("active") is False:raise ValueError(f"Pair tidak aktif/tidak ditemukan: {raw}")
        if ex.markets[sym].get("quote")!=quote:raise ValueError(f"{sym} bukan quote {quote}")
        if sym not in out:out.append(sym)
    return out
def main():
    load_dotenv()
    try:cfg=Config.from_env()
    except ValueError as exc:print(exc);return 2
    logging.basicConfig(level=logging.INFO,handlers=[logging.StreamHandler(),RotatingFileHandler(cfg.log_file,maxBytes=2_000_000,backupCount=3,encoding="utf-8")],format="%(asctime)s %(levelname)s %(message)s")
    ex=ccxt.binance({"apiKey":cfg.key or None,"secret":cfg.secret or None,"enableRateLimit":True,"timeout":20000,"options":{"defaultType":"spot","adjustForTimeDifference":True}})
    if cfg.testnet:ex.set_sandbox_mode(True)
    ex.load_markets();cfg.symbols=resolve(ex,cfg.symbols,cfg.quote)
    try:state=new_state(cfg.state_file,cfg)
    except Exception as exc:log.error("State perlu rekonsiliasi manual: %s",exc);return 2
    broker=PaperBroker(state,cfg.paper_fee) if cfg.dry_run else LiveBroker(ex,cfg.quote);bot=Bot(cfg,ex,broker,Notifier(cfg.tg_token,cfg.tg_chat),state)
    if not cfg.dry_run:
        try:broker.quote_free()
        except Exception as exc:log.error("Gagal membaca saldo: %s",type(exc).__name__);return 2
        for s,st in state["symbols"].items():
            if st.get("pending_order") or(st.get("position") or {}).get("oco_pending"):bot.event("ERROR",f"{s}: order status belum pasti; pair dibekukan dan perlu rekonsiliasi manual")
    signal.signal(signal.SIGINT,lambda *_:setattr(bot,"running",False));signal.signal(signal.SIGTERM,lambda *_:setattr(bot,"running",False));bot.run();return 0
if __name__=="__main__":sys.exit(main())
