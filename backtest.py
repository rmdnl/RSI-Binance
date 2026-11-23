#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtest strategi bot.py (konteks RSI 4H + 1H, trigger RSI 15m) pada data historis Binance.

Data    : klines 15m publik Binance (tanpa API key). Candle 1H dan 4H dibentuk dari data 15m,
          sehingga RSI "live" 1H/4H (candle berjalan) sama dengan yang dilihat bot
          (CONTEXT_USE_CLOSED=false). Pakai --closed untuk meniru CONTEXT_USE_CLOSED=true.
Logika  : memakai fungsi RSI dari bot.py, dan aturan yang sama: trigger 15m, TP dinamis 1-3%,
          SL 2%, cooldown setelah SL, reset RSI 15m, lockout re-entry, batas entry per jendela.

Contoh  : python backtest.py --symbol NEARUSDT --days 90
          python backtest.py --symbol NEARUSDT --days 180 --combos 30/30,35/40,40/35,40/40
Format combos = "RSI_4H_MAX/RSI_1H_MAX".

Asumsi  : entry di harga close candle sinyal; bila SL dan TP sama-sama tersentuh dalam satu
          candle 15m, dianggap SL dulu (konservatif); fee per sisi dan slippage SL bisa diatur.
          Hasil historis tidak menjamin hasil ke depan.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from bot import _rsi_value, rsi_series  # RSI identik dengan yang dipakai bot

TF15 = 900  # detik
BASES = ["https://data-api.binance.vision", "https://api.binance.com"]


