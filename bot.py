#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Binance spot RSI bot with conservative P0 safety controls."""
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
from filelock import FileLock

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
    try:
        value = int(env(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} harus bilangan bulat") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} harus antara {minimum} dan {maximum}")
    return value


def boolean(name: str, default: bool) -> bool:
    raw = env(name, "true" if default else "false").lower()
    allowed = {"1", "true", "yes", "y", "on", "0", "false", "no", "n", "off"}
    if raw not in allowed:
        raise ValueError(f"{name} harus true/false")
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass
class Config:
    key: str
    secret: str
    testnet: bool
    dry_run: bool
    quote: str
    symbols: list[str]
    period: int
    oversold: float
    trigger: float
    max4: float
    max1: float
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
            env("BINANCE_API_KEY"), env("BINANCE_API_SECRET"), boolean("BINANCE_TESTNET", False), boolean("DRY_RUN", True),
            env("QUOTE_ASSET", "USDT").upper(), symbols,
            finite_int("RSI_PERIOD", 14, 2, 200), finite_float("RSI_OVERSOLD", 25, 0, 100),
            finite_float("RSI_TRIGGER", 32, 0, 100), finite_float("RSI_4H_MAX", 35, 0, 100),
            finite_float("RSI_1H_MAX", 35, 0, 100), finite_int("ARM_LOOKBACK", 12, 1, 500),
            finite_float("REARM_LEVEL", 50, 0, 100), boolean("CONTEXT_USE_CLOSED", False),
            finite_float("POSITION_PCT", 30, 0.0001, 100), finite_int("MAX_OPEN_POSITIONS", 3, 1, 100),
            finite_float("TP_MIN_PCT", 1, 0.0001, 100), finite_float("TP_MAX_PCT", 3, 0.0001, 100),
            finite_float("TP_ATR_MULT", 1.5, 0, 100), finite_float("SL_PCT", 2, 0.0001, 100),
            boolean("USE_OCO", True), finite_float("COOLDOWN_MINUTES", 240, 0, 525600),
            finite_float("REENTRY_LOCKOUT_MINUTES", 120, 0, 525600), finite_int("MAX_ENTRIES_PER_WINDOW", 2, 1, 1000),
            finite_float("ENTRY_WINDOW_HOURS", 24, 0.0001, 876000), finite_float("LOOP_SECONDS", 10, 1, 3600),
            finite_float("DRY_RUN_BALANCE", 1000, 0.0001, 1e15), finite_float("PAPER_FEE_PCT", 0.1, 0, 100),
            env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID"), env("STATE_FILE", "state.json"), env("LOG_FILE", "bot.log"),
        )
        errors = []
        if not symbols:
            errors.append("SYMBOLS kosong")
        if not cfg.dry_run and not (cfg.key and cfg.secret):
            errors.append("live mode perlu API key dan secret")
        if cfg.trigger <= cfg.oversold:
            errors.append("RSI_TRIGGER harus lebih besar dari RSI_OVERSOLD")
        if cfg.tp_max < cfg.tp_min:
            errors.append("TP_MAX_PCT harus >= TP_MIN_PCT")
        if errors:
            raise ValueError("Konfigurasi tidak valid:\n  - " + "\n  - ".join(errors))
        return cfg


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(closes: list[float], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gain += max(delta, 0.0)
        loss += max(-delta, 0.0)
    avg_gain, avg_loss = gain / period, loss / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def closed(candles: list[list], tf_ms: int, now_ms: int) -> list[list]:
    return [c for c in candles if int(c[0]) + tf_ms <= now_ms]


def aggregate_candles(candles: list[list], tf_ms: int) -> list[list]:
    """Gabungkan candle (mis. 15m) menjadi timeframe lebih besar yang selaras epoch.

    Bucket terakhir yang belum penuh ikut disertakan; pemanggil menyaring candle
    yang belum tertutup dengan `closed()` sehingga tidak ada data parsial terpakai.
    """
    out: list[list] = []
    for candle in candles:
        bucket = int(candle[0]) // tf_ms * tf_ms
        if out and out[-1][0] == bucket:
            row = out[-1]
            row[2] = max(row[2], float(candle[2]))
            row[3] = min(row[3], float(candle[3]))
            row[4] = float(candle[4])
            row[5] = float(row[5]) + float(candle[5])
        else:
            out.append([bucket, float(candle[1]), float(candle[2]), float(candle[3]), float(candle[4]), float(candle[5])])
    return out


def atr_pct(candles: list[list], period: int = 14, current_price: Optional[float] = None) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = [max(c[2] - c[3], abs(c[2] - p[4]), abs(c[3] - p[4])) for p, c in zip(candles, candles[1:])]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    ref_price = current_price if (current_price and current_price > 0) else candles[-1][4]
    return atr / ref_price * 100 if ref_price > 0 else None


def update_rsi_history(history: dict, timeframe: str, candles: list[list], period: int, now_ms: int) -> list:
    tf_ms = TF_MS[timeframe]
    cutoff = now_ms - 24 * 60 * 60 * 1000
    rows = {int(row["timestamp"]): float(row["value"])
            for row in history.get(timeframe, [])
            if row.get("timestamp") is not None and row.get("value") is not None
            and int(row["timestamp"]) >= cutoff}
    values = rsi_series([c[4] for c in candles], period)
    for candle, value in zip(candles, values):
        ts = int(candle[0])
        if ts >= cutoff and ts + tf_ms <= now_ms and value is not None and math.isfinite(float(value)):
            rows[ts] = float(value)
    history[timeframe] = [{"timestamp": ts, "value": rows[ts]} for ts in sorted(rows)][-100:]
    return history[timeframe]


class ProcessLock:
    def __init__(self, state_file: str):
        self.lock = FileLock(state_file + ".lock", timeout=0)

    def __enter__(self):
        try:
            self.lock.acquire()
        except Exception:
            raise RuntimeError(f"Bot sudah berjalan; lock aktif: {self.lock.lock_file}")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.lock.release()
        except Exception:
            pass


class Notifier:
    def __init__(self, token: str, chat: str):
        self.token, self.chat = token, chat

    def send(self, text: str) -> None:
        if not (self.token and self.chat):
            return
        try:
            import requests
            response = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
            if not response.ok:
                log.warning("Telegram gagal: HTTP %s %s",
                            response.status_code, str(getattr(response, "text", ""))[:200])
        except Exception as exc:
            log.warning("Telegram gagal: %s: %s", type(exc).__name__, str(exc)[:200])


class PaperBroker:
    def __init__(self, state: dict, fee_pct: float):
        self.state, self.fee = state, fee_pct / 100

    def quote_free(self):
        return float(self.state["paper_quote"])

    def base_free(self, symbol: str):
        pos = self.state["symbols"].get(symbol, {}).get("position")
        return float(pos["qty"] if pos else 0)

    def buy(self, symbol, cost, price, client_id):
        qty = cost / price * (1 - self.fee)
        self.state["paper_quote"] -= cost
        return {"id": client_id, "qty": qty, "price": price, "cost": cost, "status": "closed"}

    def sell(self, symbol, qty, price, client_id):
        proceeds = qty * price * (1 - self.fee)
        self.state["paper_quote"] += proceeds
        return {"id": client_id, "qty": qty, "price": price, "proceeds": proceeds, "status": "closed"}


class LiveBroker:
    def __init__(self, ex: Any, quote: str):
        self.ex, self.quote = ex, quote

    def fetch_balance(self):
        return self.ex.fetch_balance()

    def quote_free(self):
        balance = self.fetch_balance()
        return float((balance.get("free") or {}).get(self.quote) or 0)

    def base_free(self, symbol: str):
        base = symbol.split("/")[0]
        balance = self.fetch_balance()
        return float((balance.get("free") or {}).get(base) or 0)

    def buy(self, symbol, cost, price, client_id):
        cost = float(self.ex.cost_to_precision(symbol, cost))
        order = self.ex.create_market_buy_order_with_cost(symbol, cost, {"newClientOrderId": client_id})
        order = self._await_final_order(order, symbol)
        filled = float(order.get("filled") or 0)
        status = str(order.get("status") or "").lower()
        if filled <= 0 or status not in {"closed", "filled"}:
            raise RuntimeError(f"buy belum final; order={order.get('id')} status={status} filled={filled}")
        qty = filled
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            if fee and fee.get("currency") == symbol.split("/")[0]:
                qty -= float(fee.get("cost") or 0)
        avg = float(order.get("average") or price)
        return {"id": str(order["id"]), "qty": qty, "price": avg, "cost": float(order.get("cost") or filled * avg)}

    def _await_final_order(self, order: dict, symbol: str, attempts: int = 5, base_delay: float = 0.4) -> dict:
        """Poll fetch_order until terminal or attempts exhausted (fill propagation race)."""
        status = str((order or {}).get("status") or "").lower()
        filled = float((order or {}).get("filled") or 0)
        if order and order.get("average") is not None and filled > 0 and status in TERMINAL:
            return order
        oid = (order or {}).get("id")
        if oid is None:
            return order
        delay = base_delay
        for _ in range(attempts):
            time.sleep(delay)
            delay = min(delay * 1.6, 3.0)
            try:
                order = self.ex.fetch_order(str(oid), symbol)
            except Exception:
                continue
            status = str(order.get("status") or "").lower()
            if status in TERMINAL:
                return order
        return order

    def sell(self, symbol, qty, price, client_id):
        qty = float(self.ex.amount_to_precision(symbol, min(qty, self.base_free(symbol))))
        minimum = ((self.ex.market(symbol).get("limits") or {}).get("amount") or {}).get("min") or 0
        if qty <= 0 or qty < float(minimum):
            return None
        order = self.ex.create_market_sell_order(symbol, qty, {"newClientOrderId": client_id})
        order = self._await_final_order(order, symbol)
        status = str(order.get("status") or "").lower()
        if status not in TERMINAL:
            raise RuntimeError(f"sell belum final: order={order.get('id')} status={status or 'unknown'}")
        filled = float(order.get("filled") or 0)
        avg = float(order.get("average") or price)
        proceeds = float(order.get("cost") or filled * avg)
        for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
            if fee and fee.get("currency") == self.quote:
                proceeds -= float(fee.get("cost") or 0)
        return {"id": str(order["id"]), "qty": filled, "price": avg, "proceeds": proceeds, "status": status}

    def place_oco(self, symbol, qty, tp, sl, client_id):
        qty = float(self.ex.amount_to_precision(symbol, min(qty, self.base_free(symbol))))
        if qty <= 0:
            raise RuntimeError("qty OCO nol")
        params = {
            "symbol": self.ex.market_id(symbol), "side": "SELL", "quantity": qty,
            "aboveType": "LIMIT_MAKER", "abovePrice": self.ex.price_to_precision(symbol, tp),
            "belowType": "STOP_LOSS", "belowStopPrice": self.ex.price_to_precision(symbol, sl),
            "listClientOrderId": client_id,
        }
        result = self.ex.private_post_orderlist_oco(params)
        tp_id = sl_id = None
        for leg in result.get("orderReports") or []:
            if str(leg.get("type", "")).upper().startswith("STOP_LOSS"):
                sl_id = str(leg["orderId"])
            else:
                tp_id = str(leg["orderId"])
        if not (tp_id and sl_id):
            raise RuntimeError("respons OCO tidak lengkap")
        return {"list_id": str(result.get("orderListId") or ""), "client_list_id": client_id,
                "tp_id": tp_id, "sl_id": sl_id}

    def poll_oco(self, symbol, oco):
        try:
            open_ids = {str(order["id"]) for order in self.ex.fetch_open_orders(symbol)}
            legs = []
            for name, order_id in (("TP", oco["tp_id"]), ("SL", oco["sl_id"])):
                if str(order_id) in open_ids:
                    return "active", None
                order = self.ex.fetch_order(str(order_id), symbol)
                status = str(order.get("status") or "").lower()
                filled = float(order.get("filled") or 0)
                amount = float(order.get("amount") or 0)
                if status not in TERMINAL:
                    return "active", None
                if filled > 0:
                    avg = float(order.get("average") or order.get("price") or 0)
                    proceeds = float(order.get("cost") or filled * avg)
                    for fee in order.get("fees") or ([order["fee"]] if order.get("fee") else []):
                        if fee and fee.get("currency") == self.quote:
                            proceeds -= float(fee.get("cost") or 0)
                    legs.append({"leg": name, "qty": filled, "price": avg, "proceeds": proceeds,
                                 "full": amount > 0 and filled >= amount * (1 - 1e-8)})
            if not legs:
                return "gone_confirmed", None
            leg = next((x for x in legs if x["full"]), legs[0])
            return ("filled" if leg["full"] else "partial"), leg
        except Exception:
            return "unknown", None


def new_symbol_state() -> dict:
    return {"status": "idle", "cooldown_until": 0.0, "last_eval_ts": 0,
            "need_reset": False, "last_trade_ts": 0.0, "entries": [],
            "position": None, "pending_order": None}


class Bot:
    def __init__(self, cfg, ex, broker, notifier, state):
        self.cfg, self.ex, self.broker, self.notifier, self.state = cfg, ex, broker, notifier, state
        self.running = True
        self.price_cache = {}
        self.last_oco_poll = {}
        self.last_portfolio_update = 0.0
        self.pause_until = 0.0
        self.rate_limit_events = 0

    def save(self):
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, allow_nan=False)
        os.replace(tmp, self.cfg.state_file)

    def event(self, level: str, message: str):
        clean = str(message)[:500]
        events = self.state.setdefault("events", [])
        events.append({"time": time.time(), "level": level.upper(), "message": clean})
        del events[:-300]
        getattr(log, level.lower(), log.info)(clean)

    def notify(self, text: str):
        self.event("INFO", text.replace("\n", " | "))
        self.notifier.send(text)

    def price(self, symbol: str, max_age: float = 8):
        now = time.time()
        cached = self.price_cache.get(symbol)
        if cached and now - cached[0] < max_age:
            return cached[1]
        raw = self.ex.fetch_ticker(symbol).get("last")
        value = float(raw) if raw is not None else 0.0
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"harga {symbol} tidak valid")
        self.price_cache[symbol] = (now, value)
        return value

    def reconcile_startup(self):
        """Conservative startup reconciliation. Never guesses on unknown OCO state."""
        if self.cfg.dry_run:
            self.reconcile_dry_run()
            return
        for symbol, st in self.state["symbols"].items():
            pos = st.get("position")
            if not pos:
                continue
            if st.get("pending_order") or pos.get("oco_pending"):
                st["reconciliation_required"] = True
                st["status"] = "reconciliation"
                self.event("ERROR", f"{symbol}: pending order/OCO membutuhkan rekonsiliasi manual")
                continue
            if pos.get("oco"):
                status, _ = self.broker.poll_oco(symbol, pos["oco"])
                if status == "unknown":
                    pos["oco_pending"] = True
                    pos["reconciliation_required"] = True
                    st["reconciliation_required"] = True
                    st["status"] = "reconciliation"
                    self.event("ERROR", f"{symbol}: status OCO tidak dapat dipastikan")
                continue
            try:
                tracked = float(pos["qty"])
                actual = float(self.broker.base_free(symbol))
                tolerance = max(tracked * 0.001, 1e-12)
                if actual < tolerance:
                    st["reconciliation_required"] = True
                    st["status"] = "reconciliation"
                    self.event("ERROR", f"{symbol}: saldo base tidak ditemukan; posisi perlu rekonsiliasi")
                elif abs(actual - tracked) > tolerance:
                    st["reconciliation_required"] = True
                    pos["qty"] = actual
                    pos["cost"] = float(pos["cost"]) * actual / tracked if tracked > 0 else float(pos["cost"])
                    self.event("WARNING", f"{symbol}: kuantitas posisi disesuaikan ke saldo bebas aktual {actual:g}")
            except Exception as exc:
                st["reconciliation_required"] = True
                st["status"] = "reconciliation"
                self.event("ERROR", f"{symbol}: saldo/startup reconciliation gagal: {type(exc).__name__}")
        self.save()

    def reconcile_dry_run(self):
        """Dry run tidak pernah membuat order exchange, jadi pending order yang tersisa pasti stale
        (mis. proses terbunuh setelah `save()` sebelum order dicatat selesai) dan aman dibersihkan."""
        changed = False
        for symbol, st in self.state["symbols"].items():
            if st.get("pending_order"):
                st["pending_order"] = None
                changed = True
                self.event("WARNING", f"{symbol}: pending order dry run tertinggal dibersihkan saat startup")
        if changed:
            self.save()

    def process(self, symbol: str):
        st = self.state["symbols"][symbol]
        now = time.time()
        if now < self.pause_until:
            return
        if st.get("reconciliation_required") or st.get("pending_order"):
            return
        pos = st.get("position") or {}
        if pos.get("oco_pending") or pos.get("reconciliation_required"):
            return
        if st["status"] == "cooldown":
            if now < st["cooldown_until"]:
                return
            st["status"] = "idle"
            st["cooldown_until"] = 0
            self.save()
        if st["status"] == "in_position":
            self.check_exit(symbol, st, now)
        else:
            self.scan_entry(symbol, st, now)

    def check_exit(self, symbol: str, st: dict, now: float):
        pos = st["position"]
        if pos.get("oco"):
            if now - self.last_oco_poll.get(symbol, 0) < 30:
                return
            self.last_oco_poll[symbol] = now
            status, result = self.broker.poll_oco(symbol, pos["oco"])
            if status == "active":
                return
            if status == "unknown":
                pos["reconciliation_required"] = True
                pos["oco_pending"] = True
                st["reconciliation_required"] = True
                st["status"] = "reconciliation"
                self.event("ERROR", f"OCO {symbol}: status tidak dapat dipastikan; perlu rekonsiliasi")
                self.save()
                return
            if status == "gone_confirmed":
                pos["oco"] = None
                pos["oco_pending"] = False
                pos["reconciliation_required"] = False
                self.event("WARNING", f"OCO {symbol} confirmed gone tanpa fill; pemantauan lokal aktif")
                self.save()
                return
            if status in ("filled", "partial") and result:
                old_qty = float(pos["qty"])
                sold = min(float(result["qty"]), old_qty)
                if status == "partial" or sold < old_qty * (1 - 1e-8):
                    # Fail-closed: leg OCO yang terisi sebagian dapat menyisakan kuantitas yang
                    # masih aktif di exchange, jadi referensi OCO tidak dilepas otomatis.
                    pos["oco_pending"] = True
                    pos["reconciliation_required"] = True
                    st["reconciliation_required"] = True
                    st["status"] = "reconciliation"
                    self.event("ERROR", f"{symbol}: OCO fill parsial {sold:g} dari {old_qty:g}; sisa "
                                        f"{max(0.0, old_qty - sold):g} mungkin masih aktif di exchange — "
                                        "rekonsiliasi manual sebelum pair dilanjutkan")
                    self.save()
                    return
                self.record_exit(symbol, st, result["leg"], result, now)
                return
        mark = self.price(symbol)
        if mark <= pos["sl"]:
            reason = "SL"
        elif mark >= pos["tp"]:
            reason = "TP"
        else:
            if not self.cfg.dry_run and self.cfg.use_oco and now >= pos.get("oco_retry_at", 0):
                self.arm_oco(symbol, st, pos)
            return
        self.exit_market(symbol, st, reason, mark, now)

    def arm_oco(self, symbol: str, st: dict, pos: dict):
        attempt = int(pos.get("oco_attempts", 0)) + 1
        pos["oco_attempts"] = attempt
        cid = "rsi-oco-" + uuid.uuid4().hex[:20]
        pos["oco_client_id"] = cid
        pos["oco_pending"] = True
        pos["reconciliation_required"] = True
        self.save()
        try:
            oco = self.broker.place_oco(symbol, pos["qty"], pos["tp"], pos["sl"], cid)
        except Exception as exc:
            pos["oco_error"] = type(exc).__name__
            st["reconciliation_required"] = True
            st["status"] = "reconciliation"
            self.event("ERROR", f"OCO {symbol} tidak dapat dipastikan ({type(exc).__name__}) attempt#{attempt}; rekonsiliasi manual")
            self.save()
            return False
        pos["oco"] = oco
        pos["oco_pending"] = False
        pos["reconciliation_required"] = False
        st["reconciliation_required"] = False
        pos.pop("oco_error", None)
        self.save()
        return True

    def record_exit(self, symbol: str, st: dict, reason: str, res: dict, now: float):
        pos = st["position"]
        old_qty = float(pos["qty"])
        sold = min(float(res["qty"]), old_qty)
        cost_part = float(pos["cost"]) * sold / old_qty if old_qty else 0.0
        pnl = float(res["proceeds"]) - cost_part
        pct = pnl / cost_part * 100 if cost_part else 0.0
        st["position"] = None
        st["last_trade_ts"] = now
        st["need_reset"] = True
        if reason == "SL":
            st["status"] = "cooldown"
            st["cooldown_until"] = now + self.cfg.cooldown * 60
        else:
            st["status"] = "idle"
        self.state["realized_pnl"] = float(self.state.get("realized_pnl", 0)) + pnl
        trades = self.state.setdefault("realized_trades", [])
        trades.append({"time": now, "symbol": symbol, "qty": sold, "cost": cost_part, "proceeds": res["proceeds"], "pnl": pnl, "pnl_pct": pct})
        del trades[:-500]
        self.save()
        self.notify(f"{reason} {symbol}: exit {res['price']:g}; PnL {pnl:+.2f} {self.cfg.quote} ({pct:+.2f}%)")

    def exit_market(self, symbol: str, st: dict, reason: str, price: float, now: float):
        pos = st["position"]
        client_id = "rsi-s-" + uuid.uuid4().hex[:24]
        st["pending_order"] = {"type": "sell", "client_id": client_id, "reason": reason, "created_at": now}
        self.save()
        try:
            result = self.broker.sell(symbol, pos["qty"], price, client_id)
        except Exception as exc:
            st["reconciliation_required"] = True
            st["status"] = "reconciliation"
            self.event("ERROR", f"Sell {symbol} ambiguous ({type(exc).__name__}); rekonsiliasi manual")
            self.save()
            raise
        if not result:
            st["pending_order"] = None
            st["reconciliation_required"] = True
            st["status"] = "reconciliation"
            self.save()
            self.event("ERROR", f"{symbol}: sell tidak terkirim/qty di bawah minimum")
            return
        st["pending_order"] = None
        old_qty = float(pos["qty"])
        sold = min(float(result["qty"]), old_qty)
        if sold < old_qty * (1 - 1e-8):
            remaining = old_qty - sold
            pos["qty"] = remaining
            pos["cost"] = float(pos["cost"]) * remaining / old_qty
            pos["oco"] = None
            self.save()
            self.event("WARNING", f"{symbol}: sell parsial; sisa {remaining:g}")
            return
        self.record_exit(symbol, st, reason, result, now)

    def context_rsi(self, c4raw: list[list], c1raw: list[list], now_ms: int):
        """Return the HTF RSI values used by entry logic, based on one configured context mode."""
        c4 = closed(c4raw, TF_MS["4h"], now_ms) if self.cfg.context_closed else c4raw
        c1 = closed(c1raw, TF_MS["1h"], now_ms) if self.cfg.context_closed else c1raw
        r4 = rsi_series([c[4] for c in c4], self.cfg.period)
        r1 = rsi_series([c[4] for c in c1], self.cfg.period)
        r4_value = r4[-1] if r4 and r4[-1] is not None else None
        r1_value = r1[-1] if r1 and r1[-1] is not None else None
        return r4_value, r1_value

    def scan_entry(self, symbol: str, st: dict, now: float):
        now_ms = int(now * 1000)
        expected_open = now_ms // TF_MS["15m"] * TF_MS["15m"] - TF_MS["15m"]
        if st["last_eval_ts"] >= expected_open:
            return
        candles = closed(self.ex.fetch_ohlcv(symbol, "15m", limit=500), TF_MS["15m"], now_ms)
        if len(candles) < self.cfg.period + self.cfg.arm + 2:
            return
        if candles[-1][0] <= st["last_eval_ts"]:
            return
        st["last_eval_ts"] = candles[-1][0]
        values = rsi_series([c[4] for c in candles], self.cfg.period)
        prev, last = values[-2], values[-1]

        # Histori + diagnostik dashboard. 1h/4h diagregasi dari candle 15m yang sudah
        # tertutup, jadi tidak menambah request API pada setiap evaluasi.
        history = st.setdefault("rsi_history", {})
        update_rsi_history(history, "15m", candles, self.cfg.period, now_ms)
        c1_closed = closed(aggregate_candles(candles, TF_MS["1h"]), TF_MS["1h"], now_ms)
        c4_closed = closed(aggregate_candles(candles, TF_MS["4h"]), TF_MS["4h"], now_ms)
        update_rsi_history(history, "1h", c1_closed, self.cfg.period, now_ms)
        update_rsi_history(history, "4h", c4_closed, self.cfg.period, now_ms)
        r1_chart = rsi_series([c[4] for c in c1_closed], self.cfg.period)
        r4_chart = rsi_series([c[4] for c in c4_closed], self.cfg.period)
        diag = {"evaluated_at": now, "candle_ts": candles[-1][0],
                "rsi_15m_previous": prev, "rsi_15m": last,
                "rsi_1h": r1_chart[-1] if r1_chart and r1_chart[-1] is not None else None,
                "rsi_4h": r4_chart[-1] if r4_chart and r4_chart[-1] is not None else None,
                "oversold_seen": False, "trigger_cross": False,
                "reason": "Menunggu evaluasi candle 15m"}
        st["diagnostics"] = diag

        def finish(reason: str):
            diag["reason"] = reason
            self.save()

        if prev is None or last is None:
            finish("Data candle 15m belum cukup untuk menghitung RSI")
            return

        if st["need_reset"]:
            if any(c[0] >= st["last_trade_ts"] * 1000 and value is not None and value >= self.cfg.rearm
                   for c, value in zip(candles, values)):
                st["need_reset"] = False

        look = [value for value in values[-1 - self.cfg.arm:-1] if value is not None]
        crossed = prev <= self.cfg.trigger < last
        armed = bool(look) and min(look) < self.cfg.oversold
        diag["oversold_seen"] = armed
        diag["trigger_cross"] = crossed
        if st["need_reset"]:
            finish(f"Menunggu RSI 15m re-arm ke {self.cfg.rearm:g}")
            return
        if not armed:
            finish(f"Belum ada RSI 15m di bawah oversold {self.cfg.oversold:g} pada lookback")
            return
        if not crossed:
            finish(f"Belum ada crossing RSI 15m ke atas trigger {self.cfg.trigger:g}")
            return
        if now - st["last_trade_ts"] < self.cfg.lockout * 60:
            finish("Menunggu re-entry lockout")
            return

        st["entries"] = [timestamp for timestamp in st["entries"] if now - timestamp < self.cfg.window_hours * 3600]
        if len(st["entries"]) >= self.cfg.max_entries:
            finish("Batas entry per window sudah tercapai")
            return
        if sum(x["status"] == "in_position" for x in self.state["symbols"].values()) >= self.cfg.max_open:
            finish("Batas posisi terbuka sudah tercapai")
            return

        c4raw = self.ex.fetch_ohlcv(symbol, "4h", limit=300)
        c1raw = self.ex.fetch_ohlcv(symbol, "1h", limit=300)
        r4, r1 = self.context_rsi(c4raw, c1raw, now_ms)
        if r4 is None or r1 is None:
            finish("Data candle belum cukup untuk RSI konteks 1h/4h")
            return
        diag["rsi_1h_context"] = r1
        diag["rsi_4h_context"] = r4
        if not (r4 < self.cfg.max4 and r1 < self.cfg.max1):
            finish(f"Filter konteks gagal: RSI 4h harus < {self.cfg.max4:g} dan RSI 1h harus < {self.cfg.max1:g}")
            return

        price = self.price(symbol)
        atr = atr_pct(closed(c1raw, TF_MS["1h"], now_ms), current_price=price)
        tp_pct = round(min(self.cfg.tp_max, max(self.cfg.tp_min, (atr or 0) * self.cfg.tp_atr)), 2)
        quote_free = self.broker.quote_free()
        equity = quote_free
        for pair, state in self.state["symbols"].items():
            if state.get("position"):
                equity += float(state["position"]["qty"]) * self.price(pair)
        cost = equity * self.cfg.position_pct / 100
        minimum = ((self.ex.market(symbol).get("limits") or {}).get("cost") or {}).get("min") or 0
        if cost > quote_free:
            finish("Saldo quote tidak cukup untuk ukuran posisi")
            return
        if minimum and cost < float(minimum):
            finish("Nilai order di bawah minimum exchange")
            return

        client_id = "rsi-b-" + uuid.uuid4().hex[:24]
        st["pending_order"] = {"type": "buy", "client_id": client_id, "cost": cost, "created_at": now}
        diag["reason"] = "Kondisi entry lolos; order BUY sedang diproses"
        self.save()
        try:
            result = self.broker.buy(symbol, cost, price, client_id)
        except Exception as exc:
            st["reconciliation_required"] = True
            st["status"] = "reconciliation"
            self.event("ERROR", f"Buy {symbol} ambiguous ({type(exc).__name__}); rekonsiliasi manual")
            self.save()
            raise
        pos = {"qty": result["qty"], "entry": result["price"], "cost": result["cost"],
               "sl": result["price"] * (1 - self.cfg.sl_pct / 100),
               "tp": result["price"] * (1 + tp_pct / 100), "tp_pct": tp_pct,
               "opened_at": now, "oco": None, "buy_order_id": result["id"]}
        st["pending_order"] = None
        st.update(status="in_position", position=pos, last_trade_ts=now)
        st["entries"].append(now)
        tag = "" if state_mode(self.cfg) == "live" else f" [{mode_label(self.cfg)}]"
        diag["reason"] = "BUY berhasil dibuka" + tag
        self.save()
        if not self.cfg.dry_run and self.cfg.use_oco:
            self.arm_oco(symbol, st, pos)
        self.notify(f"BUY {symbol}{tag} @ {result['price']:g}; TP {pos['tp']:g}, SL {pos['sl']:g}")

    def handle_exception(self, symbol: str, exc: Exception):
        text = f"{type(exc).__name__}: {str(exc)[:240]}"
        name = type(exc).__name__.lower()
        status = str(getattr(exc, "http_status", "") or "")
        headers = getattr(self.ex, "last_response_headers", None) or {}
        retry = headers.get("Retry-After") or headers.get("retry-after")
        if "ratelimit" in name or "ddos" in name or status in {"418", "429"} or " 429" in str(exc) or " 418" in str(exc):
            self.rate_limit_events += 1
            try:
                delay = max(1, float(retry)) if retry is not None else (300 if status == "418" or "418" in str(exc) else 60)
            except (TypeError, ValueError):
                delay = 60
            self.pause_until = max(self.pause_until, time.time() + delay)
            self.event("ERROR", f"Binance protection/rate limit: pause {delay:g}s; no aggressive retry. {text}")
        else:
            self.event("ERROR", f"{symbol}: {text}")

    def asset_value(self, asset: str, amount: float):
        """Nilai aset non-quote dalam quote via ticker spot; None bila market/harga tidak tersedia."""
        for market in getattr(self.ex, "markets", {}).values():
            if (market.get("base") == asset and market.get("quote") == self.cfg.quote
                    and market.get("spot") and market.get("active") is not False):
                try:
                    return amount * self.price(market["symbol"], 15)
                except Exception:
                    return None
        return None

    def update_portfolio(self):
        """Snapshot portofolio read-only untuk dashboard. Tidak pernah mengirim order."""
        now = time.time()
        positions: list[dict] = []
        assets: list[dict] = []
        unpriced: list[str] = []

        def position_row(symbol: str, pos: dict) -> dict:
            qty = float(pos.get("qty") or 0.0)
            try:
                mark = self.price(symbol, 15)
            except Exception:
                mark = None
            if mark is None:
                unpriced.append(symbol.split("/")[0])
                return {"symbol": symbol, "qty": qty, "entry": float(pos.get("entry") or 0.0),
                        "mark": None, "value": None, "unrealized_pnl": None,
                        "tp": pos.get("tp"), "sl": pos.get("sl")}
            value = qty * mark
            return {"symbol": symbol, "qty": qty, "entry": float(pos.get("entry") or 0.0),
                    "mark": mark, "value": value, "unrealized_pnl": value - float(pos.get("cost") or 0.0),
                    "tp": pos.get("tp"), "sl": pos.get("sl")}

        if self.cfg.dry_run:
            quote_free = float(self.state.get("paper_quote") or 0.0)
            equity = quote_free
            assets.append({"asset": self.cfg.quote, "total": quote_free, "free": quote_free,
                           "used": 0.0, "value_quote": quote_free})
            for symbol, st in self.state["symbols"].items():
                pos = st.get("position")
                if not pos:
                    continue
                row = position_row(symbol, pos)
                positions.append(row)
                if row["value"] is not None:
                    equity += row["value"]
                    assets.append({"asset": symbol.split("/")[0], "total": row["qty"], "free": row["qty"],
                                   "used": 0.0, "value_quote": row["value"]})
            valuation = "portofolio kertas (dry run)"
        else:
            balance = self.broker.fetch_balance()
            totals = balance.get("total") or {}
            frees = balance.get("free") or {}
            useds = balance.get("used") or {}
            equity = 0.0
            for asset in sorted(totals):
                amount = float(totals.get(asset) or 0.0)
                if amount <= 0:
                    continue
                value = amount if asset == self.cfg.quote else self.asset_value(asset, amount)
                if value is None:
                    unpriced.append(asset)
                else:
                    equity += value
                assets.append({"asset": asset, "total": amount, "free": float(frees.get(asset) or 0.0),
                               "used": float(useds.get(asset) or 0.0), "value_quote": value})
            for symbol, st in self.state["symbols"].items():
                if st.get("position"):
                    positions.append(position_row(symbol, st["position"]))
            valuation = "mark-to-market akun" + ("; aset tanpa harga dikecualikan" if unpriced else "")

        self.state["portfolio_snapshot"] = {
            "updated_at": now, "mode": self.state.get("bot_mode", "unknown"), "quote": self.cfg.quote,
            "valuation": valuation, "equity": equity,
            "unrealized_pnl": sum(float(row["unrealized_pnl"]) for row in positions if row["unrealized_pnl"] is not None),
            "realized_pnl": float(self.state.get("realized_pnl") or 0.0),
            "realized_tracking_since": self.state.get("realized_tracking_since"),
            "positions": positions, "assets": assets, "unpriced_assets": unpriced,
            "rate_limit_events": self.rate_limit_events, "paused_until": self.pause_until,
        }
        self.last_portfolio_update = now

    def run(self):
        self.notify(f"Bot dimulai ({mode_label(self.cfg)})")
        while self.running:
            if time.time() >= self.pause_until:
                for symbol in self.cfg.symbols:
                    if not self.running:
                        break
                    try:
                        self.process(symbol)
                    except Exception as exc:
                        self.handle_exception(symbol, exc)
            if time.time() - self.last_portfolio_update >= 30:
                try:
                    self.update_portfolio()
                except Exception as exc:
                    self.event("ERROR", f"Portfolio snapshot gagal: {type(exc).__name__}: {str(exc)[:200]}")
            try:
                self.save()
            except Exception:
                log.exception("Gagal menyimpan state")
            time.sleep(self.cfg.loop_seconds)
        self.save()
        self.notify("Bot dihentikan")


