"""Uji backtest.py tanpa jaringan: parser CLI, formula net return SL/TP, dan penghitungan blok re-entry.

Candle sintetis dibangun langsung (bukan unduhan) supaya hasilnya deterministik.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import random
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest as B  # noqa: E402

TFS = B.TF1H * 1000
START_MS = 1_700_000_000_000


def ns(**kw) -> argparse.Namespace:
    d = dict(period=14, fee=0.1, slip=0.05, pos=30.0, trigger=32.0, oversold=18.0,
             rearm=50.0, lookback=12, lockout=120.0, max_entries=2, window_hours=24.0,
             sl=2.0, tp_min=1.0, tp_max=3.0, tp_atr=1.5, cooldown=240.0)
    d.update(kw)
    return argparse.Namespace(**d)


def base_closes(tail: int = 60) -> list:
    """Turun panjang (RSI oversold) lalu naik cepat (crossing armed)."""
    closes = [60000.0]
    for _ in range(300):
        closes.append(closes[-1] * (1 - 0.004))
    for _ in range(4):
        closes.append(closes[-1] * (1 + 0.02))
    for _ in range(tail):
        closes.append(closes[-1] * (1 - 0.0005))
    return closes


def to_candles(closes, seed: int = 3) -> list:
    rnd = random.Random(seed)
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) * (1 + abs(rnd.gauss(0, 0.0004)))
        lo = min(prev, c) * (1 - abs(rnd.gauss(0, 0.0004)))
        out.append([START_MS + i * TFS, prev, hi, lo, c, 100.0])
        prev = c
    return out


def cross_index(closes, a) -> int:
    r1h = B.rsi_series(closes, a.period)
    for i in range(1, len(closes)):
        prev, r = r1h[i - 1], r1h[i]
        if prev is None or r is None:
            continue
        look = [x for x in r1h[max(0, i - a.lookback):i] if x is not None]
        if prev <= a.trigger < r and look and min(look) < a.oversold:
            return i
    raise AssertionError("pola crossing armed tidak ditemukan pada data sintetis")


def features(c1h: list, a) -> dict:
    return {"rsi": B.rsi_series([k[4] for k in c1h], a.period), "atr": [None] * len(c1h)}


def test_strategy_uses_single_one_hour_timeframe():
    assert B.TF1H == 3600
    assert not hasattr(B, "TF15")
    assert not hasattr(B, "htf_context")


def test_cli_help_parses_without_format_error():
    """Argparse memperlakukan % pada teks help sebagai format string -> pernah ValueError."""
    with pytest.raises(SystemExit) as e:
        B.main(["--help"])
    assert e.value.code == 0


def test_clean_dedupes_and_drops_running_candle():
    rows = [[START_MS, 1.0, 1.0, 1.0, 0.0, 1.0],
            [START_MS + TFS, 1.0, 1.0, 1.0, 1.0, 1.0],
            [START_MS + TFS, 1.0, 1.0, 1.0, 99.0, 1.0],
            [START_MS + 2 * TFS, 1.0, 1.0, 1.0, 2.0, 1.0]]
    out = B.clean(rows, now_ms=START_MS + 3 * TFS - 1)
    assert [k[0] for k in out] == [START_MS, START_MS + TFS]
    assert out[-1][4] == 99.0  # baris duplikat terakhir yang menang


def test_simulate_sl_return_matches_formula():
    a = ns()
    closes = base_closes()
    i0 = cross_index(closes, a)
    forced = list(closes)
    for k in range(1, 40):
        forced[i0 + k] = forced[i0] * (1 - 0.004 * k)   # jatuh -> SL kena
    c1h = to_candles(forced)
    res = B.simulate(c1h, features(c1h, a), 0, a)
    assert len(res["trades"]) == 1
    ts, reason, ret = res["trades"][0]
    assert reason == "SL"
    assert ts == c1h[i0][0] // 1000 + B.TF1H
    entry = c1h[i0][4]
    fee, slip = a.fee / 100.0, a.slip / 100.0
    expected = (entry * (1 - a.sl / 100.0) * (1 - slip) / entry) * (1 - fee) ** 2 - 1.0
    assert ret == pytest.approx(expected, abs=1e-12)
    assert res["equity"] == pytest.approx(1.0 + a.pos / 100.0 * expected, abs=1e-12)


def test_simulate_tp_return_matches_formula():
    a = ns()
    closes = base_closes()
    i0 = cross_index(closes, a)
    forced = list(closes)
    for k in range(1, 40):
        forced[i0 + k] = forced[i0] * (1 + 0.002 * k)   # naik -> TP kena
    c1h = to_candles(forced)
    res = B.simulate(c1h, features(c1h, a), 0, a)
    ts, reason, ret = res["trades"][0]
    assert reason == "TP"
    entry = c1h[i0][4]
    tp_pct = round(a.tp_min, 2)                        # atr None -> TP = tp_min
    fee = a.fee / 100.0
    expected = (entry * (1 + tp_pct / 100.0) / entry) * (1 - fee) ** 2 - 1.0
    assert ret == pytest.approx(expected, abs=1e-12)


def test_simulate_counts_lockout_blocks_and_respects_max_entries():
    a = ns()
    closes = base_closes()
    i0 = cross_index(closes, a)
    forced = list(closes)
    for k in range(1, 40):
        forced[i0 + k] = forced[i0] * (1 - 0.004 * k)
    c1h = to_candles(forced)
    F = features(c1h, a)
    a_locked = ns(cooldown=0.0, lockout=100000.0)
    res = B.simulate(c1h, F, 0, a_locked)
    assert len(res["trades"]) == 1
    assert res["signals"] >= 1
    assert res["blocked"] >= 1


def test_main_defaults_are_rsi_1h_oversold_18_trigger_32(tmp_path, capsys):
    """End-to-end lewat CSV lokal: default CLI harus oversold 18 / trigger 32 pada candle 1h."""
    rows = to_candles(base_closes(tail=200))
    path = tmp_path / "synth_1h.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)

    assert B.main(["--symbol", "TESTUSDT", "--data-csv", str(path),
                   "--days", "20", "--warmup-days", "1"]) == 0

    out = capsys.readouterr().out
    assert "Arming < 18" in out
    assert "cross-up > 32" in out
    assert "candle 1h" in out
    assert "4H" not in out and "15m" not in out