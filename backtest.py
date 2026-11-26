#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest strategi bot.py (RSI 1 jam) pada data historis Binance.

Data    : klines publik Binance pada granularitas sub-candle (default 5m). Candle 1 jam
          dibentuk dari sub-candle sehingga perilaku "candle berjalan" (realtime) dapat
          disimulasikan: RSI candle 1 jam yang sedang berjalan dihitung ulang di setiap
          sub-candle dengan close = sub-candle terkini, sama seperti bot yang mengevaluasi
          ulang setiap EVAL_SECONDS.
Logika  : memakai fungsi RSI dari bot.py. Aturan sama: arming di bawah RSI_OVERSOLD, entry
          saat RSI menembus RSI_TRIGGER, TP/SL, cooldown setelah SL, reset RSI, lockout
          re-entry, dan batas entry per jendela.

Mode    : --mode live   (default) sinyal dievaluasi di dalam candle 1 jam (SIGNAL_USE_CLOSED=false)
          --mode closed            sinyal hanya dari candle 1 jam yang sudah tertutup

Contoh  : python backtest.py --symbol BTCUSDT --days 365
          python backtest.py --symbol BTCUSDT --days 365 --mode closed
          python backtest.py --symbol BTCUSDT --days 365 --tp-min 4 --tp-max 4 --sl 5

Asumsi  : entry di harga close sub-candle sinyal; SL diperiksa lebih dulu daripada TP dalam
          satu sub-candle (konservatif); fee per sisi dan slippage SL dapat diatur.
          Sinyal hanya memakai data yang sudah diketahui bot saat itu: RSI candle berjalan
          dihitung dari state candle 1 jam tertutup terakhir (Prev RSI, arming, dan ATR tidak
          pernah melihat close candle yang belum selesai).
          Hasil historis tidak menjamin hasil ke depan.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from bot import _rsi_value, rsi_series  # noqa: F401  (rsi_series dipakai tes/analisis)

INTERVAL_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800}
TF1H = 3600
BASES = ["https://data-api.binance.vision", "https://api.binance.com"]


# =============================================================================
# Data
# =============================================================================
def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int, step_s: int) -> List[list]:
    import requests

    last_err: Optional[Exception] = None
    for base in BASES:
        out: List[list] = []
        cur = start_ms
        try:
            while cur < end_ms:
                r = requests.get(
                    f"{base}/api/v3/klines",
                    params={"symbol": symbol, "interval": interval, "startTime": cur,
                            "endTime": end_ms, "limit": 1000},
                    timeout=20,
                )
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    break
                out.extend([[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
                            for x in rows])
                nxt = int(rows[-1][0]) + step_s * 1000
                if nxt <= cur:
                    break
                cur = nxt
                time.sleep(0.15)
            if out:
                return out
        except Exception as e:  # coba endpoint berikutnya
            last_err = e
    raise SystemExit(f"Gagal mengunduh data {symbol} ({type(last_err).__name__ if last_err else 'kosong'}). "
                     f"Pastikan nama pair benar (mis. BTCUSDT) dan host bisa mengakses Binance.")


def load_csv(path: str) -> List[list]:
    out: List[list] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            try:
                out.append([int(float(row[0])), float(row[1]), float(row[2]), float(row[3]),
                            float(row[4]), float(row[5]) if len(row) > 5 else 0.0])
            except (ValueError, IndexError):
                continue  # lewati header
    return out


def clean(sub: List[list], step_s: int, now_ms: Optional[int] = None) -> List[list]:
    seen = {}
    for k in sub:
        seen[k[0]] = k
    out = [seen[t] for t in sorted(seen)]
    if now_ms is not None and out and out[-1][0] + step_s * 1000 > now_ms:
        out = out[:-1]  # buang sub-candle yang masih berjalan
    return out


# =============================================================================
# Fitur: candle 1 jam dari sub-candle, plus RSI "berjalan" per sub-candle
# =============================================================================
def aggregate(sub: List[list], tf_s: int) -> Tuple[list, list, list, list]:
    """Gabungkan sub-candle menjadi candle 1 jam selaras epoch (seperti Binance)."""
    keys: list = []
    hi: list = []
    lo: list = []
    cl: list = []
    for k in sub:
        b = (k[0] // 1000) // tf_s
        if not keys or keys[-1] != b:
            keys.append(b)
            hi.append(k[2])
            lo.append(k[3])
            cl.append(k[4])
        else:
            hi[-1] = max(hi[-1], k[2])
            lo[-1] = min(lo[-1], k[3])
            cl[-1] = k[4]
    return keys, hi, lo, cl


def wilder_states(closes: List[float], period: int) -> Tuple[list, list]:
    n = len(closes)
    g: list = [None] * n
    l: list = [None] * n
    if n <= period:
        return g, l
    sg = sl = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        sg += max(d, 0.0)
        sl += max(-d, 0.0)
    ag, al = sg / period, sl / period
    g[period], l[period] = ag, al
    for i in range(period + 1, n):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0.0)) / period
        al = (al * (period - 1) + max(-d, 0.0)) / period
        g[i], l[i] = ag, al
    return g, l


