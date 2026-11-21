import math
import pytest
from bot import Config, _rsi_value, rsi_series


def test_rsi_monotonic_up_is_100():
    values = rsi_series(list(range(100, 116)), 14)
    assert values[-1] == 100.0


def test_flat_rsi_is_50():
    assert _rsi_value(0, 0) == 50.0


def test_live_config_requires_keys(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    with pytest.raises(ValueError, match="API key"):
        Config.from_env()


def test_fractional_integer_setting_rejected(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("RSI_PERIOD", "14.5")
    with pytest.raises(ValueError, match="bilangan bulat"):
        Config.from_env()


def test_nonfinite_config_rejected(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("POSITION_PCT", "nan")
    with pytest.raises(ValueError):
        Config.from_env()
