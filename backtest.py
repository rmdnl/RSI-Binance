#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest strategi bot.py (RSI 1 jam saja) pada data historis Binance.

Data    : klines 1h publik Binance (tanpa API key). Candle 1h dipakai apa adanya, sama
          seperti bot yang hanya mengevaluasi candle 1h tertutup.
Logika  : memakai fungsi RSI dari bot.py, dan aturan yang sama: arming di bawah RSI_OVERSOLD,
          entry saat RSI 1h menembus ke atas RSI_TRIGGER, TP dinamis 1-3%, SL 2%, cooldown
          setelah SL, reset RSI, lockout re-entry, batas entry per jendela.

Contoh  : python backtest.py --symbol NEARUSDT --days 365
          python backtest.py --symbol NEARUSDT --days 730 --oversold 18 --trigger 32

Asumsi  : entry di harga close candle sinyal; bila SL dan TP sama-sama tersentuh dalam satu
          candle, dianggap SL dulu (konservatif); fee per sisi dan slippage SL bisa diatur.
          Hasil historis tidak menjamin hasil ke depan.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

from bot import rsi_series  # RSI identik dengan yang dipakai bot

TF1H = 3600  # detik
BASES = ["https://data-api.binance.vision", "https://api.binance.com"]


# =============================================================================
# Data
# =============================================================================
def fetch_1h(symbol: str, start_ms: int, end_ms: int) -> List[list]:
    import requests

    last_err: Optional[Exception] = None
    for base in BASES:
        out: List[list] = []
        cur = start_ms
        try:
            while cur < end_ms:
                r = requests.get(
                    f"{base}/api/v3/klines",
                    params={"symbol": symbol, "interval": "1h", "startTime": cur,
                            "endTime": end_ms, "limit": 1000},
                    timeout=20,
                )
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    break
                out.extend([[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
                            for x in rows])
                nxt = int(rows[-1][0]) + TF1H * 1000
                if nxt <= cur:
                    break
                cur = nxt
                time.sleep(0.15)
            if out:
                return out
        except Exception as e:  # coba endpoint berikutnya
            last_err = e
    raise SystemExit(f"Gagal mengunduh data {symbol} ({type(last_err).__name__ if last_err else 'kosong'}). "
                     f"Pastikan nama pair benar (mis. NEARUSDT) dan VPS bisa mengakses Binance.")


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


def clean(c1h: List[list], now_ms: Optional[int] = None) -> List[list]:
    seen = {}
    for k in c1h:
        seen[k[0]] = k
    out = [seen[t] for t in sorted(seen)]
    if now_ms is not None and out and out[-1][0] + TF1H * 1000 > now_ms:
        out = out[:-1]  # buang candle 1h yang masih berjalan
    return out


# =============================================================================
# Fitur / indikator
# =============================================================================
def atr_pct_series(hi: list, lo: list, cl: list, period: int = 14) -> list:
    n = len(cl)
    out: list = [None] * n
    if n <= period:
        return out
    trs = [max(hi[i] - lo[i], abs(hi[i] - cl[i - 1]), abs(lo[i] - cl[i - 1])) for i in range(1, n)]
    atr = sum(trs[:period]) / period
    out[period] = atr / cl[period] * 100.0
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + trs[i - 1]) / period
        out[i] = atr / cl[i] * 100.0
    return out


def build_features(c1h: List[list], a) -> dict:
    closes = [k[4] for k in c1h]
    return {"rsi": rsi_series(closes, a.period),
            "atr": atr_pct_series([k[2] for k in c1h], [k[3] for k in c1h], closes)}


# =============================================================================
# Simulasi
# =============================================================================
def simulate(c1h: List[list], F: dict, start: int, a) -> dict:
    n = len(c1h)
    rsi = F["rsi"]
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
        t_open = c1h[i][0] // 1000
        t_close = t_open + TF1H
        r = rsi[i]

        # Reset hysteresis: RSI 1h harus pulih >= REARM_LEVEL setelah trade terakhir
        if need_reset and t_open >= last_trade and r is not None and r >= a.rearm:
            need_reset = False

        if pos:
            hi, lo = c1h[i][2], c1h[i][3]
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

        prev = rsi[i - 1]
        if r is None or prev is None:
            continue
        look = [x for x in rsi[max(0, i - a.lookback):i] if x is not None]
        if not (prev <= a.trigger < r and look and min(look) < a.oversold):
            continue
        signals += 1

        entries = [t for t in entries if t_close - t < a.window_hours * 3600]
        if need_reset or (t_close - last_trade) < a.lockout * 60 or len(entries) >= a.max_entries:
            blocked += 1
            continue

        price = c1h[i][4]
        atrp = F["atr"][i]
        tp_pct = a.tp_min if atrp is None else min(a.tp_max, max(a.tp_min, atrp * a.tp_atr))
        tp_pct = round(tp_pct, 2)
        pos = {"entry": price, "sl": price * (1 - a.sl / 100.0), "tp": price * (1 + tp_pct / 100.0),
               "t": t_close}
        entries.append(t_close)
        last_trade = t_close

    open_ret = None
    if pos:
        open_ret = c1h[-1][4] / pos["entry"] - 1.0
    return {"signals": signals, "blocked": blocked, "trades": trades, "equity": equity,
            "maxdd": maxdd, "open_ret": open_ret}


