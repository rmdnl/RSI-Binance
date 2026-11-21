#!/usr/bin/env python3
"""RSI spot bot with live-safety reconciliation (see state.json)."""
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


def env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or not value.strip() else value.strip()


def finite_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(env(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} harus berupa angka")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} harus antara {minimum:g} dan {maximum:g}")
    return value


def finite_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(float(env(name, str(default))))
    except ValueError:
        raise ValueError(f"{name} harus berupa bilangan bulat")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} harus antara {minimum} dan {maximum}")
    return value


def boolean(name: str, default: bool) -> bool:
    raw = env(name, "true" if default else "false").lower()
    if raw not in ("1", "true", "yes", "y", "on", "0", "false", "no", "n", "off"):
        raise ValueError(f"{name} harus true/false")
    return raw in ("1", "true", "yes", "y", "on")


@dataclass
class Config:
    key: str
    secret: str
    testnet: bool
    dry_run: bool
    quote: str
    symbols: list[str]
    rsi_period: int
    rsi_oversold: float
    rsi_trigger: float
    rsi_4h_max: float
    rsi_1h_max: float
    arm: int
    rearm: float
    context_closed: bool
    position_pct: float
    max_open: int
    tp_min: float
    tp_max: float
    tp_atr: float
    sl_pct: float
    use_oco: bool
    cooldown: float
    lockout: float
    max_entries: int
    window_hours: float
    loop_seconds: float
    dry_balance: float
    paper_fee: float
    tg_token: str
    tg_chat: str
    state_file: str
    log_file: str

    @classmethod
    def from_env(cls) -> "Config":
        symbols = [x for x in re.split(r"[,\s;]+", env("SYMBOLS")) if x]
        cfg = cls(
            env("BINANCE_API_KEY"), env("BINANCE_API_SECRET"),
            boolean("BINANCE_TESTNET", False), boolean("DRY_RUN", True),
            env("QUOTE_ASSET", "USDT").upper(), symbols,
            finite_int("RSI_PERIOD", 14, 2, 200),
            finite_float("RSI_OVERSOLD", 30, 0, 100),
            finite_float("RSI_TRIGGER", 32, 0, 100),
            finite_float("RSI_4H_MAX", 35, 0, 100),
            finite_float("RSI_1H_MAX", 35, 0, 100),
            finite_int("ARM_LOOKBACK", 12, 1, 500),
            finite_float("REARM_LEVEL", 50, 0, 100),
            boolean("CONTEXT_USE_CLOSED", False),
            finite_float("POSITION_PCT", 30, 0.0001, 100),
            finite_int("MAX_OPEN_POSITIONS", 3, 1, 100),
            finite_float("TP_MIN_PCT", 1, 0.0001, 100),
            finite_float("TP_MAX_PCT", 3, 0.0001, 100),
            finite_float("TP_ATR_MULT", 1.5, 0, 100),
            finite_float("SL_PCT", 2, 0.0001, 100),
            boolean("USE_OCO", True),
            finite_float("COOLDOWN_MINUTES", 240, 0, 525600),
            finite_float("REENTRY_LOCKOUT_MINUTES", 120, 0, 525600),
            finite_int("MAX_ENTRIES_PER_WINDOW", 2, 1, 1000),
            finite_float("ENTRY_WINDOW_HOURS", 24, 0.0001, 876000),
            finite_float("LOOP_SECONDS", 10, 1, 3600),
            finite_float("DRY_RUN_BALANCE", 1000, 0.0001, 1e15),
            finite_float("PAPER_FEE_PCT", 0.1, 0, 100),
            env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"),
            env("STATE_FILE", "state.json"), env("LOG_FILE", "bot.log"),
        )
        errors = []
        if not symbols:
            errors.append("SYMBOLS kosong")
        if not cfg.dry_run and not (cfg.key and cfg.secret):
            errors.append("live trading butuh BINANCE_API_KEY dan BINANCE_API_SECRET")
        if cfg.rsi_trigger <= cfg.rsi_oversold:
            errors.append("RSI_TRIGGER harus lebih besar dari RSI_OVERSOLD")
        if cfg.tp_max < cfg.tp_min:
            errors.append("TP_MAX_PCT harus >= TP_MIN_PCT")
        if errors:
            raise ValueError("Konfigurasi tidak valid:\n  - " + "\n  - ".join(errors))
        return cfg


