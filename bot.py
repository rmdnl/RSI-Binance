#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bot Trading Spot Multicoin Binance
Strategi: RSI Oversold Reversal (konteks 4H + 1H, trigger 15m)

Aturan entry (semua harus terpenuhi):
  1. RSI 4H  < RSI_4H_MAX  (default 30)   -> konteks oversold besar
  2. RSI 1H  < RSI_1H_MAX  (default 30)   -> konteks oversold besar
  3. RSI 15m (candle close) pernah < RSI_OVERSOLD (30) dalam ARM_LOOKBACK candle terakhir,
     lalu menembus naik ke atas RSI_TRIGGER (32)  -> RSI mulai berbalik naik

Manajemen posisi:
  - Ukuran posisi : POSITION_PCT % dari equity (default 30%)
  - Take profit   : dinamis 1-3% (berdasarkan volatilitas ATR 1H saat entry)
  - Stop loss     : SL_PCT % (default 2%) -> pair masuk status COOLDOWN
  - Eksekusi exit : mode live memasang order OCO di Binance (TP = LIMIT_MAKER, SL = STOP_LOSS),
                    sehingga posisi tetap terlindungi walau bot/VPS mati. Bila OCO gagal dipasang,
                    bot memantau harga sendiri dan mencoba memasang OCO lagi tiap 5 menit.
                    Mode DRY_RUN mensimulasikan TP/SL lewat pemantauan harga.
  - Anti-whipsaw  : lockout re-entry, wajib "reset" RSI 15m >= REARM_LEVEL setelah trade,
                    dan batas jumlah entry per pair per jendela waktu

Konfigurasi lewat file .env (lihat .env.example).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

log = logging.getLogger("bot")

TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
CANDLE_LIMIT = 300  # cukup agar RSI Wilder konvergen (sama dengan TradingView)
NETWORK_ERRORS: tuple = ()  # diisi saat ccxt di-import


# =============================================================================
# Konfigurasi
# =============================================================================
def _get(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else v.strip()


def _f(name: str, default: float) -> float:
    raw = _get(name, str(default))
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} harus berupa angka, bukan '{raw}'")


def _i(name: str, default: int) -> int:
    raw = _get(name, str(default))
    try:
        return int(float(raw))
    except ValueError:
        raise ValueError(f"{name} harus berupa bilangan bulat, bukan '{raw}'")


def _b(name: str, default: bool) -> bool:
    return _get(name, "true" if default else "false").lower() in ("1", "true", "yes", "y", "on")


