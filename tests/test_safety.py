import os
import tempfile

import pytest

from bot import (
    Config,
    LiveBroker,
    ProcessLock,
    _rsi_value,
    atr_pct,
    rsi_series,
)


def test_rsi_monotonic_up_is_100():
    assert rsi_series(list(range(100, 116)), 14)[-1] == 100.0


def test_rsi_flat_is_100_not_50():
    # P0: avg_loss == 0 must yield RSI 100 (Wilder), never the old 50 sentinel.
    assert _rsi_value(0.0, 0.0) == 100.0
    assert rsi_series([100.0] * 30, 14)[-1] == 100.0


def test_rsi_all_losses_is_zero():
    assert _rsi_value(0.0, 5.0) == 0.0


def test_rsi_no_data_returns_none():
    series = rsi_series([100.0] * 14, 14)
    assert all(v is None for v in series)


def _ohlc(o, h, l, c):
    return [0, o, h, l, c, 0.0]


def test_atr_pct_uses_current_price_override():
    # Constant range of 2 on a price of 100 -> ATR% = 2.0 with mean-normalization.
    candles = [_ohlc(100, 101, 99, 100) for _ in range(20)]
    base = atr_pct(candles, period=14)
    assert base is not None
    # Over riding current_price to 200 must halve the numerator-relative percentage.
    overridden = atr_pct(candles, period=14, current_price=200.0)
    assert overridden is not None
    assert overridden == pytest.approx(base / 2, rel=1e-9)


def test_atr_pct_insufficient_data():
    candles = [_ohlc(100, 101, 99, 100) for _ in range(5)]
    assert atr_pct(candles, period=14) is None


def test_process_lock_is_cross_platform():
    # P0: replaces Unix-only fcntl; must not import/raise on Windows.
    with tempfile.TemporaryDirectory() as d:
        state = os.path.join(d, "state.json")
        with ProcessLock(state):
            with pytest.raises(RuntimeError):
                with ProcessLock(state):
                    pass


def test_live_oco_status_unknown_is_fail_closed():
    class Exchange:
        def fetch_open_orders(self, symbol):
            raise RuntimeError("temporary exchange error")

    broker = LiveBroker(Exchange(), "USDT")
    status, result = broker.poll_oco("BTC/USDT", {"tp_id": "1", "sl_id": "2"})
    assert status == "unknown"
    assert result is None


def test_config_fraction_rejected(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("RSI_PERIOD", "14.5")
    with pytest.raises(ValueError):
        Config.from_env()