# =============================================================================
# CLI
# =============================================================================
def ts_str(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backtest bot RSI 1h multicoin Binance")
    p.add_argument("--symbol", default="NEARUSDT")
    p.add_argument("--days", type=int, default=365, help="lama periode uji (hari)")
    p.add_argument("--warmup-days", type=int, default=30, help="data tambahan untuk pemanasan RSI")
    p.add_argument("--period", type=int, default=14)
    p.add_argument("--oversold", type=float, default=18)
    p.add_argument("--trigger", type=float, default=32)
    p.add_argument("--lookback", type=int, default=12, help="jumlah candle 1h untuk syarat arming")
    p.add_argument("--rearm", type=float, default=50)
    p.add_argument("--pos", type=float, default=30, help="persen equity per trade")
    p.add_argument("--tp-min", type=float, default=1.0)
    p.add_argument("--tp-max", type=float, default=3.0)
    p.add_argument("--tp-atr", type=float, default=1.5)
    p.add_argument("--sl", type=float, default=2.0)
    p.add_argument("--cooldown", type=float, default=240, help="menit cooldown setelah SL")
    p.add_argument("--lockout", type=float, default=120, help="menit lockout re-entry")
    p.add_argument("--max-entries", type=int, default=2)
    p.add_argument("--window-hours", type=float, default=24)
    p.add_argument("--fee", type=float, default=0.1, help="fee per sisi dalam persen")
    p.add_argument("--slip", type=float, default=0.05, help="slippage SL market dalam persen")
    p.add_argument("--data-csv", help="pakai file CSV (ts_ms,o,h,l,c,v) alih-alih mengunduh")
    p.add_argument("--save-csv", help="simpan data unduhan ke CSV agar bisa diulang tanpa unduh")
    a = p.parse_args(argv)

    if a.data_csv:
        c1h = clean(load_csv(a.data_csv), now_ms=int(time.time() * 1000))
    else:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (a.days + a.warmup_days) * 86400 * 1000
        print(f"Mengunduh {a.symbol} 1h ({a.days + a.warmup_days} hari)...", flush=True)
        c1h = clean(fetch_1h(a.symbol.upper(), start_ms, end_ms), now_ms=end_ms)
        if a.save_csv:
            with open(a.save_csv, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(c1h)
    if len(c1h) < 500:
        raise SystemExit(f"Data terlalu sedikit ({len(c1h)} candle 1h).")

    F = build_features(c1h, a)
    last_s = c1h[-1][0] // 1000 + TF1H
    test_start_s = last_s - a.days * 86400
    start = next((i for i, k in enumerate(c1h) if k[0] // 1000 >= test_start_s), len(c1h) - 1)
    start = max(start, a.lookback + a.period + 2)
    if start < 200:
        print(f"Peringatan: hanya {start} candle sebelum jendela uji untuk pemanasan RSI; "
              f"nilai RSI awal bisa belum stabil (disarankan >= 200 candle, mis. --warmup-days 30).")

    print(f"\n{a.symbol} | uji {ts_str(c1h[start][0] // 1000)} → {ts_str(last_s)} "
          f"({(last_s - c1h[start][0] // 1000) / 86400:.0f} hari, {len(c1h) - start} candle 1h) | "
          f"RSI {a.period} pada candle 1h tertutup")
    print(f"Arming < {a.oversold:g} (lihat {a.lookback} candle) lalu cross-up > {a.trigger:g} | "
          f"TP {a.tp_min:g}-{a.tp_max:g}% | SL {a.sl:g}% | posisi {a.pos:g}% equity | fee {a.fee:g}%/sisi\n")
    head = (f"{'cross1h':>8} {'ditahan':>8} {'trade':>6} {'TP':>4} {'SL':>4} "
            f"{'win%':>6} {'avg net%':>9} {'ekuitas%':>9} {'maxDD%':>7}")
    print(head)
    print("-" * len(head))

    res = simulate(c1h, F, start, a)
    tr = res["trades"]
    tp = sum(1 for t in tr if t[1] == "TP")
    sl = len(tr) - tp
    win = f"{tp / len(tr) * 100:.0f}" if tr else "-"
    avg = f"{sum(t[2] for t in tr) / len(tr) * 100:+.2f}" if tr else "-"
    extra = f"  (+1 posisi masih terbuka, {res['open_ret'] * 100:+.2f}%)" if res["open_ret"] is not None else ""
    print(f"{res['signals']:>8} {res['blocked']:>8} {len(tr):>6} {tp:>4} {sl:>4} {win:>6} {avg:>9} "
          f"{(res['equity'] - 1) * 100:>+9.2f} {res['maxdd'] * 100:>7.2f}{extra}")

    i = len(c1h) - 1
    last = F["rsi"][i]
    print(f"\nKondisi terakhir {a.symbol}: RSI 1h {'-' if last is None else f'{last:.1f}'}")
    print("\nKeterangan: cross1h = RSI 1h menembus trigger setelah < oversold pada lookback (hanya dihitung "
          "saat bot tidak memegang posisi/cooldown, seperti bot asli); ditahan = sinyal yang diblok aturan "
          "anti beli beruntun (cooldown, lockout, batas entry per jendela, atau reset RSI belum pulih); "
          "ekuitas% = hasil kumulatif dengan ukuran posisi di atas.")
    print("Catatan: SL/TP bersamaan dalam satu candle dihitung SL; maxDD dihitung saat trade ditutup "
          "(bukan intratrade); hasil historis tidak menjamin ke depan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())