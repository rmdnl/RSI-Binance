import pytest
from bot import _rsi_value, rsi_series

def test_rsi_monotonic_up_is_100():
    assert rsi_series(list(range(100,116)),14)[-1]==100.0

def test_flat_rsi_is_50():
    assert _rsi_value(0,0)==50.0

def test_fractional_integer_setting_rejected(monkeypatch):
    monkeypatch.setenv('SYMBOLS','BTC/USDT');monkeypatch.setenv('RSI_PERIOD','14.5')
    with pytest.raises(ValueError):
        from bot import Config
        Config.from_env()
