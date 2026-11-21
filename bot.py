#!/usr/bin/env python3
"""Binance spot RSI bot. Uncertain order outcomes fail closed; see dashboard.py."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

import ccxt
from dotenv import load_dotenv

log = logging.getLogger("bot")
TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
TERMINAL = {"closed", "canceled", "cancelled", "expired", "rejected"}


def env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def finite_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(env(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} harus berupa angka") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} harus antara {minimum:g} dan {maximum:g}")
    return value


def finite_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} harus bilangan bulat") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} harus antara {minimum} dan {maximum}")
    return value


def boolean(name: str, default: bool) -> bool:
    raw = env(name, "true" if default else "false").lower()
    if raw not in {"1", "true", "yes", "y", "on", "0", "false", "no", "n", "off"}:
        raise ValueError(f"{name} harus true/false")
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass
class Config:
    key: str; secret: str; testnet: bool; dry_run: bool; quote: str; symbols: list[str]
    period: int; oversold: float; trigger: float; max4: float; max1: float; arm: int
    rearm: float; context_closed: bool; position_pct: float; max_open: int
    tp_min: float; tp_max: float; tp_atr: float; sl_pct: float; use_oco: bool
    cooldown: float; lockout: float; max_entries: int; window_hours: float
    loop_seconds: float; dry_balance: float; paper_fee: float
    tg_token: str; tg_chat: str; state_file: str; log_file: str

    @classmethod
    def from_env(cls) -> "Config":
        symbols = [s for s in re.split(r"[,\s;]+", env("SYMBOLS")) if s]
        cfg = cls(
            env("BINANCE_API_KEY"), env("BINANCE_API_SECRET"), boolean("BINANCE_TESTNET", False),
            boolean("DRY_RUN", True), env("QUOTE_ASSET", "USDT").upper(), symbols,
            finite_int("RSI_PERIOD", 14, 2, 200), finite_float("RSI_OVERSOLD", 30, 0, 100),
            finite_float("RSI_TRIGGER", 32, 0, 100), finite_float("RSI_4H_MAX", 35, 0, 100),
            finite_float("RSI_1H_MAX", 35, 0, 100), finite_int("ARM_LOOKBACK", 12, 1, 500),
            finite_float("REARM_LEVEL", 50, 0, 100), boolean("CONTEXT_USE_CLOSED", False),
            finite_float("POSITION_PCT", 30, 0.0001, 100), finite_int("MAX_OPEN_POSITIONS", 3, 1, 100),
            finite_float("TP_MIN_PCT", 1, 0.0001, 100), finite_float("TP_MAX_PCT", 3, 0.0001, 100),
            finite_float("TP_ATR_MULT", 1.5, 0, 100), finite_float("SL_PCT", 2, 0.0001, 100),
            boolean("USE_OCO", True), finite_float("COOLDOWN_MINUTES", 240, 0, 525600),
            finite_float("REENTRY_LOCKOUT_MINUTES", 120, 0, 525600),
            finite_int("MAX_ENTRIES_PER_WINDOW", 2, 1, 1000),
            finite_float("ENTRY_WINDOW_HOURS", 24, 0.0001, 876000),
            finite_float("LOOP_SECONDS", 10, 1, 3600), finite_float("DRY_RUN_BALANCE", 1000, 0.0001, 1e15),
            finite_float("PAPER_FEE_PCT", 0.1, 0, 100), env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"),
            env("STATE_FILE", "state.json"), env("LOG_FILE", "bot.log"))
        errors = []
        if not symbols: errors.append("SYMBOLS kosong")
        if not cfg.dry_run and not (cfg.key and cfg.secret): errors.append("live mode perlu API key dan secret")
        if cfg.trigger <= cfg.oversold: errors.append("RSI_TRIGGER harus lebih besar dari RSI_OVERSOLD")
        if cfg.tp_max < cfg.tp_min: errors.append("TP_MAX_PCT harus >= TP_MIN_PCT")
        if errors: raise ValueError("Konfigurasi tidak valid:\n  - " + "\n  - ".join(errors))
        return cfg


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0: return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(closes: list[float], period: int = 14) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= period: return result
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]; gains += max(d, 0); losses += max(-d, 0)
    ag, al = gains / period, losses / period
    result[period] = _rsi_value(ag, al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        result[i] = _rsi_value(ag, al)
    return result


def closed(candles: list[list], tf_ms: int, now_ms: int) -> list[list]:
    return [c for c in candles if int(c[0]) + tf_ms <= now_ms]


def atr_pct(candles: list[list], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1: return None
    tr = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4])) for p, c in zip(candles, candles[1:])]
    atr = sum(tr[:period]) / period
    for value in tr[period:]: atr = (atr * (period - 1) + value) / period
    return atr / candles[-1][4] * 100 if candles[-1][4] > 0 else None


class Notifier:
    def __init__(self, token: str, chat: str): self.token, self.chat = token, chat
    def send(self, text: str) -> None:
        if not (self.token and self.chat): return
        try:
            import requests
            response = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat, "text": text, "disable_web_page_preview": True}, timeout=10)
            if not response.ok: log.warning("Telegram gagal: HTTP %s", response.status_code)
        except Exception: log.warning("Telegram gagal")


class PaperBroker:
    def __init__(self, state: dict, fee_pct: float): self.state, self.fee = state, fee_pct / 100
    def quote_free(self): return float(self.state["paper_quote"])
    def base_free(self, symbol):
        pos = self.state["symbols"].get(symbol, {}).get("position")
        return float(pos["qty"] if pos else 0)
    def buy(self, symbol, cost, price, client_id):
        qty = cost / price * (1 - self.fee); self.state["paper_quote"] -= cost
        return {"id": client_id, "qty": qty, "price": price, "cost": cost, "status": "closed"}
    def sell(self, symbol, qty, price, client_id):
        proceeds = qty * price * (1 - self.fee); self.state["paper_quote"] += proceeds
        return {"id": client_id, "qty": qty, "price": price, "proceeds": proceeds, "status": "closed"}


class LiveBroker:
    def __init__(self, ex: Any, quote: str): self.ex, self.quote = ex, quote
    def quote_free(self): return float((self.ex.fetch_balance().get("free") or {}).get(self.quote) or 0)
    def base_free(self, symbol): return float((self.ex.fetch_balance().get("free") or {}).get(symbol.split("/")[0]) or 0)
    def buy(self, symbol, cost, price, client_id):
        cost = float(self.ex.cost_to_precision(symbol, cost))
        order = self.ex.create_market_buy_order_with_cost(symbol, cost, {"newClientOrderId": client_id})
        if order.get("average") is None or order.get("filled") is None: order = self.ex.fetch_order(order["id"], symbol)
        filled = float(order.get("filled") or 0)
        if filled <= 0 or str(order.get("status", "")).lower() not in {"closed", "filled"}:
            raise RuntimeError(f"buy status belum final; order={order.get('id')} filled={filled}")
        qty = filled; fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            if fee and fee.get("currency") == symbol.split("/")[0]: qty -= float(fee.get("cost") or 0)
        avg = float(order.get("average") or price)
        return {"id": str(order["id"]), "qty": qty, "price": avg, "cost": float(order.get("cost") or filled * avg)}
    def sell(self, symbol, qty, price, client_id):
        qty = min(qty, self.base_free(symbol)); qty = float(self.ex.amount_to_precision(symbol, qty))
        min_qty = ((self.ex.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0
        if qty <= 0 or qty < float(min_qty): return None
        order = self.ex.create_market_sell_order(symbol, qty, {"newClientOrderId": client_id})
        if order.get("average") is None or order.get("filled") is None: order = self.ex.fetch_order(order["id"], symbol)
        filled = float(order.get("filled") or 0); avg = float(order.get("average") or price)
        proceeds = float(order.get("cost") or filled * avg)
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            if fee and fee.get("currency") == self.quote: proceeds -= float(fee.get("cost") or 0)
        return {"id": str(order["id"]), "qty": filled, "price": avg, "proceeds": proceeds,
                "status": str(order.get("status") or "").lower()}
    def place_oco(self, symbol, qty, tp, sl, client_id):
        qty = float(self.ex.amount_to_precision(symbol, min(qty, self.base_free(symbol))))
        if qty <= 0: raise RuntimeError("qty OCO nol")
        params = {"symbol": self.ex.market_id(symbol), "side": "SELL", "quantity": qty,
            "aboveType": "LIMIT_MAKER", "abovePrice": self.ex.price_to_precision(symbol, tp),
            "belowType": "STOP_LOSS", "belowStopPrice": self.ex.price_to_precision(symbol, sl),
            "listClientOrderId": client_id}
        result = self.ex.request("orderList/oco", "private", "POST", params)
        reports = result.get("orderReports") or []; tp_id = sl_id = None
        for item in reports:
            typ = str(item.get("type") or "").upper()
            if typ.startswith("STOP_LOSS"): sl_id = str(item["orderId"])
            else: tp_id = str(item["orderId"])
        if not (tp_id and sl_id): raise RuntimeError("respons OCO tidak lengkap; perlu rekonsiliasi manual")
        return {"list_id": str(result.get("orderListId")), "client_list_id": client_id, "tp_id": tp_id, "sl_id": sl_id}
    def poll_oco(self, symbol, oco):
        open_ids = {str(o["id"]) for o in self.ex.fetch_open_orders(symbol)}
        legs = []
        for name, oid in (("TP", oco["tp_id"]), ("SL", oco["sl_id"])):
            if str(oid) in open_ids: return "active", None
            order = self.ex.fetch_order(str(oid), symbol)
            status = str(order.get("status") or "").lower()
            filled = float(order.get("filled") or 0); amount = float(order.get("amount") or 0)
            if status not in TERMINAL: return "active", None
            if filled > 0:
                avg = float(order.get("average") or order.get("price") or 0)
                proceeds = float(order.get("cost") or filled * avg)
                fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
                for fee in fees:
                    if fee and fee.get("currency") == self.quote: proceeds -= float(fee.get("cost") or 0)
                legs.append({"leg": name, "qty": filled, "price": avg, "proceeds": proceeds,
                             "full": amount > 0 and filled >= amount * (1 - 1e-8)})
        if not legs: return "gone", None
        # Any partial fill is reported explicitly; never mark the whole position closed.
        leg = next((x for x in legs if x["full"]), legs[0])
        return ("filled" if leg["full"] else "partial"), leg


def new_symbol_state():
    return {"status": "idle", "cooldown_until": 0.0, "last_eval_ts": 0, "need_reset": False,
            "last_trade_ts": 0.0, "entries": [], "position": None, "pending_order": None}


class Bot:
    def __init__(self, cfg, ex, broker, notifier, state):
        self.cfg, self.ex, self.broker, self.notifier, self.state = cfg, ex, broker, notifier, state
        self.running = True
    def save(self):
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f: json.dump(self.state, f, indent=2)
        os.replace(tmp, self.cfg.state_file)
    def notify(self, text): log.info(text.replace("\n", " | ")); self.notifier.send(text)
    def process(self, symbol):
        st = self.state["symbols"][symbol]; now = time.time()
        if st.get("pending_order"):
            return  # ambiguous order outcome: freeze; operator must reconcile before resuming
        if st["status"] == "cooldown":
            if now < st["cooldown_until"]: return
            st["status"] = "idle"; st["cooldown_until"] = 0; self.save()
        if st["status"] == "in_position": self.check_exit(symbol, st, now)
        else: self.scan_entry(symbol, st, now)
    def check_exit(self, symbol, st, now):
        pos = st["position"]
        if pos.get("oco_pending"): return
        if pos.get("oco"):
            status, result = self.broker.poll_oco(symbol, pos["oco"])
            if status == "active": return
            if status == "gone":
                pos["oco"] = None; self.save()
                self.notify(f"⚠️ OCO {symbol} dibatalkan/berakhir tanpa fill; pemantauan lokal aktif")
                return
            old_qty, old_cost = float(pos["qty"]), float(pos["cost"])
            sold = min(float(result["qty"]), old_qty)
            if status == "partial" or sold < old_qty * (1 - 1e-8):
                remaining = max(0.0, old_qty - sold)
                pos["qty"] = remaining
                pos["cost"] = old_cost * remaining / old_qty if old_qty else 0.0
                pos["oco"] = None
                self.save()
                self.notify(f"⚠️ {symbol}: OCO fill parsial {sold:g}; sisa {remaining:g} dipantau")
                return
            self.record_exit(symbol, st, result["leg"], result, now); return
        price = float(self.ex.fetch_ticker(symbol)["last"])
        if price <= pos["sl"]: reason = "SL"
        elif price >= pos["tp"]: reason = "TP"
        else:
            if not self.cfg.dry_run and self.cfg.use_oco and now >= pos.get("oco_retry_at", 0): self.arm_oco(symbol, st, pos, now)
            return
        self.exit_market(symbol, st, reason, price, now)
    def arm_oco(self, symbol, st, pos, now):
        # Persist intent before network I/O; after timeout never submit a duplicate blindly.
        client_id = pos.get("oco_client_id") or ("rsi-oco-" + uuid.uuid4().hex[:24])
        pos["oco_client_id"] = client_id; pos["oco_pending"] = True; self.save()
        try: oco = self.broker.place_oco(symbol, pos["qty"], pos["tp"], pos["sl"], client_id)
        except Exception as exc:
            pos["oco_error"] = type(exc).__name__; self.save()
            self.notify(f"🚨 OCO {symbol} belum dapat dipastikan. Tidak ada retry otomatis untuk mencegah OCO duplikat; cek Binance dan state.")
            return False
        pos["oco"] = oco; pos["oco_pending"] = False; pos.pop("oco_error", None); self.save()
        return True
    def exit_market(self, symbol, st, reason, price, now):
        pos = st["position"]; client_id = "rsi-s-" + uuid.uuid4().hex[:24]
        st["pending_order"] = {"type": "sell", "client_id": client_id, "reason": reason, "created_at": now}
        self.save()
        try: result = self.broker.sell(symbol, pos["qty"], price, client_id)
        except Exception:
            self.save(); raise  # do not retry an ambiguous market sell automatically
        if not result:
            st["pending_order"] = None; self.save()
            self.notify(f"🚨 {symbol}: sell tidak terkirim/qty di bawah minimum; posisi tetap perlu dicek")
            return
        st["pending_order"] = None
        old_qty, old_cost = float(pos["qty"]), float(pos["cost"])
        sold = min(float(result["qty"]), old_qty)
        if sold < old_qty * (1 - 1e-8):
            remainder = old_qty - sold
            pos["qty"] = remainder; pos["cost"] = old_cost * remainder / old_qty
            pos["oco"] = None; self.save()
            self.notify(f"⚠️ {symbol}: market sell parsial; sisa posisi {remainder:g} tetap tercatat")
            return
        self.record_exit(symbol, st, reason, result, now)
    def record_exit(self, symbol, st, reason, result, now):
        pos = st["position"]; sold = min(float(result["qty"]), float(pos["qty"]))
        cost_part = float(pos["cost"]) * sold / float(pos["qty"]) if pos["qty"] else 0
        pnl = float(result["proceeds"]) - cost_part; pct = pnl / cost_part * 100 if cost_part else 0
        st["position"] = None; st["last_trade_ts"] = now; st["need_reset"] = True
        if reason == "SL": st["status"] = "cooldown"; st["cooldown_until"] = now + self.cfg.cooldown * 60
        else: st["status"] = "idle"
        self.save(); self.notify(f"{reason} {symbol}: exit {result['price']:g}, PnL {pnl:+.2f} {self.cfg.quote} ({pct:+.2f}%)")
    def scan_entry(self, symbol, st, now):
        now_ms = int(now * 1000); expected = now_ms // TF_MS["15m"] * TF_MS["15m"] - TF_MS["15m"]
        if st["last_eval_ts"] >= expected: return
        candles = closed(self.ex.fetch_ohlcv(symbol, "15m", limit=300), TF_MS["15m"], now_ms)
        if len(candles) < self.cfg.period + self.cfg.arm + 2 or candles[-1][0] <= st["last_eval_ts"]: return
        st["last_eval_ts"] = candles[-1][0]; self.save()
        values = rsi_series([c[4] for c in candles], self.cfg.period); prev, last = values[-2:]
        if prev is None or last is None: return
        if st["need_reset"]:
            after = st["last_trade_ts"] * 1000
            if any(c[0] >= after and v is not None and v >= self.cfg.rearm for c, v in zip(candles, values)): st["need_reset"] = False
        look = [v for v in values[-1-self.cfg.arm:-1] if v is not None]
        if st["need_reset"] or not (prev <= self.cfg.trigger < last and look and min(look) < self.cfg.oversold): return
        if now - st["last_trade_ts"] < self.cfg.lockout * 60: return
        st["entries"] = [t for t in st["entries"] if now - t < self.cfg.window_hours * 3600]
        if len(st["entries"]) >= self.cfg.max_entries: return
        if sum(s["status"] == "in_position" for s in self.state["symbols"].values()) >= self.cfg.max_open: return
        c4raw = self.ex.fetch_ohlcv(symbol, "4h", limit=300); c1raw = self.ex.fetch_ohlcv(symbol, "1h", limit=300)
        c4 = closed(c4raw, TF_MS["4h"], now_ms) if self.cfg.context_closed else c4raw
        c1 = closed(c1raw, TF_MS["1h"], now_ms) if self.cfg.context_closed else c1raw
        r4, r1 = rsi_series([c[4] for c in c4], self.cfg.period), rsi_series([c[4] for c in c1], self.cfg.period)
        if not r4 or not r1 or r4[-1] is None or r1[-1] is None or not (r4[-1] < self.cfg.max4 and r1[-1] < self.cfg.max1): return
        atr = atr_pct(closed(c1raw, TF_MS["1h"], now_ms)); tp_pct = round(min(self.cfg.tp_max, max(self.cfg.tp_min, (atr or 0) * self.cfg.tp_atr)), 2)
        price = float(self.ex.fetch_ticker(symbol)["last"]); free = self.broker.quote_free(); equity = free
        for pair, state in self.state["symbols"].items():
            if state.get("position"): equity += float(state["position"]["qty"]) * float(self.ex.fetch_ticker(pair)["last"])
        cost = equity * self.cfg.position_pct / 100
        market = self.ex.market(symbol); minimum = ((market.get("limits") or {}).get("cost") or {}).get("min") or 0
        if cost > free or (minimum and cost < float(minimum)): return
        client_id = "rsi-b-" + uuid.uuid4().hex[:24]
        st["pending_order"] = {"type": "buy", "client_id": client_id, "cost": cost, "created_at": now}
        self.save()
        result = self.broker.buy(symbol, cost, price, client_id)
        pos = {"qty": result["qty"], "entry": result["price"], "cost": result["cost"],
            "sl": result["price"] * (1 - self.cfg.sl_pct / 100), "tp": result["price"] * (1 + tp_pct / 100),
            "tp_pct": tp_pct, "opened_at": now, "oco": None, "buy_order_id": result["id"]}
        st["pending_order"] = None; st.update(status="in_position", position=pos, last_trade_ts=now)
        st["entries"].append(now); self.save()
        if not self.cfg.dry_run and self.cfg.use_oco: self.arm_oco(symbol, st, pos, now)
        self.notify(f"BUY {symbol}{' [DRY RUN]' if self.cfg.dry_run else ''} @ {result['price']:g}; TP {pos['tp']:g}, SL {pos['sl']:g}")
    def run(self):
        self.notify("Bot dimulai" + (" (DRY RUN)" if self.cfg.dry_run else " (LIVE)"))
        while self.running:
            for symbol in self.cfg.symbols:
                if not self.running: break
                try: self.process(symbol)
                except Exception as exc:
                    log.exception("%s: error", symbol); self.notifier.send(f"Error {symbol}: {type(exc).__name__}; order/state perlu dicek bila order sempat dikirim")
            self.save(); time.sleep(self.cfg.loop_seconds)
        self.save(); self.notify("Bot dihentikan")


def new_state(path, cfg):
    state = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f: state = json.load(f)
    mode = "dry" if cfg.dry_run else ("testnet" if cfg.testnet else "live")
    previous = state.get("bot_mode")
    if previous and previous != mode and not cfg.dry_run:
        raise RuntimeError(f"State mode {previous} tidak cocok dengan {mode}; jangan reset otomatis, rekonsiliasi manual diperlukan")
    state["bot_mode"] = mode; state.setdefault("paper_quote", cfg.dry_balance); state.setdefault("symbols", {})
    for sym in cfg.symbols:
        base = new_symbol_state(); base.update(state["symbols"].get(sym, {})); state["symbols"][sym] = base
    return state


def resolve(ex, symbols, quote):
    out = []
    for raw in symbols:
        key = raw.upper().replace("-", "/").replace("_", "/")
        sym = key if key in ex.markets else None
        if not sym:
            for item in ex.markets_by_id.get(key.replace("/", ""), []):
                if item.get("spot"): sym = item["symbol"]; break
        if not sym or not ex.markets[sym].get("spot") or ex.markets[sym].get("active") is False:
            raise ValueError(f"Pair tidak aktif/tidak ditemukan: {raw}")
        if ex.markets[sym].get("quote") != quote: raise ValueError(f"{sym} bukan quote {quote}")
        if sym not in out: out.append(sym)
    return out


def main():
    load_dotenv()
    try: cfg = Config.from_env()
    except ValueError as exc: print(exc); return 2
    logging.basicConfig(level=logging.INFO, handlers=[logging.StreamHandler(), RotatingFileHandler(cfg.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")], format="%(asctime)s %(levelname)s %(message)s")
    ex = ccxt.binance({"apiKey": cfg.key or None, "secret": cfg.secret or None, "enableRateLimit": True, "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    if cfg.testnet: ex.set_sandbox_mode(True)
    ex.load_markets(); cfg.symbols = resolve(ex, cfg.symbols, cfg.quote)
    try: state = new_state(cfg.state_file, cfg)
    except Exception as exc: log.error("State perlu rekonsiliasi manual: %s", exc); return 2
    broker = PaperBroker(state, cfg.paper_fee) if cfg.dry_run else LiveBroker(ex, cfg.quote)
    bot = Bot(cfg, ex, broker, Notifier(cfg.tg_token, cfg.tg_chat), state)
    if not cfg.dry_run:
        try: broker.quote_free()
        except Exception as exc: log.error("Gagal membaca saldo: %s", type(exc).__name__); return 2
        for sym, st in state["symbols"].items():
            if st.get("pending_order") or (st.get("position") or {}).get("oco_pending"):
                bot.notify(f"🚨 {sym}: ada order dengan status belum pasti di state. Pair dibekukan; rekonsiliasi manual dengan Binance sebelum menghapus intent.")
    signal.signal(signal.SIGINT, lambda *_: setattr(bot, "running", False))
    signal.signal(signal.SIGTERM, lambda *_: setattr(bot, "running", False))
    bot.run(); return 0


if __name__ == "__main__": sys.exit(main())
