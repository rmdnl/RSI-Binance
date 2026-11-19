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
  - Anti-whipsaw  : lockout re-entry, wajib "reset" RSI 15m >= REARM_LEVEL setelah trade,
                    dan batas jumlah entry per pair per jendela waktu

Konfigurasi lewat file .env (lihat .env.example).
"""
from __future__ import annotations

import json
import logging
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
        if not (0 < self.position_pct <= 100):
            errs.append("POSITION_PCT harus di antara 0 dan 100")
        if self.tp_min <= 0 or self.tp_max < self.tp_min:
            errs.append("TP_MIN_PCT harus > 0 dan TP_MAX_PCT >= TP_MIN_PCT")
        if self.sl_pct <= 0:
            errs.append("SL_PCT harus > 0")
        if self.rsi_trigger <= self.rsi_oversold:
            errs.append("RSI_TRIGGER harus lebih besar dari RSI_OVERSOLD")
        if self.max_open < 1:
            errs.append("MAX_OPEN_POSITIONS minimal 1")
        if self.arm_lookback < 1:
            errs.append("ARM_LOOKBACK minimal 1")
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

    def buy(self, sym: str, cost: float, last_price: float) -> dict:
        cost = float(self.ex.cost_to_precision(sym, cost))
        order = self._settle(self.ex.create_market_buy_order_with_cost(sym, cost), sym)
        filled = float(order.get("filled") or 0)
        if filled <= 0:
            raise RuntimeError(f"Order beli {sym} tidak terisi: id={order.get('id')} status={order.get('status')}")
        price = float(order.get("average") or last_price)
        spent = float(order.get("cost") or filled * price)
        base = sym.split("/")[0]
        net = filled
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            if fee and fee.get("currency") == base and fee.get("cost"):
                net -= float(fee["cost"])  # fee dibayar dalam koin dasar -> kurangi qty yang bisa dijual
        return {"qty": net, "price": price, "cost": spent}

    def sell(self, sym: str, qty: float, last_price: float) -> Optional[dict]:
        free = self.base_free(sym)
        q = min(qty, free)
        if q <= 0:
            return None
        q = float(self.ex.amount_to_precision(sym, q))
        market = self.ex.market(sym)
        min_amt = ((market.get("limits") or {}).get("amount") or {}).get("min") or 0
        if q <= 0 or q < float(min_amt):
            return None
        order = self._settle(self.ex.create_market_sell_order(sym, q), sym)
        filled = float(order.get("filled") or q)
        price = float(order.get("average") or last_price)
        proceeds = float(order.get("cost") or filled * price)
        fees = order.get("fees") or ([order["fee"]] if order.get("fee") else [])
        for fee in fees:
            if fee and fee.get("currency") == self.quote and fee.get("cost"):
                proceeds -= float(fee["cost"])
        return {"qty": filled, "price": price, "proceeds": proceeds}


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
        price = self.last_price(sym)
        if price <= pos["sl"]:
            self._exit(sym, st, "SL", price, now)
        elif price >= pos["tp"]:
            self._exit(sym, st, "TP", price, now)

    def _exit(self, sym: str, st: dict, reason: str, price: float, now: float) -> None:
        pos = st["position"]
        res = self.broker.sell(sym, pos["qty"], price)
        st["position"] = None
        st["last_trade_ts"] = now
        st["need_reset"] = True

        if res is None:
            st["status"] = "idle"
            self.save()
            self.notify(f"⚠️ {sym}: saldo koin tidak ditemukan saat {reason} (mungkin sudah dijual manual). "
                        f"Status posisi direset ke idle.")
            return

        sold = min(res["qty"], pos["qty"])
        cost_part = pos["cost"] * (sold / pos["qty"]) if pos["qty"] > 0 else pos["cost"]
        pnl = res["proceeds"] - cost_part
        pnl_pct = pnl / cost_part * 100.0 if cost_part else 0.0
        q = self.cfg.quote

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
        open_count = sum(1 for s in self.state["symbols"].values() if s["status"] == "in_position")
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

        res = self.broker.buy(sym, cost, price)
        entry = res["price"]
        pos = {
            "qty": res["qty"],
            "entry": entry,
            "cost": res["cost"],
            "sl": entry * (1 - cfg.sl_pct / 100.0),
            "tp": entry * (1 + tp_pct / 100.0),
            "tp_pct": tp_pct,
            "opened_at": now,
        }
        st["position"] = pos
        st["status"] = "in_position"
        st["last_trade_ts"] = now
        st["entries"].append(now)
        self.save()
        self.notify(
            f"🟢 BUY {sym}{' [DRY RUN]' if cfg.dry_run else ''}\n"
            f"Harga {fp(entry)} | Nilai {res['cost']:.2f} {cfg.quote} ({cfg.position_pct:g}% equity)\n"
            f"TP {fp(pos['tp'])} (+{tp_pct:g}%) | SL {fp(pos['sl'])} (-{cfg.sl_pct:g}%)\n"
            f"RSI 4H {r4:.1f} | 1H {r1:.1f} | 15m {prev:.1f}→{last:.1f}"
        )

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
            f"cooldown {cfg.cooldown_min:g} mnt"
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
        self.notify("🛑 Bot dihentikan. Posisi terbuka (jika ada) TIDAK otomatis dijual.")


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
            if st["position"] and broker.base_free(sym) < st["position"]["qty"] * 0.5:
                st.update(status="idle", position=None, need_reset=True, last_trade_ts=time.time())
                bot.notify(f"⚠️ {sym}: saldo koin tidak sesuai state, posisi direset ke idle.")

    def _stop(signum, frame):
        log.info("Sinyal berhenti diterima, menutup bot...")
        bot.running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