def rsi(closes: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return result
    gain = loss = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gain += max(delta, 0.0)
        loss += max(-delta, 0.0)
    avg_gain, avg_loss = gain / period, loss / period
    result[period] = 50.0 if avg_loss == 0 and avg_gain == 0 else (100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        result[i] = 50.0 if avg_loss == 0 and avg_gain == 0 else (100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss))
    return result


def closed(candles: list[list], tf_ms: int, now_ms: int) -> list[list]:
    return [c for c in candles if int(c[0]) + tf_ms <= now_ms]


def atr_pct(candles: list[list], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4])) for p, c in zip(candles, candles[1:])]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr / candles[-1][4] * 100 if candles[-1][4] > 0 else None


class Notifier:
    def __init__(self, token: str, chat: str):
        self.token, self.chat = token, chat

    def send(self, text: str) -> None:
        if not (self.token and self.chat):
            return
        try:
            import requests
            r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage",
                              json={"chat_id": self.chat, "text": text, "disable_web_page_preview": True}, timeout=10)
            if not r.ok:
                log.warning("Telegram gagal: HTTP %s", r.status_code)
        except Exception:
            log.warning("Telegram gagal", exc_info=False)


class PaperBroker:
    def __init__(self, state: dict, fee_pct: float):
        self.state, self.fee = state, fee_pct / 100

    def quote_free(self):
        return float(self.state["paper_quote"])

    def base_free(self, symbol: str):
        pos = self.state["symbols"].get(symbol, {}).get("position")
        return float(pos["qty"] if pos else 0)

    def buy(self, symbol: str, cost: float, price: float) -> dict:
        qty = cost / price * (1 - self.fee)
        self.state["paper_quote"] -= cost
        return {"id": "paper-buy", "qty": qty, "price": price, "cost": cost,
                "status": "closed", "filled": qty}

    def sell(self, symbol: str, qty: float, price: float) -> dict:
        proceeds = qty * price * (1 - self.fee)
        self.state["paper_quote"] += proceeds
        return {"qty": qty, "price": price, "proceeds": proceeds, "status": "closed"}