def atr_abs_series(hi: list, lo: list, cl: list, period: int = 14) -> list:
    """ATR Wilder dalam satuan harga untuk tiap candle 1 jam tertutup."""
    n = len(cl)
    out: list = [None] * n
    if n <= period:
        return out
    trs = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, n)]
    atr = sum(trs[:period]) / period
    out[period] = atr
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i - 1]) / period
        out[i] = atr
    return out


def build_features(sub: List[list], period: int, step_s: int) -> dict:
    keys, hi, lo, cl = aggregate(sub, TF1H)
    g, l = wilder_states(cl, period)
    rsi_candle = [None if g[i] is None else _rsi_value(g[i], l[i]) for i in range(len(cl))]
    atr_abs = atr_abs_series(hi, lo, cl, period)
    kc_list: list = [-1] * len(sub)
    last_sub: list = [False] * len(sub)
    run_hi: list = [0.0] * len(sub)
    run_lo: list = [0.0] * len(sub)
    j = -1
    cur = None
    for i, k in enumerate(sub):
        t = k[0] // 1000
        b = t // TF1H
        if b != cur:
            cur = b
            j += 1
            run_hi[i], run_lo[i] = k[2], k[3]
        else:
            run_hi[i], run_lo[i] = max(run_hi[i - 1], k[2]), min(run_lo[i - 1], k[3])
        kc_list[i] = j
        # sub-candle terakhir dalam bucket 1 jam -> candle 1 jam baru saja close
        last_sub[i] = ((t % TF1H) + step_s) >= TF1H
    return {"keys": keys, "cl": cl, "g": g, "l": l, "rsi_candle": rsi_candle,
            "atr_abs": atr_abs, "kc": kc_list, "last_sub": last_sub,
            "sub_cl": [k[4] for k in sub], "run_hi": run_hi, "run_lo": run_lo}


def live_rsi(F: dict, i: int, period: int) -> Optional[float]:
    """RSI candle 1 jam yang sedang berjalan pada sub-candle i (close = sub close).

    Wilders state diambil dari candle 1 jam TERTUTUP terakhir (kc-1), sama seperti bot yang
    menyusun series = candle tertutup + candle berjalan. Bucket kc sendiri tidak dipakai
    karena close agregatnya belum final (look-ahead).
    """
    kc = F["kc"][i]
    kp = kc - 1
    if kp < period or F["g"][kp] is None:
        return None
    d = F["sub_cl"][i] - F["cl"][kp]
    ng = (F["g"][kp] * (period - 1) + max(d, 0.0)) / period
    nl = (F["l"][kp] * (period - 1) + max(-d, 0.0)) / period
    return _rsi_value(ng, nl)