def state_mode(cfg: Config) -> str:
    return "dry" if cfg.dry_run else ("testnet" if cfg.testnet else "live")


def mode_label(cfg: Config) -> str:
    return {"dry": "DRY RUN", "testnet": "TESTNET", "live": "LIVE"}[state_mode(cfg)]


def new_state(path: str, cfg: Config) -> dict:
    state = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    mode = state_mode(cfg)
    old = state.get("bot_mode")
    if old and old != mode:
        raise RuntimeError(f"State mode {old} tidak cocok dengan {mode}; rekonsiliasi manual")
    state["bot_mode"] = mode
    state.setdefault("paper_quote", cfg.dry_balance)
    state.setdefault("symbols", {})
    state.setdefault("realized_pnl", 0.0)
    state.setdefault("realized_trades", [])
    state.setdefault("realized_tracking_since", time.time())
    state.setdefault("events", [])
    for symbol in cfg.symbols:
        base = new_symbol_state()
        base.update(state["symbols"].get(symbol, {}))
        state["symbols"][symbol] = base
    return state


def resolve_symbols(ex: Any, symbols: list[str], quote: str) -> list[str]:
    resolved = []
    for raw in symbols:
        key = raw.upper().replace("-", "/").replace("_", "/")
        symbol = key if key in ex.markets else None
        if not symbol:
            for market in ex.markets_by_id.get(key.replace("/", ""), []):
                if market.get("spot"):
                    symbol = market["symbol"]
                    break
        if not symbol or not ex.markets[symbol].get("spot") or ex.markets[symbol].get("active") is False:
            raise ValueError(f"Pair tidak aktif/tidak ditemukan: {raw}")
        if ex.markets[symbol].get("quote") != quote:
            raise ValueError(f"{symbol} bukan quote {quote}")
        if symbol not in resolved:
            resolved.append(symbol)
    return resolved