class LiveBroker:
    def __init__(self, ex: Any, quote: str):
        self.ex, self.quote = ex, quote

    def quote_free(self) -> float:
        return float((self.ex.fetch_balance().get("free") or {}).get(self.quote) or 0)

    def base_free(self, symbol: str) -> float:
        base = symbol.split("/")[0]
        return float((self.ex.fetch_balance().get("free") or {}).get(base) or 0)

    def buy(self, symbol: str, cost: float, price: float) -> dict:
        # ccxt Binance does not send duplicate market buys if clientOrderId is reused.
        cost = float(self.ex.cost_to_precision(symbol, cost))
        client_id = "rsi-b-" + uuid.uuid4().hex[:24]
        order = self.ex.create_market_buy_order_with_cost(symbol, cost, {"newClientOrderId": client_id})
        if order.get("average") is None or order.get("filled") is None:
            order = self.ex.fetch_order(order["id"], symbol)
        filled = float(order.get("filled") or 0)
        status = str(order.get("status") or "").lower()
        if filled <= 0:
            raise RuntimeError(f"market buy {symbol} tidak terisi: id={order.get('id')} status={status}")
        net = filled
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        base = symbol.split("/")[0]
        for fee in fees:
            if fee and fee.get("currency") == base:
                net -= float(fee.get("cost") or 0)
        average = float(order.get("average") or price)
        return {"id": str(order["id"]), "client_id": client_id, "qty": net,
                "price": average, "cost": float(order.get("cost") or filled * average),
                "status": status, "filled": filled}

    def sell(self, symbol: str, qty: float, price: float) -> Optional[dict]:
        free = self.base_free(symbol)
        qty = min(qty, free)
        qty = float(self.ex.amount_to_precision(symbol, qty))
        min_amount = ((self.ex.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0
        if qty <= 0 or qty < float(min_amount):
            return None
        client_id = "rsi-s-" + uuid.uuid4().hex[:24]
        order = self.ex.create_market_sell_order(symbol, qty, {"newClientOrderId": client_id})
        if order.get("average") is None or order.get("filled") is None:
            order = self.ex.fetch_order(order["id"], symbol)
        filled = float(order.get("filled") or qty)
        average = float(order.get("average") or price)
        proceeds = float(order.get("cost") or filled * average)
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            if fee and fee.get("currency") == self.quote:
                proceeds -= float(fee.get("cost") or 0)
        return {"id": str(order["id"]), "qty": filled, "price": average,
                "proceeds": proceeds, "status": str(order.get("status") or "")}

    def place_oco(self, symbol: str, qty: float, tp: float, sl: float) -> dict:
        market = self.ex.market(symbol)
        info = market.get("info") or {}
        if info.get("ocoAllowed") is False:
            raise RuntimeError("pair tidak mendukung OCO")
        qty = float(self.ex.amount_to_precision(symbol, min(qty, self.base_free(symbol))))
        if qty <= 0:
            raise RuntimeError("qty OCO nol atau melebihi saldo bebas")
        list_id = "rsi-oco-" + uuid.uuid4().hex
        params = {"symbol": self.ex.market_id(symbol), "side": "SELL", "quantity": qty,
                  "aboveType": "LIMIT_MAKER", "abovePrice": self.ex.price_to_precision(symbol, tp),
                  "belowType": "STOP_LOSS", "belowStopPrice": self.ex.price_to_precision(symbol, sl),
                  "listClientOrderId": list_id[:36]}
        result = self.ex.request("orderList/oco", "private", "POST", params)
        report = result.get("orderReports") or []
        tp_id = sl_id = None
        for leg in report:
            type_name = str(leg.get("type") or "").upper()
            if type_name.startswith("STOP_LOSS"):
                sl_id = str(leg["orderId"])
            else:
                tp_id = str(leg["orderId"])
        if not (tp_id and sl_id):
            raise RuntimeError("respons OCO tidak lengkap")
        return {"list_id": str(result.get("orderListId") or list_id),
                "client_list_id": list_id[:36], "tp_id": tp_id, "sl_id": sl_id}

    def poll_oco(self, symbol: str, oco: dict) -> tuple[str, Optional[dict]]:
        open_orders = {str(o["id"]): o for o in self.ex.fetch_open_orders(symbol)}
        ids = {str(oco["tp_id"]), str(oco["sl_id"])}
        if ids & set(open_orders):
            return "active", None
        for leg, order_id in (("SL", oco["sl_id"]), ("TP", oco["tp_id"])):
            order = self.ex.fetch_order(str(order_id), symbol)
            status = str(order.get("status") or "").lower()
            filled = float(order.get("filled") or 0)
            orig = float(order.get("amount") or order.get("origQty") or 0)
            if status in ("closed", "filled") or (filled > 0 and orig > 0 and filled >= orig):
                price = float(order.get("average") or order.get("price") or 0)
                proceeds = float(order.get("cost") or filled * price)
                fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
                for fee in fees:
                    if fee and fee.get("currency") == self.quote:
                        proceeds -= float(fee.get("cost") or 0)
                return "filled", {"leg": leg, "qty": filled, "price": price, "proceeds": proceeds}
        # tidak lagi terbuka, tidak ada leg yang lengkap: status unknown/gone
        return "unknown", None


def new_symbol_state() -> dict:
    return {"status": "idle", "cooldown_until": 0.0, "last_eval_ts": 0,
            "need_reset": False, "last_trade_ts": 0.0, "entries": [], "position": None}


class Bot:
    def __init__(self, cfg: Config, ex: Any, broker: Any, notifier: Notifier, state: dict):
        self.cfg, self.ex, self.broker, self.notifier, self.state = cfg, ex, broker, notifier, state
        self.running = True

    def save(self) -> None:
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.cfg.state_file)

    def notify(self, text: str) -> None:
        log.info(text.replace("\n", " | "))
        self.notifier.send(text)

    def reconcile_startup(self) -> None:
        """No longer infer ownership from a random OCO or reset positions by 50%.
        Live state is reconciled by exact base balance and stored list IDs.
        """
        if self.cfg.dry_run:
            return
        for symbol, st in self.state["symbols"].items():
            pos = st.get("position")
            if not pos:
                continue
            free = self.broker.base_free(symbol)
            qty = float(pos["qty"])
            tolerance = max(qty * 0.001, 1e-12)
            if pos.get("oco"):
                # Do not adopt or attach an unowned OCO. It must retain our stored list ID.
                status, _ = self.broker.poll_oco(symbol, pos["oco"])
                if status == "unknown":
                    pos["oco"] = None
                    pos["oco_retry_at"] = time.time()
                    self.save()
                    self.notify(f"⚠️ {symbol}: OCO tersimpan tidak lagi aktif; bot mencoba melampirkan ulang dengan client ID miliknya sendiri.")
            elif abs(free - qty) <= tolerance:
                continue
            elif free <= tolerance:
                st.update(status="idle", position=None, need_reset=True, last_trade_ts=time.time())
                self.save()
                self.notify(f"⚠️ {symbol}: saldo koin tidak ada, posisi state direset idle.")
            else:
                # partial manual exit: adjust tracked quantity and cost proportionally
                sold = max(0.0, qty - free)
                ratio = free / qty
                pos["qty"] = free
                pos["cost"] *= ratio
                pos["partial_manual_adjustment"] = sold
                self.save()
                self.notify(f"⚠️ {symbol}: penjualan parsial/manual terdeteksi; posisi state disesuaikan ke {free:g}.")

    def process(self, symbol: str) -> None:
        st = self.state["symbols"].get(symbol)
        now = time.time()
        if st["status"] == "cooldown":
            if now >= st["cooldown_until"]:
                st["status"] = "idle"
                st["cooldown_until"] = 0
                self.save()
                self.notify(f"Cooldown {symbol} selesai")
            else:
                return
        if st["status"] == "in_position":
            self.check_exit(symbol, st, now)
        else:
            self.scan_entry(symbol, st, now)

    def check_exit(self, symbol: str, st: dict, now: float) -> None:
        pos = st["position"]
        if pos.get("oco"):
            status, result = self.broker.poll_oco(symbol, pos["oco"])
            if status == "active":
                return
            if status == "filled" and result:
                sold = min(float(result["qty"]), float(pos["qty"]))
                remaining = float(pos["qty"]) - sold
                if remaining > max(pos["qty"] * 0.001, 1e-12):
                    pos["qty"] = remaining
                    pos["cost"] *= remaining / float(pos["qty"] + sold)
                    pos["oco"] = None
                    pos["oco_retry_at"] = now
                    self.save()
                    self.notify(f"⚠️ {symbol}: OCO terisi sebagian ({sold:g}); sisa posisi akan dipantau.")
                    return
                self.record_exit(symbol, st, result["leg"], result, now)
            elif status == "unknown":
                pos["oco"] = None
                pos["oco_retry_at"] = now
                self.save()
                self.notify(f"⚠️ {symbol}: status OCO tidak dapat dipastikan; TP/SL diambil alih pemantauan bot.")
            return
        price = float(self.ex.fetch_ticker(symbol)["last"])
        if price <= pos["sl"]:
            reason = "SL"
        elif price >= pos["tp"]:
            reason = "TP"
        else:
            if now >= pos.get("oco_retry_at", 0) and not self.cfg.dry_run and self.cfg.use_oco:
                self.arm_oco(symbol, st, pos, now)
            return
        self.exit_market(symbol, st, reason, price, now)

    def arm_oco(self, symbol: str, st: dict, pos: dict, now: float) -> bool:
        if self.cfg.dry_run or not self.cfg.use_oco:
            return False
        try:
            oco = self.broker.place_oco(symbol, pos["qty"], pos["tp"], pos["sl"])
        except Exception as exc:
            pos["oco"] = None
            pos["oco_retry_at"] = now + 300
            self.save()
            self.notify(f"⚠️ Gagal memasang OCO {symbol}: {type(exc).__name__}: {str(exc)[:150]}")
            return False
        pos["oco"] = oco
        pos.pop("oco_retry_at", None)
        self.save()
        return True

    def record_exit(self, symbol: str, st: dict, reason: str, result: dict, now: float) -> None:
        pos = st["position"]
        sold = min(float(result["qty"]), float(pos["qty"]))
        cost_part = float(pos["cost"]) * sold / float(pos["qty"])
        pnl = float(result["proceeds"]) - cost_part
        pnl_pct = pnl / cost_part * 100 if cost_part else 0
        st["position"] = None
        st["last_trade_ts"] = now
        st["need_reset"] = True
        if reason == "SL":
            st["status"] = "cooldown"
            st["cooldown_until"] = now + self.cfg.cooldown * 60
        else:
            st["status"] = "idle"
        self.save()
        self.notify(f"{reason} {symbol}: exit {result['price']:g}, PnL {pnl:+.2f} {self.cfg.quote} ({pnl_pct:+.2f}%)")

    def exit_market(self, symbol: str, st: dict, reason: str, price: float, now: float) -> None:
        pos = st["position"]
        result = self.broker.sell(symbol, pos["qty"], price)
        if not result:
            st.update(status="idle", position=None, need_reset=True, last_trade_ts=now)
            self.save()
            self.notify(f"⚠️ {symbol}: saldo koin tidak ditemukan saat {reason}")
            return
        self.record_exit(symbol, st, reason, result, now)

    def scan_entry(self, symbol: str, st: dict, now: float) -> None:
        now_ms = int(now * 1000)
        expected_open = (now_ms // TF_MS["15m"]) * TF_MS["15m"] - TF_MS["15m"]
        if st["last_eval_ts"] >= expected_open:
            return
        candles15 = closed(self.ex.fetch_ohlcv(symbol, "15m", limit=300), TF_MS["15m"], now_ms)
        if len(candles15) < self.cfg.rsi_period + self.cfg.arm + 2:
            return
        if candles15[-1][0] <= st["last_eval_ts"]:
            return
        st["last_eval_ts"] = candles15[-1][0]
        r15 = rsi([c[4] for c in candles15], self.cfg.rsi_period)
        if st["need_reset"]:
            after = st["last_trade_ts"] * 1000
            for candle, value in zip(candles15, r15):
                if value is not None and candle[0] >= after and value >= self.cfg.rearm:
                    st["need_reset"] = False
                    break
        prev, last = r15[-2], r15[-1]
        if prev is None or last is None:
            return
        lookback = [v for v in r15[-1-self.cfg.arm:-1] if v is not None]
        crossed = prev <= self.cfg.rsi_trigger < last
        armed = bool(lookback) and min(lookback) < self.cfg.rsi_oversold
        if not (crossed and armed) or st["need_reset"]:
            return
        if now - st["last_trade_ts"] < self.cfg.lockout * 60:
            return
        window = self.cfg.window_hours * 3600
        st["entries"] = [t for t in st["entries"] if now - t < window]
        if len(st["entries"]) >= self.cfg.max_entries:
            return
        open_count = sum(1 for s in self.state["symbols"].values() if s["status"] == "in_position")
        if open_count >= self.cfg.max_open:
            return

        c4 = closed(self.ex.fetch_ohlcv(symbol, "4h", limit=300), TF_MS["4h"], now_ms) if self.cfg.context_closed else self.ex.fetch_ohlcv(symbol, "4h", limit=300)
        c1 = closed(self.ex.fetch_ohlcv(symbol, "1h", limit=300), TF_MS["1h"], now_ms) if self.cfg.context_closed else self.ex.fetch_ohlcv(symbol, "1h", limit=300)
        r4 = rsi([c[4] for c in c4], self.cfg.rsi_period)
        r1 = rsi([c[4] for c in c1], self.cfg.rsi_period)
        if not r4 or not r1 or r4[-1] is None or r1[-1] is None:
            return
        if not (r4[-1] < self.cfg.rsi_4h_max and r1[-1] < self.cfg.rsi_1h_max):
            return

        atr = atr_pct(closed(c1, TF_MS["1h"], now_ms))
        tp_pct = round(min(self.cfg.tp_max, max(self.cfg.tp_min, (atr or 0) * self.cfg.tp_atr)), 2)
        price = float(self.ex.fetch_ticker(symbol)["last"])
        quote_free = self.broker.quote_free()
        equity = quote_free
        for s, ss in self.state["symbols"].items():
            if ss["position"]:
                equity += ss["position"]["qty"] * float(self.ex.fetch_ticker(s)["last"])
        cost = equity * self.cfg.position_pct / 100
        if cost > quote_free:
            return
        market = self.ex.market(symbol)
        min_cost = ((market.get("limits") or {}).get("cost") or {}).get("min") or 0
        if min_cost and cost < float(min_cost):
            return

        result = self.broker.buy(symbol, cost, price)
        pos = {"qty": result["qty"], "entry": result["price"], "cost": result["cost"],
               "sl": result["price"] * (1 - self.cfg.sl_pct / 100),
               "tp": result["price"] * (1 + tp_pct / 100), "tp_pct": tp_pct,
               "opened_at": now, "oco": None, "buy_order_id": result.get("id")}
        st.update(status="in_position", position=pos, last_trade_ts=now)
        st["entries"].append(now)
        self.save()
        if self.arm_oco(symbol, st, pos, now):
            mode = "OCO"
        else:
            mode = "bot monitor"
        self.notify(f"BUY {symbol}{' [DRY RUN]' if self.cfg.dry_run else ''} | {result['price']:g} | {mode} | TP {pos['tp']:g} SL {pos['sl']:g}")

    def run(self) -> None:
        self.notify("Bot dimulai" + (" (DRY RUN)" if self.cfg.dry_run else " (LIVE)"))
        while self.running:
            for symbol in self.cfg.symbols:
                if not self.running:
                    break
                try:
                    self.process(symbol)
                except Exception as exc:
                    log.exception("%s: error", symbol)
                    self.notifier.send(f"Error {symbol}: {type(exc).__name__}: {str(exc)[:200]}")
            self.save()
            end = time.time() + self.cfg.loop_seconds
            while self.running and time.time() < end:
                time.sleep(0.5)
        self.save()
        self.notify("Bot dihentikan")


def setup(path: str) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.addHandler(RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))


def resolve(ex: Any, raw: list[str], quote: str) -> list[str]:
    out = []
    for key in raw:
        key = key.upper().replace("-", "/").replace("_", "/")
        symbol = key if key in ex.markets else None
        if not symbol:
            for market in ex.markets_by_id.get(key.replace("/", ""), []):
                if market.get("spot"):
                    symbol = market["symbol"]
                    break
        if not symbol:
            raise ValueError(f"Pair {key} tidak ditemukan")
        market = ex.markets[symbol]
        if not market.get("spot") or market.get("active") is False:
            raise ValueError(f"Pair {symbol} tidak aktif")
        if market.get("quote") != quote:
            raise ValueError(f"Pair {symbol} bukan quote {quote}")
        if symbol not in out:
            out.append(symbol)
    return out


def load_state(path: str, cfg: Config) -> dict:
    state = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            os.replace(path, path + ".bak")
            state = {}
    mode = "dry" if cfg.dry_run else ("live-testnet" if cfg.testnet else "live")
    old_mode = state.get("bot_mode")
    if old_mode and old_mode != mode:
        log.warning("State berasal dari mode %s, sekarang %s; state posisi tidak akan dipakai untuk live live", old_mode, mode)
        if not cfg.dry_run:
            state["symbols"] = {}
    state["bot_mode"] = mode
    state.setdefault("paper_quote", cfg.dry_balance)
    state.setdefault("symbols", {})
    for symbol in cfg.symbols:
        base = new_symbol_state()
        base.update(state["symbols"].get(symbol, {}))
        state["symbols"][symbol] = base
    return state


def main() -> int:
    load_dotenv()
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        print(exc)
        return 2
    setup(cfg.log_file)
    ex = ccxt.binance({"apiKey": cfg.key or None, "secret": cfg.secret or None,
                       "enableRateLimit": True,
                       "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    if cfg.testnet:
        ex.set_sandbox_mode(True)
    ex.load_markets()
    try:
        cfg.symbols = resolve(ex, cfg.symbols, cfg.quote)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    state = load_state(cfg.state_file, cfg)
    broker = PaperBroker(state, cfg.paper_fee) if cfg.dry_run else LiveBroker(ex, cfg.quote)
    notifier = Notifier(cfg.tg_token, cfg.tg_chat)
    bot = Bot(cfg, ex, broker, notifier, state)
    if not cfg.dry_run:
        try:
            log.info("Saldo %s bebas: %.8f", cfg.quote, broker.quote_free())
        except Exception as exc:
            log.error("Gagal membaca saldo: %s", type(exc).__name__)
            return 2
        bot.reconcile_startup()
    def stop(signum, frame):
        bot.running = False
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