# =============================================================================
# Simulasi
# =============================================================================
def simulate(sub: List[list], F: dict, start: int, mode: str, a) -> dict:
    n = len(sub)
    step_s = a.step_s
    rsi_candle = F["rsi_candle"]
    lookback = a.lookback
    fee, slip = a.fee / 100.0, a.slip / 100.0

    pos = None
    cooldown_until = 0
    need_reset = False
    last_trade = 0
    entries: List[int] = []
    equity = peak = 1.0
    maxdd = 0.0
    trades: list = []
    signals = blocked = 0

    for i in range(start, n):
        t_open = sub[i][0] // 1000
        t_close = t_open + step_s

        if pos:
            hi, lo = sub[i][2], sub[i][3]
            reason, px = None, 0.0
            if lo <= pos["sl"]:  # SL dicek dulu (konservatif)
                reason, px = "SL", pos["sl"] * (1 - slip)
            elif hi >= pos["tp"]:
                reason, px = "TP", pos["tp"]
            if reason:
                ret = (px / pos["entry"]) * (1 - fee) * (1 - fee) - 1.0
                equity *= 1.0 + a.pos / 100.0 * ret
                peak = max(peak, equity)
                maxdd = max(maxdd, (peak - equity) / peak)
                trades.append((pos["t"], reason, ret))
                last_trade, need_reset = t_close, True
                if reason == "SL":
                    cooldown_until = t_close + a.cooldown * 60
                pos = None
            continue

        if t_close < cooldown_until:
            blocked += 1
            continue

        kc = F["kc"][i]
        if mode == "live":
            if F["last_sub"][i] or kc < 1:
                continue  # tepat di batas candle; bukan skenario "candle berjalan"
            last = live_rsi(F, i, a.period)
        else:
            if not F["last_sub"][i] or kc < 1:
                continue
            last = rsi_candle[kc]
        # prev dan arming selalu dari candle 1 jam yang SUDAH tertutup
        prev = rsi_candle[kc - 1]
        look = [x for x in rsi_candle[max(0, kc - lookback):kc] if x is not None]
        if last is None or prev is None:
            continue

        if need_reset and last >= a.rearm:
            need_reset = False

        if not (prev <= a.trigger < last and look and min(look) < a.oversold):
            continue
        signals += 1

        entries = [t for t in entries if t_close - t < a.window_hours * 3600]
        if need_reset or (t_close - last_trade) < a.lockout * 60 or len(entries) >= a.max_entries:
            blocked += 1
            continue

        price = sub[i][4]
        atrp = None
        if mode == "live":
            kp = kc - 1
            if kp >= 0 and F["atr_abs"][kp] is not None:
                tr = max(F["run_hi"][i] - F["run_lo"][i],
                         abs(F["run_hi"][i] - F["cl"][kp]), abs(F["run_lo"][i] - F["cl"][kp]))
                atrp = (F["atr_abs"][kp] * (a.period - 1) + tr) / a.period / price * 100.0
        elif F["atr_abs"][kc] is not None:
            atrp = F["atr_abs"][kc] / F["cl"][kc] * 100.0
        tp_pct = a.tp_min if atrp is None else min(a.tp_max, max(a.tp_min, atrp * a.tp_atr))
        tp_pct = round(tp_pct, 2)
        pos = {"entry": price, "sl": price * (1 - a.sl / 100.0), "tp": price * (1 + tp_pct / 100.0),
               "t": t_close}
        entries.append(t_close)
        last_trade = t_close

    open_ret = None
    if pos:
        open_ret = sub[-1][4] / pos["entry"] - 1.0
    return {"signals": signals, "blocked": blocked, "trades": trades, "equity": equity,
            "maxdd": maxdd, "open_ret": open_ret}