def main() -> int:
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    load_dotenv(env_file if os.path.exists(env_file) else None)
    try:
        cfg = Config.from_env()
    except ValueError as exc:
        print(exc)
        return 2

    logging.basicConfig(level=logging.INFO,
        handlers=[logging.StreamHandler(), RotatingFileHandler(cfg.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")],
        format="%(asctime)s %(levelname)s %(message)s")

    notifier = Notifier(cfg.tg_token, cfg.tg_chat)
    if not (cfg.tg_token and cfg.tg_chat):
        log.warning("Telegram tidak dikonfigurasi (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID kosong); "
                    "notifikasi start/stop/trade dinonaktifkan")

    ex = ccxt.binance({"apiKey": cfg.key or None, "secret": cfg.secret or None,
                       "enableRateLimit": True, "timeout": 20000,
                       "options": {"defaultType": "spot", "adjustForTimeDifference": True}})
    try:
        if cfg.testnet:
            ex.set_sandbox_mode(True)
        ex.load_markets()
        cfg.symbols = resolve_symbols(ex, cfg.symbols, cfg.quote)
    except Exception as exc:
        log.exception("Gagal menyiapkan exchange")
        notifier.send("Bot tidak dijalankan: gagal menyiapkan exchange "
                      f"({type(exc).__name__}: {str(exc)[:160]})")
        return 2

    try:
        state = new_state(cfg.state_file, cfg)
    except Exception as exc:
        log.error("State perlu rekonsiliasi manual: %s", exc)
        notifier.send(f"Bot tidak dijalankan: state perlu rekonsiliasi manual ({exc})")
        return 2

    broker = PaperBroker(state, cfg.paper_fee) if cfg.dry_run else LiveBroker(ex, cfg.quote)

    try:
        with ProcessLock(cfg.state_file):
            bot = Bot(cfg, ex, broker, notifier, state)
            if not cfg.dry_run:
                try:
                    broker.quote_free()
                except Exception as exc:
                    log.error("Gagal membaca saldo: %s", type(exc).__name__)
                    notifier.send(f"Bot tidak dijalankan: gagal membaca saldo ({type(exc).__name__})")
                    return 2
            try:
                bot.reconcile_startup()
            except Exception as exc:
                log.exception("Rekonsiliasi startup gagal")
                notifier.send("Bot tidak dijalankan: rekonsiliasi startup gagal "
                              f"({type(exc).__name__}: {str(exc)[:160]})")
                return 2
            signal.signal(signal.SIGINT, lambda *_: setattr(bot, "running", False))
            signal.signal(signal.SIGTERM, lambda *_: setattr(bot, "running", False))
            bot.run()
    except RuntimeError as exc:
        log.error("%s", exc)
        notifier.send(f"Bot tidak dijalankan: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
