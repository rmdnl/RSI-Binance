"""Uji backtest.py tanpa jaringan: mode candle berjalan vs tertutup, formula net return, dan CLI.

Data sintetis dibangun pada granularitas sub-candle (5m) dan dimulai tepat di batas jam sehingga
setiap bucket 1 jam berisi 12 sub-candle (indeks 0..11, indeks 11 = sub terakhir / candle close).
RSI Wilder periode 14 butuh lompatan cukup besar untuk naik dari oversold ke atas trigger, jadi
profil harga di sini memakai lompatan ~4% pada sub pertama candle berjalan.
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest as B  # noqa: E402

STEP_S = 300
STEP_MS = STEP_S * 1000
SUBS_PER_HOUR = 12
START_MS = 1_699_999_200_000  # t % 3_600_000 == 0 -> selaras batas candle 1 jam Binance
DECLINE_PCT = 0.004
JUMP = (0.04,)  # lompatan sub pertama candle berjalan


def ns(**kw) -> argparse.Namespace:
    d = dict(period=14, step_s=STEP_S, fee=0.1, slip=0.05, pos=30.0, trigger=32.0, oversold=18.0,
             rearm=50.0, lookback=12, lockout=120.0, max_entries=2, window_hours=24.0,
             sl=5.0, tp_min=4.0, tp_max=4.0, tp_atr=1.5, cooldown=240.0)
    d.update(kw)
    return argparse.Namespace(**d)


def _candle(t: int, price: float, band: float = 0.001) -> list:
    return [t, price, price * (1 + band), price * (1 - band), price, 1.0]


def synth_subs(up_sub=JUMP, after_pct: float = 0.0, tail_path=(0.0,), decline_1h: int = 300,
               band: float = 0.001) -> list:
    """`decline_1h` candle 1 jam turun 0.4% (datar di dalam jam), lalu satu candle 1 jam BERJALAN:
    sub ke-i bergerak `up_sub[i]` (sisanya `after_pct`). Setelah itu `tail_path` menentukan
    pergerakan tiap sub-candle berikutnya (satu entri = satu sub 5m).
    """
    subs: list = []
    t = START_MS
    price = 60000.0
    for _ in range(decline_1h):
        for _ in range(SUBS_PER_HOUR):
            subs.append(_candle(t, price, band))
            t += STEP_MS
        price *= (1 - DECLINE_PCT)
    for k in range(SUBS_PER_HOUR):
        pct = up_sub[k] if k < len(up_sub) else after_pct
        price *= (1 + pct)
        subs.append(_candle(t, price, band))
        t += STEP_MS
    for pct in tail_path:
        price *= (1 + pct)
        subs.append(_candle(t, price, band))
        t += STEP_MS
    return subs


def live_signal_indices(subs: list, a, F: dict) -> list:
    """Indeks sub-candle yang memenuhi syarat cross-up mode live."""
    out = []
    for i in range(len(subs)):
        kc = F["kc"][i]
        if F["last_sub"][i] or kc < 1:
            continue
        last = B.live_rsi(F, i, a.period)
        prev = F["rsi_candle"][kc - 1]
        look = [x for x in F["rsi_candle"][max(0, kc - a.lookback):kc] if x is not None]
        if prev is not None and last is not None and prev <= a.trigger < last and look and min(look) < a.oversold:
            out.append(i)
    return out


def live_signal_index(subs: list, a, F: dict) -> int:
    idx = live_signal_indices(subs, a, F)
    assert idx, "cross-up mode live tidak ditemukan pada data sintetis"
    return idx[0]


def test_cli_help_parses_without_format_error():
    with pytest.raises(SystemExit) as e:
        B.main(["--help"])
    assert e.value.code == 0


def test_clean_dedupes_and_drops_open_sub_candle():
    rows = [_candle(START_MS, 1.0), _candle(START_MS + STEP_MS, 1.0), _candle(START_MS + STEP_MS, 99.0),
            _candle(START_MS + 2 * STEP_MS, 2.0)]
    out = B.clean(rows, STEP_S, now_ms=START_MS + 3 * STEP_MS - 1)
    assert [k[0] for k in out] == [START_MS, START_MS + STEP_MS]
    assert out[-1][4] == 99.0  # baris duplikat terakhir yang menang


def test_live_rsi_uses_last_closed_candle_state_not_future_bucket_close():
    """close agregat bucket berjalan (data masa depan) tidak boleh memengaruhi RSI berjalan."""
    a = ns()
    subs = synth_subs(after_pct=0.0, tail_path=(0.0,) * 24)
    F = B.build_features(subs, a.period, a.step_s)
    i = len(subs) - 1
    kp = F["kc"][i] - 1
    d = F["sub_cl"][i] - F["cl"][kp]
    gains = (F["g"][kp] * (a.period - 1) + max(d, 0.0)) / a.period
    losses = (F["l"][kp] * (a.period - 1) + max(-d, 0.0)) / a.period

    assert B.live_rsi(F, i, a.period) == pytest.approx(100.0 * gains / (gains + losses), abs=1e-12)


def test_live_mode_enters_inside_running_candle_then_hits_sl():
    a = ns()
    subs = synth_subs(after_pct=0.0, tail_path=(-0.01,) * 36)
    F = B.build_features(subs, a.period, a.step_s)
    i0 = live_signal_index(subs, a, F)

    assert not F["last_sub"][i0], "entry mode live tidak boleh menunggu sub terakhir candle"
    assert F["rsi_candle"][F["kc"][i0] - 1] < a.trigger, "candle 1 jam tertutup belum cross"
    assert B.live_rsi(F, i0, a.period) > a.trigger

    res = B.simulate(subs, F, 0, "live", a)

    assert res["signals"] == 1
    assert len(res["trades"]) == 1
    ts, reason, ret = res["trades"][0]
    assert reason == "SL"
    assert ts == subs[i0][0] // 1000 + a.step_s
    expected = (1 - a.sl / 100.0) * (1 - a.slip / 100.0) * (1 - a.fee / 100.0) ** 2 - 1.0
    assert ret == pytest.approx(expected, abs=1e-12)
    assert res["equity"] == pytest.approx(1.0 + a.pos / 100.0 * expected, abs=1e-12)


def test_live_mode_hits_fixed_tp_even_when_atr_large():
    a = ns()
    subs = synth_subs(after_pct=0.01, tail_path=(0.01,) * 24, band=0.03)
    F = B.build_features(subs, a.period, a.step_s)
    i0 = live_signal_index(subs, a, F)

    res = B.simulate(subs, F, 0, "live", a)

    assert not F["last_sub"][i0]
    ts, reason, ret = res["trades"][0]
    assert reason == "TP"
    expected = (1 + a.tp_max / 100.0) * (1 - a.fee / 100.0) ** 2 - 1.0
    assert ret == pytest.approx(expected, abs=1e-12)


def test_live_mode_atr_can_raise_tp_above_min():
    a = ns(tp_atr=1.5, tp_min=1.0, tp_max=10.0)
    subs = synth_subs(after_pct=0.01, tail_path=(0.01,) * 24, band=0.03)
    F = B.build_features(subs, a.period, a.step_s)
    i0 = live_signal_index(subs, a, F)
    price = subs[i0][4]
    kp = F["kc"][i0] - 1
    tr = max(F["run_hi"][i0] - F["run_lo"][i0], abs(F["run_hi"][i0] - F["cl"][kp]),
             abs(F["run_lo"][i0] - F["cl"][kp]))
    atrp = (F["atr_abs"][kp] * (a.period - 1) + tr) / a.period / price * 100.0
    expected_tp = round(min(a.tp_max, max(a.tp_min, atrp * a.tp_atr)), 2)
    assert expected_tp > a.tp_min, "band 3% harus menghasilkan TP ATR di atas tp_min"

    res = B.simulate(subs, F, 0, "live", a)

    ts, reason, ret = res["trades"][0]
    assert reason == "TP"
    expected = (1 + expected_tp / 100.0) * (1 - a.fee / 100.0) ** 2 - 1.0
    assert ret == pytest.approx(expected, abs=1e-12)


def test_closed_mode_ignores_cross_that_reverses_before_candle_close():
    a = ns()
    # satu sub melonjak (memicu cross live) lalu jatuh 2%/sub sampai candle 1 jam itu ditutup
    subs = synth_subs(after_pct=-0.02, tail_path=(0.0,) * 24)
    F = B.build_features(subs, a.period, a.step_s)

    live = B.simulate(subs, F, 0, "live", a)
    closed = B.simulate(subs, F, 0, "closed", a)

    assert live["signals"] >= 1, "mode live harus melihat cross di dalam candle"
    assert live["trades"], "mode live harus membuka posisi"
    assert closed["signals"] == 0, "cross yang batal sebelum close tidak boleh terlihat mode closed"
    assert closed["trades"] == []


def test_closed_mode_enters_only_at_candle_close():
    a = ns()
    subs = synth_subs(after_pct=0.01, tail_path=(0.01,) * 24)
    F = B.build_features(subs, a.period, a.step_s)

    res = B.simulate(subs, F, 0, "closed", a)

    assert res["trades"], "candle 1 jam yang ditutup di atas trigger harus memicu entry"
    ts, reason, ret = res["trades"][0]
    assert ts % STEP_S == 0 or ts % 3600 == 0, "exit dicatat pada boundary sub-candle"
    assert reason in ("TP", "SL")


def test_live_mode_counts_lockout_blocks():
    a = ns()
    # candle A: lompat +4% (entry), lalu jatuh 3%/sub sampai SL; candle B: lompat +4% lagi
    # -> cross kedua harus diblok lockout/need_reset
    subs = synth_subs(after_pct=-0.03, tail_path=(0.25,) + (0.0,) * 23)
    F = B.build_features(subs, a.period, a.step_s)
    a_locked = ns(cooldown=0.0, lockout=100000.0)

    res = B.simulate(subs, F, 0, "live", a_locked)

    assert len(res["trades"]) == 1, "trade pertama kena SL"
    assert res["blocked"] >= 1, "cross berikutnya harus diblok lockout/need_reset"


def test_main_prints_mode_and_fixed_tp_sl_from_csv(tmp_path, capsys):
    subs = synth_subs(after_pct=0.0, tail_path=(-0.01,) * 36, decline_1h=350)
    path = tmp_path / "synth_5m.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(subs)

    assert B.main(["--symbol", "TESTUSDT", "--data-csv", str(path),
                   "--days", "20", "--warmup-days", "1", "--mode", "live"]) == 0
    out_live = capsys.readouterr().out
    assert "candle 1 jam BERJALAN" in out_live
    assert "TP 4-4%" in out_live and "SL 5%" in out_live
    assert "cross  ditahan" in out_live

    assert B.main(["--symbol", "TESTUSDT", "--data-csv", str(path),
                   "--days", "20", "--warmup-days", "1", "--mode", "closed"]) == 0
    out_closed = capsys.readouterr().out
    assert "candle 1 jam tertutup saja" in out_closed