@dataclass
class Config:
    api_key: str
    api_secret: str
    testnet: bool
    dry_run: bool
    quote: str
    symbols: List[str]
    rsi_period: int
    rsi_oversold: float
    rsi_trigger: float
    rsi_4h_max: float
    rsi_1h_max: float
    arm_lookback: int
    context_use_closed: bool
    rearm_level: float
    position_pct: float
    max_open: int
    tp_min: float
    tp_max: float
    tp_atr_mult: float
    sl_pct: float
    use_oco: bool
    cooldown_min: float
    lockout_min: float
    max_entries_window: int
    entry_window_hours: float
    loop_seconds: float
    dry_run_balance: float
    paper_fee_pct: float
    tg_token: str
    tg_chat: str
    state_file: str
    log_file: str

    @classmethod
    def from_env(cls) -> "Config":
        symbols = [s for s in re.split(r"[,\s;]+", _get("SYMBOLS")) if s]
        cfg = cls(
            api_key=_get("BINANCE_API_KEY"),
            api_secret=_get("BINANCE_API_SECRET"),
            testnet=_b("BINANCE_TESTNET", False),
            dry_run=_b("DRY_RUN", True),
            quote=_get("QUOTE_ASSET", "USDT").upper(),
            symbols=symbols,
            rsi_period=_i("RSI_PERIOD", 14),
            rsi_oversold=_f("RSI_OVERSOLD", 30),
            rsi_trigger=_f("RSI_TRIGGER", 32),
            rsi_4h_max=_f("RSI_4H_MAX", 30),
            rsi_1h_max=_f("RSI_1H_MAX", 30),
            arm_lookback=_i("ARM_LOOKBACK", 12),
            context_use_closed=_b("CONTEXT_USE_CLOSED", False),
            rearm_level=_f("REARM_LEVEL", 50),
            position_pct=_f("POSITION_PCT", 30),
            max_open=_i("MAX_OPEN_POSITIONS", 3),
            tp_min=_f("TP_MIN_PCT", 1.0),
            tp_max=_f("TP_MAX_PCT", 3.0),
            tp_atr_mult=_f("TP_ATR_MULT", 1.5),
            sl_pct=_f("SL_PCT", 2.0),
            use_oco=_b("USE_OCO", True),
            cooldown_min=_f("COOLDOWN_MINUTES", 240),
            lockout_min=_f("REENTRY_LOCKOUT_MINUTES", 120),
            max_entries_window=_i("MAX_ENTRIES_PER_WINDOW", 2),
            entry_window_hours=_f("ENTRY_WINDOW_HOURS", 24),
            loop_seconds=_f("LOOP_SECONDS", 10),
            dry_run_balance=_f("DRY_RUN_BALANCE", 1000),
            paper_fee_pct=_f("PAPER_FEE_PCT", 0.1),
            tg_token=_get("TELEGRAM_BOT_TOKEN"),
            tg_chat=_get("TELEGRAM_CHAT_ID"),
            state_file=_get("STATE_FILE", "state.json"),
            log_file=_get("LOG_FILE", "bot.log"),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        errs = []
        if not self.symbols:
            errs.append("SYMBOLS kosong. Isi minimal satu pair, mis. SYMBOLS=BNB/USDT,ETH/USDT")
        if not self.dry_run and not (self.api_key and self.api_secret):
            errs.append("DRY_RUN=false butuh BINANCE_API_KEY dan BINANCE_API_SECRET")
        numeric_values = {
            "RSI_OVERSOLD": self.rsi_oversold, "RSI_TRIGGER": self.rsi_trigger,
            "RSI_4H_MAX": self.rsi_4h_max, "RSI_1H_MAX": self.rsi_1h_max,
            "REARM_LEVEL": self.rearm_level, "POSITION_PCT": self.position_pct,
            "TP_MIN_PCT": self.tp_min, "TP_MAX_PCT": self.tp_max,
            "TP_ATR_MULT": self.tp_atr_mult, "SL_PCT": self.sl_pct,
            "COOLDOWN_MINUTES": self.cooldown_min, "REENTRY_LOCKOUT_MINUTES": self.lockout_min,
            "ENTRY_WINDOW_HOURS": self.entry_window_hours, "LOOP_SECONDS": self.loop_seconds,
            "PAPER_FEE_PCT": self.paper_fee_pct, "DRY_RUN_BALANCE": self.dry_run_balance,
        }
        for name, value in numeric_values.items():
            if not math.isfinite(float(value)):
                errs.append(f"{name} harus berupa angka finite")
        if self.rsi_period < 2:
            errs.append("RSI_PERIOD minimal 2")
        if not (0 <= self.rsi_oversold < self.rsi_trigger <= 100):
            errs.append("RSI_OVERSOLD dan RSI_TRIGGER harus 0..100 serta trigger > oversold")
        if not (0 < self.rsi_4h_max <= 100 and 0 < self.rsi_1h_max <= 100):
            errs.append("RSI_4H_MAX dan RSI_1H_MAX harus > 0 dan <= 100")
        if not (0 <= self.rearm_level <= 100):
            errs.append("REARM_LEVEL harus 0..100")
        if not (0 < self.position_pct <= 100):
            errs.append("POSITION_PCT harus di antara 0 dan 100")
        if self.tp_min <= 0 or self.tp_max < self.tp_min or self.tp_atr_mult <= 0:
            errs.append("TP_MIN_PCT/TP_ATR_MULT harus > 0 dan TP_MAX_PCT >= TP_MIN_PCT")
        if self.sl_pct <= 0:
            errs.append("SL_PCT harus > 0")
        if self.max_open < 1 or self.max_entries_window < 1:
            errs.append("MAX_OPEN_POSITIONS dan MAX_ENTRIES_PER_WINDOW minimal 1")
        if self.arm_lookback < 1:
            errs.append("ARM_LOOKBACK minimal 1")
        if self.cooldown_min < 0 or self.lockout_min < 0 or self.entry_window_hours <= 0:
            errs.append("Cooldown/lockout tidak boleh negatif dan ENTRY_WINDOW_HOURS harus > 0")
        if self.paper_fee_pct < 0:
            errs.append("PAPER_FEE_PCT tidak boleh negatif")
        if self.loop_seconds < 1:
            errs.append("LOOP_SECONDS minimal 1")
        if errs:
            raise ValueError("Konfigurasi .env tidak valid:\n  - " + "\n  - ".join(errs))


# =============================================================================
# Indikator
# =============================================================================
def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 50.0 if avg_gain == 0 else 100.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(closes: List[float], period: int = 14) -> List[Optional[float]]:
    """RSI Wilder (RMA) - sama dengan ta.rsi di TradingView."""
    n = len(closes)
    out: List[Optional[float]] = [None] * n
    if n <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    avg_g, avg_l = gain / period, loss / period
    out[period] = _rsi_value(avg_g, avg_l)
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
        out[i] = _rsi_value(avg_g, avg_l)
    return out


def atr_pct(candles: List[list], period: int = 14) -> Optional[float]:
    """ATR Wilder dalam persen dari close terakhir. candles = [ts, o, h, l, c, v]."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    last = candles[-1][4]
    return atr / last * 100.0 if last > 0 else None


def closed_only(candles: List[list], tf_ms: int, now_ms: int) -> List[list]:
    """Buang candle yang masih berjalan (belum close)."""
    return [c for c in candles if c[0] + tf_ms <= now_ms]


def last_val(series: List[Optional[float]]) -> Optional[float]:
    return series[-1] if series and series[-1] is not None else None


def fp(x: float) -> str:
    return f"{x:.6g}"


# =============================================================================
# Telegram
# =============================================================================
class Notifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token and chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            import requests

            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                data={"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True},
                timeout=10,
            )
            if not r.ok:
                log.warning("Telegram gagal: HTTP %s %s", r.status_code, r.text[:120])
        except Exception as e:  # jangan log pesan error penuh: bisa memuat token di URL
            log.warning("Telegram gagal: %s", type(e).__name__)


# =============================================================================
# Broker (paper / live)
# =============================================================================
class PaperBroker:
    """Simulasi (DRY_RUN): tidak mengirim order ke Binance."""

    def __init__(self, state: dict, fee_pct: float):
        self.state = state
        self.fee = fee_pct / 100.0

    def quote_free(self) -> float:
        return float(self.state["paper_quote"])

    def base_free(self, sym: str) -> float:
        pos = self.state["symbols"].get(sym, {}).get("position")
        return float(pos["qty"]) if pos else 0.0

    def buy(self, sym: str, cost: float, last_price: float) -> dict:
        qty = cost / last_price * (1 - self.fee)
        self.state["paper_quote"] -= cost
        return {"qty": qty, "price": last_price, "cost": cost}

    def sell(self, sym: str, qty: float, last_price: float) -> Optional[dict]:
        proceeds = qty * last_price * (1 - self.fee)
        self.state["paper_quote"] += proceeds
        return {"qty": qty, "price": last_price, "proceeds": proceeds}


class LiveBroker:
    """Order market sungguhan lewat ccxt."""

    def __init__(self, ex: Any, quote: str):
        self.ex = ex
        self.quote = quote

    def quote_free(self) -> float:
        bal = self.ex.fetch_balance()
        return float((bal.get("free") or {}).get(self.quote) or 0.0)

    def base_free(self, sym: str) -> float:
        base = sym.split("/")[0]
        bal = self.ex.fetch_balance()
        return float((bal.get("free") or {}).get(base) or 0.0)

    def _settle(self, order: dict, sym: str) -> dict:
        if order.get("average") is None or order.get("filled") is None:
            order = self.ex.fetch_order(order["id"], sym)
        return order

    def buy(
        self, sym: str, cost: float, last_price: float,
        client_order_id: Optional[str] = None, submit_new: bool = True,
    ) -> dict:
        cost = float(self.ex.cost_to_precision(sym, cost))
        if not client_order_id:
            raise RuntimeError(f"BUY {sym} ditolak: clientOrderId wajib ada")
        if submit_new:
            order = self.ex.create_market_buy_order_with_cost(
                sym, cost, {"clientOrderId": client_order_id}
            )
        else:
            order = self.ex.fetch_order(
                None, sym, {"origClientOrderId": client_order_id}
            )
        order = self._settle(order, sym)
        filled = float(order.get("filled") or 0)
        status = str(order.get("status") or "").lower()
        if filled <= 0 or status == "open":
            raise RuntimeError(
                f"BUY {sym} belum final: id={order.get('id')} status={status} filled={filled}"
            )
        price = float(order.get("average") or last_price)
        if price <= 0:
            raise RuntimeError(f"BUY {sym} harga fill tidak valid")
        spent = float(order.get("cost") or filled * price)
        base = sym.split("/")[0]
        net = filled
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            if not fee or not fee.get("cost"):
                continue
            fee_cost = float(fee["cost"])
            if fee.get("currency") == base:
                net -= fee_cost
            elif fee.get("currency") == self.quote:
                spent += fee_cost
        if net <= 0:
            raise RuntimeError(f"BUY {sym} net quantity tidak valid setelah fee")
        return {"qty": net, "price": price, "cost": spent}

    def sellable_amount(self, sym: str, qty: float, price: float) -> tuple:
        """Return (rounded_qty, reason) after Binance amount/notional filters."""
        free = self.base_free(sym)
        available = min(float(qty), free)
        if available <= 0:
            return 0.0, "no_free_balance"
        rounded = float(self.ex.amount_to_precision(sym, available))
        market = self.ex.market(sym)
        info = market.get("info") or {}
        filters = {x.get("filterType"): x for x in (info.get("filters") or [])}
        if free + max(float(qty) * 1e-8, 1e-12) < float(qty):
            return 0.0, "balance_mismatch"
        market_lot = filters.get("MARKET_LOT_SIZE") or {}
        market_min = float(market_lot.get("minQty") or 0)
        lot_min = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0)
        min_amount = max(market_min, lot_min)
        min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0)
        if "NOTIONAL" in filters:
            nf = filters["NOTIONAL"]
            if nf.get("applyMinToMarket", True):
                min_cost = max(min_cost, float(nf.get("minNotional") or 0))
        elif "MIN_NOTIONAL" in filters:
            nf = filters["MIN_NOTIONAL"]
            if nf.get("applyToMarket", True):
                min_cost = max(min_cost, float(nf.get("minNotional") or 0))
        if rounded <= 0 or rounded < min_amount:
            return 0.0, "below_min_amount"
        if min_cost and rounded * float(price) < min_cost:
            return 0.0, "below_min_notional"
        return rounded, None

    def sell(
        self,
        sym: str,
        qty: float,
        last_price: float,
        client_order_id: Optional[str] = None,
        submit_new: bool = True,
    ) -> Optional[dict]:
        if not client_order_id:
            raise RuntimeError(f"SELL {sym} ditolak: clientOrderId wajib ada")

        if submit_new:
            q, reason = self.sellable_amount(sym, qty, last_price)
            if reason == "no_free_balance":
                return {"reconciliation_required": True, "reason": reason}
            if reason:
                return {"dust": True, "reason": reason}

            # ID sudah disimpan ke state sebelum request dikirim.
            order = self.ex.create_market_sell_order(
                sym, q, {"clientOrderId": client_order_id}
            )
        else:
            # Request sebelumnya mungkin diterima Binance meski respons timeout.
            # Cari order lama. Jangan pernah mengirim order kedua secara otomatis.
            order = self.ex.fetch_order(
                None, sym, {"origClientOrderId": client_order_id}
            )

        order = self._settle(order, sym)

        filled_raw = order.get("filled")
        if filled_raw is None:
            raise RuntimeError(
                f"SELL {sym} tanpa data filled: id={order.get('id')} "
                f"clientOrderId={client_order_id}"
            )

        filled = float(filled_raw)
        status = str(order.get("status") or "").lower()

        if filled <= 0 or status == "open":
            raise RuntimeError(
                f"SELL {sym} belum final: id={order.get('id')} "
                f"status={status} filled={filled}; menunggu rekonsiliasi"
            )

        price = float(order.get("average") or 0)
        if price <= 0:
            raise RuntimeError(
                f"SELL {sym} harga fill tidak valid: id={order.get('id')}"
            )

        proceeds = float(order.get("cost") or filled * price)
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            if fee and fee.get("currency") == self.quote and fee.get("cost"):
                proceeds -= float(fee["cost"])

        return {"qty": filled, "price": price, "proceeds": proceeds}

    # ---- OCO: TP + SL dieksekusi di sisi Binance ------------------------------
    def oco_supported(self, sym: str) -> bool:
        info = self.ex.market(sym).get("info") or {}
        if info.get("ocoAllowed") is False:
            return False
        types = info.get("orderTypes") or []
        return not types or "LIMIT_MAKER" in types

    def place_oco(self, sym: str, qty: float, tp_price: float, sl_stop: float) -> dict:
        """SELL OCO: sisi atas LIMIT_MAKER (TP), sisi bawah STOP_LOSS (SL, market saat terpicu)."""
        if not self.oco_supported(sym):
            raise RuntimeError(f"{sym} tidak mendukung OCO (ocoAllowed/LIMIT_MAKER)")
        info = self.ex.market(sym).get("info") or {}
        types = info.get("orderTypes") or []
        q, reason = self.sellable_amount(sym, qty, sl_stop)
        if reason:
            raise RuntimeError(f"Qty OCO {sym} tidak dapat diperdagangkan: {reason}")
        q = self.ex.amount_to_precision(sym, q)
        pp = lambda x: self.ex.price_to_precision(sym, x)  # noqa: E731
        params = {
            "symbol": self.ex.market_id(sym),
            "side": "SELL",
            "quantity": q,
            "aboveType": "LIMIT_MAKER",
            "abovePrice": pp(tp_price),
        }
        if types and "STOP_LOSS" not in types and "STOP_LOSS_LIMIT" in types:
            params.update(belowType="STOP_LOSS_LIMIT", belowStopPrice=pp(sl_stop),
                          belowPrice=pp(sl_stop * 0.997), belowTimeInForce="GTC")
        else:
            params.update(belowType="STOP_LOSS", belowStopPrice=pp(sl_stop))
        res = self.ex.request("orderList/oco", ["private"], "POST", params)
        tp_id = sl_id = None
        for r in res.get("orderReports") or []:
            if str(r.get("type", "")).upper().startswith("STOP_LOSS"):
                sl_id = str(r["orderId"])
            else:
                tp_id = str(r["orderId"])
        if tp_id and sl_id:
            return {"list_id": str(res.get("orderListId")), "tp_id": tp_id, "sl_id": sl_id}
        found = self.find_oco(sym)  # respons tidak lengkap: cari order yang baru terpasang
        if not found:
            raise RuntimeError(f"Respons OCO {sym} tidak lengkap dan order tidak ditemukan")
        return found

    def find_oco(self, sym: str) -> Optional[dict]:
        """Cari pasangan OCO SELL yang sedang aktif untuk pair ini (adopsi bila sudah ada)."""
        groups: Dict[str, dict] = {}
        for o in self.ex.fetch_open_orders(sym):
            info = o.get("info") or {}
            lid = str(info.get("orderListId", -1))
            if lid in ("-1", "None", ""):
                continue
            if str(info.get("side", "")).upper() != "SELL":
                continue
            t = str(info.get("type", "")).upper()
            g = groups.setdefault(lid, {})
            if t.startswith("STOP_LOSS"):
                g["sl_id"] = str(o["id"])
            elif t.startswith(("LIMIT_MAKER", "TAKE_PROFIT")):
                g["tp_id"] = str(o["id"])
        for lid, g in groups.items():
            if "tp_id" in g and "sl_id" in g:
                return {"list_id": lid, **g}
        return None

    def poll_oco(self, sym: str, oco: dict) -> tuple:
        """Return ('active'|'filled'|'gone', hasil). 'filled' memuat leg TP/SL yang terisi."""
        ids = {str(oco["tp_id"]), str(oco["sl_id"])}
        if ids & {str(o["id"]) for o in self.ex.fetch_open_orders(sym)}:
            return "active", None
        for leg, oid in (("SL", oco["sl_id"]), ("TP", oco["tp_id"])):
            try:
                o = self.ex.fetch_order(str(oid), sym)
            except Exception as exc:
                if type(exc).__name__ == "OrderNotFound":
                    continue
                raise
            filled = float(o.get("filled") or 0)
            if filled > 0:
                price = float(o.get("average") or o.get("price") or 0)
                if price <= 0:
                    raise RuntimeError(f"Fill OCO {sym} tanpa harga valid")
                proceeds = float(o.get("cost") or filled * price)
                fees = o.get("fees") or ([o["fee"]] if o.get("fee") else [])
                for fee in fees:
                    if fee and fee.get("currency") == self.quote and fee.get("cost"):
                        proceeds -= float(fee["cost"])
                return "filled", {"leg": leg, "qty": filled, "price": price, "proceeds": proceeds}
        return "gone", None


# =============================================================================
# Bot
# =============================================================================
def new_symbol_state() -> dict:
    return {
        "status": "idle",  # idle | in_position | cooldown
        "cooldown_until": 0.0,
        "last_eval_ts": 0,  # open-time (ms) candle 15m terakhir yang sudah dievaluasi
        "need_reset": False,  # wajib RSI 15m >= REARM_LEVEL dulu sebelum entry berikutnya
        "last_trade_ts": 0.0,
        "entries": [],  # timestamp entry (untuk batas entry per jendela waktu)
        "position": None,
    }


class Bot:
    def __init__(self, cfg: Config, ex: Any, broker: Any, notifier: Notifier, state: dict, now_fn=time.time):
        self.cfg = cfg
        self.ex = ex
        self.broker = broker
        self.notifier = notifier
        self.state = state
        self.now = now_fn
        self.running = True
        self._err_ts: Dict[str, float] = {}
        self._retry_at: Dict[str, float] = {}

    # ---- util --------------------------------------------------------------
    def save(self) -> None:
        tmp = self.cfg.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.cfg.state_file)

    def notify(self, text: str) -> None:
        log.info(text.replace("\n", " | "))
        self.notifier.send(text)

    def fetch_candles(self, sym: str, tf: str) -> List[list]:
        return self.ex.fetch_ohlcv(sym, tf, limit=CANDLE_LIMIT)

    def last_price(self, sym: str) -> float:
        return float(self.ex.fetch_ticker(sym)["last"])

    def min_cost(self, sym: str) -> float:
        m = self.ex.market(sym)
        c = ((m.get("limits") or {}).get("cost") or {}).get("min")
        return float(c) if c else 0.0

    # ---- siklus ------------------------------------------------------------
    def process(self, sym: str) -> None:
        st = self.state["symbols"][sym]
        now = self.now()

        if st.get("buy_pending"):
            self._reconcile_buy(sym, st, now)
            return
        if st.get("status") in ("dust", "reconciliation_required"):
            return

        if st["status"] == "cooldown":
            if now < st["cooldown_until"]:
                return
            st["status"] = "idle"
            st["cooldown_until"] = 0.0
            self.save()
            self.notify(f"⏳ Cooldown {sym} selesai. Pair aktif kembali (menunggu sinyal baru).")

        if st["status"] == "in_position":
            self._check_exit(sym, st, now)
            return

        self._scan_entry(sym, st, now)

    def _check_exit(self, sym: str, st: dict, now: float) -> None:
        pos = st["position"]

        # Rekonsiliasi SELL tertunda harus didahulukan dari pemeriksaan harga.
        # Jangan mengirim order baru hanya karena harga sudah berbalik.
        pending = pos.get("sell_pending")
        if pending:
            price = self.last_price(sym)
            self._exit(
                sym, st, pending.get("reason", "SL"), price, now
            )
            return

        if pos.get("oco"):  # TP/SL dijaga Binance lewat OCO; bot hanya memantau statusnya
            self._check_oco(sym, st, pos, now)
            return
        price = self.last_price(sym)
        if price <= pos["sl"]:
            self._exit(sym, st, "SL", price, now)
            return
        if price >= pos["tp"]:
            self._exit(sym, st, "TP", price, now)
            return
        if now >= pos.get("oco_retry_at", 0):
            self._arm_oco(sym, st, pos, now)

    def _mark_dust(self, sym: str, st: dict, pos: dict, reason: str) -> None:
        pos.pop("sell_pending", None)
        pos["dust"] = True
        pos["dust_reason"] = reason
        st["status"] = "dust"
        self.save()
        self.notify(
            f"⚠️ {sym}: posisi di bawah filter order Binance ({reason}). "
            f"Dicatat sebagai DUST dan tidak akan dicoba SELL/OCO berulang."
        )

    def _mark_reconciliation(self, sym: str, st: dict, reason: str) -> None:
        st["status"] = "reconciliation_required"
        st["reconciliation_reason"] = reason
        self.save()
        self.notify(
            f"🚨 {sym}: rekonsiliasi manual diperlukan ({reason}). "
            f"Posisi dipertahankan di state dan entry baru diblokir."
        )

    def _arm_oco(self, sym: str, st: dict, pos: dict, now: float) -> bool:
        """Pasang (atau adopsi yang sudah ada) order OCO TP+SL di Binance. Hanya mode live."""
        cfg = self.cfg
        if cfg.dry_run or not cfg.use_oco or not hasattr(self.broker, "place_oco"):
            return False
        try:
            existing = self.broker.find_oco(sym)
            if existing:
                oco = existing
            else:
                qty, reason = self.broker.sellable_amount(sym, pos["qty"], pos["sl"])
                if reason in ("below_min_amount", "below_min_notional"):
                    self._mark_dust(sym, st, pos, reason)
                    return False
                if reason:
                    self._mark_reconciliation(sym, st, reason)
                    return False
                oco = self.broker.place_oco(sym, qty, pos["tp"], pos["sl"])
        except Exception as e:
            pos["oco"] = None
            pos["oco_retry_at"] = now + 300
            log.warning("%s: gagal memasang OCO (%s: %s)", sym, type(e).__name__, str(e)[:200])
            if not pos.get("oco_warned"):
                pos["oco_warned"] = True
                self.notifier.send(
                    f"⚠️ Gagal memasang OCO {sym} ({type(e).__name__}: {str(e)[:150]}).\n"
                    f"Bot memantau TP/SL sendiri dan mencoba lagi tiap 5 menit."
                )
            self.save()
            return False
        pos["oco"] = oco
        pos.pop("oco_retry_at", None)
        if pos.pop("oco_warned", False):
            self.notify(f"✅ OCO {sym} akhirnya terpasang di Binance.")
        self.save()
        return True

    def _check_oco(self, sym: str, st: dict, pos: dict, now: float) -> None:
        status, res = self.broker.poll_oco(sym, pos["oco"])
        if status == "active":
            return
        if status == "filled":
            self._record_exit(sym, st, res["leg"], res, now)
            return
        # OCO sudah tidak ada tanpa ada yang terisi (dibatalkan manual / kedaluwarsa)
        if self.broker.base_free(sym) < pos["qty"] * 0.5:
            self._mark_reconciliation(
                sym, st, "OCO hilang dan saldo bebas jauh di bawah kuantitas posisi"
            )
            return
        pos["oco"] = None
        pos["oco_retry_at"] = 0
        self.save()
        self.notify(f"⚠️ OCO {sym} tidak aktif lagi (dibatalkan/kedaluwarsa). "
                    f"Bot memantau TP/SL sendiri dan memasang ulang OCO.")

    def _exit(self, sym: str, st: dict, reason: str, price: float, now: float) -> None:
        pos = st["position"]
        pending = pos.get("sell_pending")
        submit_new = pending is None

        if submit_new and isinstance(self.broker, LiveBroker):
            _, preflight_reason = self.broker.sellable_amount(sym, pos["qty"], price)
            if preflight_reason in ("below_min_amount", "below_min_notional"):
                self._mark_dust(sym, st, pos, preflight_reason)
                return
            if preflight_reason:
                self._mark_reconciliation(sym, st, preflight_reason)
                return

        if submit_new:
            # Binance clientOrderId maksimal 36 karakter.
            base = sym.split("/")[0].lower()
            client_order_id = f"rsis{int(now * 1000)}{base}"
            pos["sell_pending"] = {
                "client_order_id": client_order_id,
                "reason": reason,
                "requested_qty": float(pos["qty"]),
                "created_at": now,
            }
            self.save()
        else:
            client_order_id = pending["client_order_id"]
            reason = pending.get("reason", reason)

        if isinstance(self.broker, LiveBroker):
            res = self.broker.sell(
                sym, pos["qty"], price, client_order_id, submit_new
            )
        else:
            res = self.broker.sell(sym, pos["qty"], price)

        if isinstance(res, dict) and res.get("dust"):
            self._mark_dust(sym, st, pos, res.get("reason", "below_minimum"))
            return
        if isinstance(res, dict) and res.get("reconciliation_required"):
            self._mark_reconciliation(sym, st, res.get("reason", "no_free_balance"))
            return

        if res is None:
            if not pos.get("sell_warned"):
                pos["sell_warned"] = True
                self.save()
                self.notify(
                    f"⚠️ {sym}: SELL {reason} belum dapat dilakukan. "
                    f"Posisi dan ID order tetap disimpan untuk rekonsiliasi."
                )
            return

        pos.pop("sell_warned", None)
        self._record_exit(sym, st, reason, res, now)

    def _record_exit(self, sym: str, st: dict, reason: str, res: dict, now: float) -> None:
        """Catat fill aktual; pertahankan sisa posisi bila order hanya terisi sebagian."""
        pos = st["position"]
        original_qty = float(pos["qty"])
        sold = min(float(res.get("qty") or 0), original_qty)

        if sold <= 0 or original_qty <= 0:
            raise RuntimeError(
                f"Fill exit {sym} tidak valid: sold={sold}, position={original_qty}"
            )

        pos.pop("sell_pending", None)
        original_cost = float(pos["cost"])
        cost_part = original_cost * (sold / original_qty)
        pnl = float(res["proceeds"]) - cost_part
        pnl_pct = pnl / cost_part * 100.0 if cost_part else 0.0
        q = self.cfg.quote
        remaining = max(0.0, original_qty - sold)
        tolerance = max(original_qty * 1e-8, 1e-12)

        # Partial fill: hanya kurangi bagian yang benar-benar terjual.
        if remaining > tolerance:
            pos["qty"] = remaining
            pos["cost"] = max(0.0, original_cost - cost_part)
            pos["oco"] = None
            pos["oco_retry_at"] = 0
            pos.pop("oco_warned", None)
            st["status"] = "in_position"
            if isinstance(self.broker, LiveBroker):
                _, dust_reason = self.broker.sellable_amount(sym, remaining, float(res["price"]))
                if dust_reason in ("below_min_amount", "below_min_notional"):
                    self._mark_dust(sym, st, pos, dust_reason)
                    self.notify(
                        f"PnL fill parsial {sym}: {pnl:+.2f} {q} ({pnl_pct:+.2f}%). "
                        f"Sisa {remaining:.8f} dicatat sebagai dust."
                    )
                    return
            self.save()
            self.notify(
                f"⚠️ PARTIAL EXIT {sym} ({reason})\n"
                f"Terjual {sold:.8f} dari {original_qty:.8f}\n"
                f"Sisa posisi {remaining:.8f}\n"
                f"PnL bagian terjual {pnl:+.2f} {q} ({pnl_pct:+.2f}%)\n"
                f"Sisa posisi tetap dipantau bot."
            )
            return

        # Full fill: baru sekarang posisi boleh ditutup.
        st["position"] = None
        st["last_trade_ts"] = now
        st["need_reset"] = True

        if reason == "SL":
            st["status"] = "cooldown"
            st["cooldown_until"] = now + self.cfg.cooldown_min * 60
            until = time.strftime("%H:%M", time.localtime(st["cooldown_until"]))
            self.save()
            self.notify(
                f"🛑 STOP LOSS {sym}\n"
                f"Entry {fp(pos['entry'])} → Exit {fp(res['price'])}\n"
                f"PnL {pnl:+.2f} {q} ({pnl_pct:+.2f}%)\n"
                f"Pair masuk COOLDOWN {self.cfg.cooldown_min:g} menit (sampai {until} waktu server)."
            )
        else:
            st["status"] = "idle"
            self.save()
            self.notify(
                f"🎯 TAKE PROFIT {sym}\n"
                f"Entry {fp(pos['entry'])} → Exit {fp(res['price'])}\n"
                f"PnL {pnl:+.2f} {q} ({pnl_pct:+.2f}%)"
            )

    def _finalize_buy(self, sym: str, st: dict, res: dict, now: float,
                      tp_pct: float, r4: float, r1: float, prev: float, last: float) -> None:
        st.pop("buy_pending", None)
        entry = float(res["price"])
        pos = {
            "qty": float(res["qty"]), "entry": entry, "cost": float(res["cost"]),
            "sl": entry * (1 - self.cfg.sl_pct / 100.0),
            "tp": entry * (1 + tp_pct / 100.0), "tp_pct": tp_pct,
            "opened_at": now, "oco": None,
        }
        st["position"] = pos
        st["status"] = "in_position"
        st["last_trade_ts"] = now
        st.setdefault("entries", []).append(now)
        self.save()
        armed = self._arm_oco(sym, st, pos, now)
        exit_mode = ("OCO terpasang di Binance (TP + SL)" if armed else
                     "simulasi, dipantau bot" if self.cfg.dry_run else
                     "dipantau bot (OCO belum terpasang, lihat peringatan)")
        self.notify(
            f"🟢 BUY {sym}{' [DRY RUN]' if self.cfg.dry_run else ''}\n"
            f"Harga {fp(entry)} | Nilai {res['cost']:.2f} {self.cfg.quote} "
            f"({self.cfg.position_pct:g}% equity)\n"
            f"TP {fp(pos['tp'])} (+{tp_pct:g}%) | SL {fp(pos['sl'])} (-{self.cfg.sl_pct:g}%)\n"
            f"Exit: {exit_mode}\n"
            f"RSI 4H {r4:.1f} | 1H {r1:.1f} | 15m {prev:.1f}→{last:.1f}"
        )

    def _reconcile_buy(self, sym: str, st: dict, now: float) -> None:
        pending = st["buy_pending"]
        if now < float(pending.get("next_check_at", 0)):
            return
        try:
            res = self.broker.buy(
                sym, pending["cost"], pending["price"],
                pending["client_order_id"], False,
            )
        except Exception as exc:
            if type(exc).__name__ == "OrderNotFound":
                pending["attempts"] = int(pending.get("attempts", 0)) + 1
                pending["next_check_at"] = now + 30
                self.save()
                if pending["attempts"] in (1, 5, 20):
                    self.notify(
                        f"🚨 BUY {sym} belum ditemukan saat rekonsiliasi. "
                        f"Tidak mengirim BUY ulang otomatis; posisi entry diblokir demi keamanan."
                    )
                return
            raise
        self._finalize_buy(
            sym, st, res, float(pending["created_at"]), float(pending["tp_pct"]),
            float(pending["rsi_4h"]), float(pending["rsi_1h"]),
            float(pending["rsi_15m_prev"]), float(pending["rsi_15m_last"]),
        )

    def _begin_buy(self, sym: str, st: dict, cost: float, price: float,
                   tp_pct: float, r4: float, r1: float, prev: float, last: float,
                   now: float) -> None:
        if isinstance(self.broker, LiveBroker):
            client_order_id = f"rsib{int(now * 1000)}{sym.split('/')[0].lower()}"
            st["buy_pending"] = {
                "client_order_id": client_order_id, "cost": cost, "price": price,
                "tp_pct": tp_pct, "rsi_4h": r4, "rsi_1h": r1,
                "rsi_15m_prev": prev, "rsi_15m_last": last,
                "created_at": now, "attempts": 0, "next_check_at": now,
            }
            self.save()
            res = self.broker.buy(sym, cost, price, client_order_id, True)
        else:
            res = self.broker.buy(sym, cost, price)
        self._finalize_buy(sym, st, res, now, tp_pct, r4, r1, prev, last)

    def _scan_entry(self, sym: str, st: dict, now: float) -> None:
        cfg = self.cfg
        if now < self._retry_at.get(sym, 0):
            return
        now_ms = int(now * 1000)
        tf15 = TF_MS["15m"]

        # Evaluasi hanya sekali per candle 15m yang baru close
        expected_open = (now_ms // tf15) * tf15 - tf15
        if st["last_eval_ts"] >= expected_open:
            return

        closed = closed_only(self.fetch_candles(sym, "15m"), tf15, now_ms)
        if len(closed) < cfg.rsi_period + cfg.arm_lookback + 2:
            log.warning("%s: data candle 15m kurang (%d)", sym, len(closed))
            self._retry_at[sym] = now + 60
            return
        if closed[-1][0] <= st["last_eval_ts"]:
            self._retry_at[sym] = now + 30  # candle baru belum tersedia di exchange
            return
        st["last_eval_ts"] = closed[-1][0]

        r15 = rsi_series([c[4] for c in closed], cfg.rsi_period)

        # Hysteresis anti-whipsaw: setelah trade, RSI 15m harus pulih >= REARM_LEVEL dulu
        if st["need_reset"]:
            after_ms = st["last_trade_ts"] * 1000
            for c, r in zip(closed, r15):
                if r is not None and c[0] >= after_ms and r >= cfg.rearm_level:
                    st["need_reset"] = False
                    log.info("%s: RSI 15m sudah pulih >= %g, pair siap lagi", sym, cfg.rearm_level)
                    break

        last, prev = r15[-1], r15[-2]
        if last is None or prev is None:
            return
        lookback = [x for x in r15[-1 - cfg.arm_lookback:-1] if x is not None]
        crossed = prev <= cfg.rsi_trigger < last
        armed = bool(lookback) and min(lookback) < cfg.rsi_oversold
        if not (crossed and armed):
            return

        tag = f"{sym}: sinyal 15m (RSI {prev:.1f}→{last:.1f}, min {min(lookback):.1f})"

        # ---- gerbang anti beli beruntun -------------------------------------
        if st["need_reset"]:
            log.info("%s -> ditahan: menunggu RSI 15m pulih >= %g sejak trade terakhir", tag, cfg.rearm_level)
            return
        if now - st["last_trade_ts"] < cfg.lockout_min * 60:
            sisa = cfg.lockout_min - (now - st["last_trade_ts"]) / 60
            log.info("%s -> ditahan: lockout re-entry, sisa %.0f menit", tag, sisa)
            return
        window_s = cfg.entry_window_hours * 3600
        st["entries"] = [t for t in st["entries"] if now - t < window_s]
        if len(st["entries"]) >= cfg.max_entries_window:
            log.info("%s -> ditahan: batas %d entry per %g jam tercapai", tag, cfg.max_entries_window,
                     cfg.entry_window_hours)
            return
        open_count = sum(
            1 for s in self.state["symbols"].values()
            if s.get("position") is not None or s.get("buy_pending")
        )
        if open_count >= cfg.max_open:
            log.info("%s -> ditahan: posisi terbuka sudah %d (maks %d)", tag, open_count, cfg.max_open)
            return

        # ---- konteks 4H & 1H -------------------------------------------------
        c4 = self.fetch_candles(sym, "4h")
        c1 = self.fetch_candles(sym, "1h")
        c4_ctx = closed_only(c4, TF_MS["4h"], now_ms) if cfg.context_use_closed else c4
        c1_ctx = closed_only(c1, TF_MS["1h"], now_ms) if cfg.context_use_closed else c1
        r4 = last_val(rsi_series([c[4] for c in c4_ctx], cfg.rsi_period))
        r1 = last_val(rsi_series([c[4] for c in c1_ctx], cfg.rsi_period))
        if r4 is None or r1 is None:
            log.warning("%s: RSI 4H/1H belum bisa dihitung", tag)
            return
        if not (r4 < cfg.rsi_4h_max and r1 < cfg.rsi_1h_max):
            log.info("%s -> ditolak: konteks belum oversold (RSI 4H %.1f, 1H %.1f)", tag, r4, r1)
            return

        # ---- take profit dinamis 1-3% ---------------------------------------
        atr = atr_pct(closed_only(c1, TF_MS["1h"], now_ms))
        if atr is None:
            tp_pct = cfg.tp_min
        else:
            tp_pct = min(cfg.tp_max, max(cfg.tp_min, atr * cfg.tp_atr_mult))
        tp_pct = round(tp_pct, 2)

        # ---- ukuran posisi: POSITION_PCT dari equity -------------------------
        price = self.last_price(sym)
        quote_free = self.broker.quote_free()
        equity = quote_free
        for s, ss in self.state["symbols"].items():
            if ss["position"]:
                equity += ss["position"]["qty"] * self.last_price(s)
        cost = equity * cfg.position_pct / 100.0
        if quote_free < cost:
            log.info("%s -> ditolak: saldo %s bebas %.2f < kebutuhan %.2f", tag, cfg.quote, quote_free, cost)
            return
        mc = self.min_cost(sym)
        if mc and cost < mc:
            log.info("%s -> ditolak: nilai order %.2f di bawah minimum notional %.2f", tag, cost, mc)
            return

        self._begin_buy(sym, st, cost, price, tp_pct, r4, r1, prev, last, now)

    # ---- loop utama --------------------------------------------------------
    def _handle_error(self, sym: str, e: Exception) -> None:
        if NETWORK_ERRORS and isinstance(e, NETWORK_ERRORS):
            log.warning("%s: gangguan jaringan/exchange (%s), coba lagi nanti", sym, type(e).__name__)
            return
        log.exception("%s: error", sym)
        key = f"{sym}:{type(e).__name__}"
        now = time.time()
        if now - self._err_ts.get(key, 0) > 1800:  # maksimal 1 notifikasi error / 30 menit / jenis
            self._err_ts[key] = now
            self.notifier.send(f"⚠️ Error {sym}: {type(e).__name__}: {str(e)[:200]}")

    def run(self) -> None:
        cfg = self.cfg
        mode = "DRY RUN (simulasi)" if cfg.dry_run else ("LIVE (TESTNET)" if cfg.testnet else "LIVE")
        self.notify(
            f"🚀 Bot dimulai - mode {mode}\n"
            f"Pair: {', '.join(cfg.symbols)}\n"
            f"Entry: RSI 4H<{cfg.rsi_4h_max:g} & 1H<{cfg.rsi_1h_max:g}, 15m <{cfg.rsi_oversold:g} → >{cfg.rsi_trigger:g}\n"
            f"Posisi {cfg.position_pct:g}% equity | TP {cfg.tp_min:g}-{cfg.tp_max:g}% | SL {cfg.sl_pct:g}% | "
            f"cooldown {cfg.cooldown_min:g} mnt\n"
            f"Exit: {'OCO di Binance' if (cfg.use_oco and not cfg.dry_run) else 'dipantau bot'}"
        )
        while self.running:
            for sym in cfg.symbols:
                if not self.running:
                    break
                try:
                    self.process(sym)
                except Exception as e:
                    self._handle_error(sym, e)
            try:
                self.save()
            except Exception:
                log.exception("Gagal menyimpan state")
            end = time.time() + cfg.loop_seconds
            while self.running and time.time() < end:
                time.sleep(min(1.0, max(0.0, end - time.time())))
        self.save()
        self.notify("🛑 Bot dihentikan. Posisi terbuka tidak otomatis dijual. Posisi dengan OCO tetap "
                    "dijaga Binance; posisi tanpa OCO tidak terpantau selama bot mati.")


# =============================================================================
# Startup
# =============================================================================
def setup_logging(path: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    fh = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def resolve_symbols(ex: Any, raw: List[str], quote: str) -> List[str]:
    out: List[str] = []
    for s in raw:
        key = s.upper().replace("-", "/").replace("_", "/")
        sym: Optional[str] = None
        if "/" in key and key in ex.markets:
            sym = key
        else:
            cands = ex.markets_by_id.get(key.replace("/", "")) or []
            for m in cands:
                if m.get("spot"):
                    sym = m["symbol"]
                    break
        if not sym:
            raise ValueError(f"Pair '{s}' tidak ditemukan di Binance spot")
        m = ex.markets[sym]
        if m.get("quote") != quote:
            raise ValueError(f"Pair {sym} bukan quote {quote} (set QUOTE_ASSET jika memang ingin quote lain)")
        if m.get("active") is False:
            raise ValueError(f"Pair {sym} sedang tidak aktif diperdagangkan")
        if sym not in out:
            out.append(sym)
    return out


def load_state(path: str, cfg: Config) -> dict:
    state: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            log.exception("state.json rusak, membuat state baru (backup: %s.bak)", path)
            os.replace(path, path + ".bak")
            state = {}
    state.setdefault("paper_quote", cfg.dry_run_balance)
    state.setdefault("symbols", {})
    for sym in cfg.symbols:
        base = new_symbol_state()
        base.update(state["symbols"].get(sym, {}))
        state["symbols"][sym] = base
    return state


def main() -> int:
    global NETWORK_ERRORS
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    try:
        cfg = Config.from_env()
    except ValueError as e:
        print(e)
        return 2

    setup_logging(cfg.log_file)

    import ccxt

    NETWORK_ERRORS = (ccxt.NetworkError,)
    ex = ccxt.binance({
        "apiKey": cfg.api_key or None,
        "secret": cfg.api_secret or None,
        "enableRateLimit": True,
        "options": {"defaultType": "spot", "adjustForTimeDifference": True},
    })
    if cfg.testnet:
        ex.set_sandbox_mode(True)
    ex.load_markets()

    try:
        cfg.symbols = resolve_symbols(ex, cfg.symbols, cfg.quote)
    except ValueError as e:
        log.error("%s", e)
        return 2

    state = load_state(cfg.state_file, cfg)
    broker: Any = PaperBroker(state, cfg.paper_fee_pct) if cfg.dry_run else LiveBroker(ex, cfg.quote)
    notifier = Notifier(cfg.tg_token, cfg.tg_chat)
    if bool(cfg.tg_token) != bool(cfg.tg_chat):
        log.warning("TELEGRAM_BOT_TOKEN dan TELEGRAM_CHAT_ID harus diisi keduanya; notifikasi dinonaktifkan")

    if not cfg.dry_run:
        try:
            log.info("Koneksi API OK. Saldo %s bebas: %.2f", cfg.quote, broker.quote_free())
        except Exception as e:
            log.error("Gagal membaca saldo (cek API key/izin/IP whitelist): %s", type(e).__name__)
            return 2

    bot = Bot(cfg, ex, broker, notifier, state)

    # Rekonsiliasi: posisi di state tapi koinnya sudah tidak ada di akun (mis. dijual manual)
    if not cfg.dry_run:
        for sym, st in state["symbols"].items():
            # Posisi ber-OCO dicek statusnya di loop utama (koinnya terkunci di order, bukan "free")
            if (st["position"] and not st["position"].get("oco")
                    and broker.base_free(sym) < st["position"]["qty"] * 0.5):
                st["status"] = "reconciliation_required"
                st["reconciliation_reason"] = "saldo bebas jauh di bawah kuantitas posisi saat startup"
                bot.notify(
                    f"🚨 {sym}: saldo tidak cocok dengan state. Posisi TIDAK dihapus; "
                    f"entry baru diblokir sampai rekonsiliasi manual."
                )
                bot.save()

    def _stop(signum, frame):
        log.info("Sinyal berhenti diterima, menutup bot...")
        bot.running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