# =============================================================================
# CLI
# =============================================================================
def ts_str(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backtest bot RSI 1h Binance (realtime atau closed)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--days", type=int, default=365, help="lama periode uji (hari)")
    p.add_argument("--warmup-days", type=int, default=30, help="data tambahan untuk pemanasan RSI")
    p.add_argument("--base-interval", default="5m", choices=sorted(INTERVAL_SECONDS),
                   help="granularitas sub-candle untuk simulasi realtime (default 5m)")
    p.add_argument("--mode", default="live", choices=["live", "closed"],
                   help="live = evaluasi di dalam candle 1 jam (default); closed = hanya candle tertutup")
    p.add_argument("--period", type=int, default=14)
    p.add_argument("--oversold", type=float, default=18)
    p.add_argument("--trigger", type=float, default=32)
    p.add_argument("--lookback", type=int, default=12, help="jumlah candle 1h untuk syarat arming")
    p.add_argument("--rearm", type=float, default=50)
    p.add_argument("--pos", type=float, default=30, help="persen equity per trade")
    p.add_argument("--tp-min", type=float, default=4.0)
    p.add_argument("--tp-max", type=float, default=4.0)
    p.add_argument("--tp-atr", type=float, default=1.5)
    p.add_argument("--sl", type=float, default=5.0)
    p.add_argument("--cooldown", type=float, default=240, help="menit cooldown setelah SL")
    p.add_argument("--lockout", type=float, default=120, help="menit lockout re-entry")
    p.add_argument("--max-entries", type=int, default=2)
    p.add_argument("--window-hours", type=float, default=24)
    p.add_argument("--fee", type=float, default=0.1, help="fee per sisi dalam persen")
    p.add_argument("--slip", type=float, default=0.05, help="slippage SL market dalam persen")
    p.add_argument("--data-csv", help="pakai file CSV sub-candle (ts_ms,o,h,l,c,v)")
    p.add_argument("--save-csv", help="simpan data unduhan ke CSV agar bisa diulang tanpa unduh")
    a = p.parse_args(argv)

    a.step_s = INTERVAL_SECONDS[a.base_interval]

    if a.data_csv:
        sub = clean(load_csv(a.data_csv), a.step_s, now_ms=int(time.time() * 1000))
    else:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (a.days + a.warmup_days) * 86400 * 1000
        print(f"Mengunduh {a.symbol} {a.base_interval} ({a.days + a.warmup_days} hari)...", flush=True)
        sub = clean(fetch_klines(a.symbol.upper(), a.base_interval, start_ms, end_ms, a.step_s), a.step_s,
                    now_ms=end_ms)
        if a.save_csv:
            with open(a.save_csv, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(sub)
    if len(sub) < 500:
        raise SystemExit(f"Data terlalu sedikit ({len(sub)} sub-candle {a.base_interval}).")

    F = build_features(sub, a.period, a.step_s)
    last_s = sub[-1][0] // 1000 + a.step_s
    test_start_s = last_s - a.days * 86400
    start = next((i for i, k in enumerate(sub) if k[0] // 1000 >= test_start_s), len(sub) - 1)
    start = max(start, 200)
    if start < a.period * 3:
        print(f"Peringatan: hanya {start} sub-candle sebelum jendela uji untuk pemanasan RSI; "
              f"nilai RSI awal bisa belum stabil (naikkan --warmup-days).")

    mode_txt = "candle 1 jam BERJALAN (realtime)" if a.mode == "live" else "candle 1 jam tertutup saja"
    print()
    print(f"{a.symbol} | uji {ts_str(sub[start][0] // 1000)} → {ts_str(last_s)} "
          f"({(last_s - sub[start][0] // 1000) / 86400:.0f} hari, {len(sub) - start} sub-candle "
          f"{a.base_interval}) | RSI {a.period} pada {mode_txt}")
    print(f"Arming < {a.oversold:g} (lihat {a.lookback} candle 1h) lalu cross-up > {a.trigger:g} | "
          f"TP {a.tp_min:g}-{a.tp_max:g}% | SL {a.sl:g}% | posisi {a.pos:g}% equity | fee {a.fee:g}%/sisi")
    print()
    head = (f"{'cross':>8} {'ditahan':>8} {'trade':>6} {'TP':>4} {'SL':>4} "
            f"{'win%':>6} {'avg net%':>9} {'ekuitas%':>9} {'maxDD%':>7}")
    print(head)
    print("-" * len(head))

    res = simulate(sub, F, start, a.mode, a)
    tr = res["trades"]
    tp = sum(1 for t in tr if t[1] == "TP")
    sl = len(tr) - tp
    win = f"{tp / len(tr) * 100:.0f}" if tr else "-"
    avg = f"{sum(t[2] for t in tr) / len(tr) * 100:+.2f}" if tr else "-"
    extra = f"  (+1 posisi masih terbuka, {res['open_ret'] * 100:+.2f}%)" if res["open_ret"] is not None else ""
    print(f"{res['signals']:>8} {res['blocked']:>8} {len(tr):>6} {tp:>4} {sl:>4} {win:>6} {avg:>9} "
          f"{(res['equity'] - 1) * 100:>+9.2f} {res['maxdd'] * 100:>7.2f}{extra}")

    i = len(sub) - 1
    live_now = live_rsi(F, i, a.period)
    closed_now = F["rsi_candle"][F["kc"][i]] if F["kc"][i] >= 0 else None
    live_txt = "-" if live_now is None else f"{live_now:.1f}"
    closed_txt = "-" if closed_now is None else f"{closed_now:.1f}"
    print()
    print(f"Kondisi terakhir {a.symbol}: RSI 1h berjalan {live_txt} | "
          f"candle 1h tertutup terakhir {closed_txt}")
    print()
    print("Keterangan: cross = sinyal cross-up setelah arming; ditahan = diblok cooldown/lockout/"
          "batas entry per jendela/reset RSI belum pulih; ekuitas% = hasil kumulatif dengan ukuran "
          "posisi di atas. Mode live mengevaluasi di dalam candle 1 jam sehingga sinyal bisa muncul "
          "beberapa kali lebih banyak daripada mode closed.")
    print("Catatan: entry di close sub-candle sinyal; SL/TP bersamaan dalam satu sub-candle dihitung "
          "SL lebih dulu; maxDD dihitung saat trade ditutup (bukan intratrade); hasil historis tidak "
          "menjamin ke depan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())