# =============================================================================
# Data
# =============================================================================
def fetch_15m(symbol: str, start_ms: int, end_ms: int) -> List[list]:
    import requests

    last_err: Optional[Exception] = None
    for base in BASES:
        out: List[list] = []
        cur = start_ms
        try:
            while cur < end_ms:
                r = requests.get(
                    f"{base}/api/v3/klines",
                    params={"symbol": symbol, "interval": "15m", "startTime": cur,
                            "endTime": end_ms, "limit": 1000},
                    timeout=20,
                )
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    break
                out.extend([[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
                            for x in rows])
                nxt = int(rows[-1][0]) + TF15 * 1000
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


def clean(c15: List[list], now_ms: Optional[int] = None) -> List[list]:
    seen = {}
    for k in c15:
        seen[k[0]] = k
    out = [seen[t] for t in sorted(seen)]
    if now_ms is not None and out and out[-1][0] + TF15 * 1000 > now_ms:
        out = out[:-1]  # buang candle 15m yang masih berjalan
    return out


# =============================================================================
# Fitur / indikator
# =============================================================================
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


def aggregate(c15: List[list], tf_s: int):
    """Gabungkan candle 15m ke candle TF lebih besar (selaras epoch UTC, seperti Binance)."""
    keys: list = []
    hi: list = []
    lo: list = []
    cl: list = []
    for k in c15:
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


def htf_context(c15: List[list], tf_s: int, period: int):
    """RSI timeframe besar di setiap close candle 15m.
    live   = candle TF berjalan ikut dihitung (close = harga terkini), seperti bot default
    closed = hanya candle TF yang sudah close
    Return (live, closed, indeks candle TF terakhir yang sudah close, (hi, lo, cl))."""
    _, hi, lo, cl = aggregate(c15, tf_s)
    g, l = wilder_states(cl, period)
    n = len(c15)
    live: list = [None] * n
    closed: list = [None] * n
    kc_list: list = [-1] * n
    j = -1
    cur = None
    for i, k in enumerate(c15):
        t = k[0] // 1000
        b = t // tf_s
        if b != cur:
            cur = b
            j += 1
        last_sub = (t + TF15) % tf_s == 0  # sub-candle terakhir dalam candle TF -> candle TF close
        prev = j - 1
        if last_sub:
            live[i] = _rsi_value(g[j], l[j]) if g[j] is not None else None
        elif prev >= 0 and g[prev] is not None:
            d = k[4] - cl[prev]
            ng = (g[prev] * (period - 1) + max(d, 0.0)) / period
            nl = (l[prev] * (period - 1) + max(-d, 0.0)) / period
            live[i] = _rsi_value(ng, nl)
        kc = j if last_sub else prev
        kc_list[i] = kc
        if kc >= 0 and g[kc] is not None:
            closed[i] = _rsi_value(g[kc], l[kc])
    return live, closed, kc_list, (hi, lo, cl)


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


def build_features(c15: List[list], a) -> dict:
    r15 = rsi_series([k[4] for k in c15], a.period)
    r4_live, r4_closed, _, _ = htf_context(c15, 14400, a.period)
    r1_live, r1_closed, kc1, (h1, l1, c1) = htf_context(c15, 3600, a.period)
    atr_s = atr_pct_series(h1, l1, c1)
    atr1 = [atr_s[kc] if kc >= 0 else None for kc in kc1]
    return {"r15": r15, "r4_live": r4_live, "r4_closed": r4_closed,
            "r1_live": r1_live, "r1_closed": r1_closed, "atr1": atr1}


# =============================================================================
# Simulasi
# =============================================================================
def simulate(c15: List[list], F: dict, start: int, max4: float, max1: float, a) -> dict:
    n = len(c15)
    r15 = F["r15"]
    r4 = F["r4_closed"] if a.closed else F["r4_live"]
    r1 = F["r1_closed"] if a.closed else F["r1_live"]
    fee, slip = a.fee / 100.0, a.slip / 100.0

    pos = None
    cooldown_until = 0
    need_reset = False
    last_trade = 0
    entries: List[int] = []
    equity = peak = 1.0
    maxdd = 0.0
    trades: list = []
    raw = ctx = blocked = 0

    for i in range(start, n):
        t_open = c15[i][0] // 1000
        t_close = t_open + TF15
        r = r15[i]

        # Reset hysteresis: RSI 15m harus pulih >= REARM_LEVEL setelah trade terakhir
        if need_reset and t_open >= last_trade and r is not None and r >= a.rearm:
            need_reset = False

        if pos:
            hi, lo = c15[i][2], c15[i][3]
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

        prev = r15[i - 1]
        if r is None or prev is None:
            continue
        look = [x for x in r15[max(0, i - a.lookback):i] if x is not None]
        if not (prev <= a.trigger < r and look and min(look) < a.oversold):
            continue
        raw += 1

        v4, v1 = r4[i], r1[i]
        if v4 is None or v1 is None or not (v4 < max4 and v1 < max1):
            continue
        ctx += 1

        entries = [t for t in entries if t_close - t < a.window_hours * 3600]
        if need_reset or (t_close - last_trade) < a.lockout * 60 or len(entries) >= a.max_entries:
            blocked += 1
            continue

        price = c15[i][4]
        atrp = F["atr1"][i]
        tp_pct = a.tp_min if atrp is None else min(a.tp_max, max(a.tp_min, atrp * a.tp_atr))
        tp_pct = round(tp_pct, 2)
        pos = {"entry": price, "sl": price * (1 - a.sl / 100.0), "tp": price * (1 + tp_pct / 100.0),
               "t": t_close}
        entries.append(t_close)
        last_trade = t_close

    open_ret = None
    if pos:
        open_ret = c15[-1][4] / pos["entry"] - 1.0
    return {"raw": raw, "ctx": ctx, "blocked": blocked, "trades": trades, "equity": equity,
            "maxdd": maxdd, "open_ret": open_ret}


# =============================================================================
# CLI
# =============================================================================
def parse_combos(s: str) -> List[Tuple[float, float]]:
    out = []
    for part in s.split(","):
        a4, a1 = part.strip().split("/")
        out.append((float(a4), float(a1)))
    return out


def ts_str(t: int) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backtest bot RSI multicoin Binance")
    p.add_argument("--symbol", default="NEARUSDT")
    p.add_argument("--days", type=int, default=90, help="lama periode uji (hari)")
    p.add_argument("--warmup-days", type=int, default=30, help="data tambahan untuk pemanasan RSI 4H")
    p.add_argument("--combos", default="30/30,35/40,40/35,40/40",
                   help="daftar RSI_4H_MAX/RSI_1H_MAX dipisah koma")
    p.add_argument("--closed", action="store_true", help="RSI 4H/1H hanya candle close (CONTEXT_USE_CLOSED=true)")
    p.add_argument("--period", type=int, default=14)
    p.add_argument("--oversold", type=float, default=25)
    p.add_argument("--trigger", type=float, default=32)
    p.add_argument("--lookback", type=int, default=12)
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
    p.add_argument("--fee", type=float, default=0.1, help="fee per sisi (%%)")
    p.add_argument("--slip", type=float, default=0.05, help="slippage SL market (%%)")
    p.add_argument("--data-csv", help="pakai file CSV (ts_ms,o,h,l,c,v) alih-alih mengunduh")
    p.add_argument("--save-csv", help="simpan data unduhan ke CSV agar bisa diulang tanpa unduh")
    a = p.parse_args(argv)

    if a.data_csv:
        c15 = clean(load_csv(a.data_csv), now_ms=int(time.time() * 1000))
    else:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - (a.days + a.warmup_days) * 86400 * 1000
        print(f"Mengunduh {a.symbol} 15m ({a.days + a.warmup_days} hari)...", flush=True)
        c15 = clean(fetch_15m(a.symbol.upper(), start_ms, end_ms), now_ms=end_ms)
        if a.save_csv:
            with open(a.save_csv, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(c15)
    if len(c15) < 2000:
        raise SystemExit(f"Data terlalu sedikit ({len(c15)} candle 15m).")

    F = build_features(c15, a)
    last_s = c15[-1][0] // 1000 + TF15
    test_start_s = last_s - a.days * 86400
    start = next((i for i, k in enumerate(c15) if k[0] // 1000 >= test_start_s), len(c15) - 1)
    start = max(start, a.lookback + a.period + 2)
    if start < 200:
        print(f"Peringatan: hanya {start} candle sebelum jendela uji untuk pemanasan RSI; "
              f"nilai RSI awal bisa belum stabil (disarankan >= 200 candle, mis. --warmup-days 30).")

    print(f"\n{a.symbol} | uji {ts_str(c15[start][0] // 1000)} → {ts_str(last_s)} "
          f"({(last_s - c15[start][0] // 1000) / 86400:.0f} hari, {len(c15) - start} candle 15m) | "
          f"RSI 4H/1H: {'candle close saja' if a.closed else 'candle berjalan (live)'}")
    print(f"TP {a.tp_min:g}-{a.tp_max:g}% | SL {a.sl:g}% | posisi {a.pos:g}% equity | fee {a.fee:g}%/sisi\n")
    head = (f"{'4H/1H':>7} {'cross15m':>9} {'lolos':>6} {'ditahan':>8} {'trade':>6} {'TP':>4} {'SL':>4} "
            f"{'win%':>6} {'avg net%':>9} {'ekuitas%':>9} {'maxDD%':>7}")
    print(head)
    print("-" * len(head))
    for m4, m1 in parse_combos(a.combos):
        res = simulate(c15, F, start, m4, m1, a)
        tr = res["trades"]
        tp = sum(1 for t in tr if t[1] == "TP")
        sl = len(tr) - tp
        win = f"{tp / len(tr) * 100:.0f}" if tr else "-"
        avg = f"{sum(t[2] for t in tr) / len(tr) * 100:+.2f}" if tr else "-"
        extra = f"  (+1 posisi masih terbuka, {res['open_ret'] * 100:+.2f}%)" if res["open_ret"] is not None else ""
        print(f"{m4:g}/{m1:g}".rjust(7) + f" {res['raw']:>9} {res['ctx']:>6} {res['blocked']:>8} {len(tr):>6} "
              f"{tp:>4} {sl:>4} {win:>6} {avg:>9} {(res['equity'] - 1) * 100:>+9.2f} {res['maxdd'] * 100:>7.2f}{extra}")

    i = len(c15) - 1
    f4 = lambda x: "-" if x is None else f"{x:.1f}"  # noqa: E731
    print(f"\nKondisi terakhir {a.symbol}: RSI 15m {f4(F['r15'][i])} | 1H {f4(F['r1_live'][i])} | "
          f"4H {f4(F['r4_live'][i])}")
    print("\nKeterangan: cross15m = RSI 15m menembus trigger setelah <oversold (hanya dihitung saat bot tidak "
          "memegang posisi/cooldown, seperti bot asli); lolos = konteks 4H/1H terpenuhi; ditahan = sinyal "
          "yang lolos konteks tetapi diblok aturan anti beli beruntun (cooldown, lockout, batas entry per "
          "jendela, atau reset RSI belum pulih); ekuitas% = hasil kumulatif dengan ukuran posisi di atas.")
    print("Catatan: SL/TP bersamaan dalam satu candle dihitung SL; maxDD dihitung saat trade ditutup "
          "(bukan intratrade); hasil historis tidak menjamin ke depan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
