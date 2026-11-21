import pytest
from bot import _rsi_value, atr_pct, rsi_series

def test_rsi_monotonic_up_is_100():
    assert rsi_series(list(range(100,116)),14)[-1]==100.0

def test_flat_rsi_is_50():
    assert _rsi_value(0,0)==50.0

def test_atr_true_range_uses_previous_close():
    candles=[[0,10,11,9,10,0],[1,10,13,10,12,0],[2,12,12,8,9,0]]
    assert atr_pct(candles,1)==pytest.approx(4/9*100)

def test_integer_config_rejects_fraction(monkeypatch):
    monkeypatch.setenv('SYMBOLS','BTC/USDT');monkeypatch.setenv('RSI_PERIOD','14.5')
    with pytest.raises(ValueError):
        from bot import Config
        Config.from_env()

def test_nonfinite_config_rejected(monkeypatch):
    monkeypatch.setenv('SYMBOLS','BTC/USDT');monkeypatch.setenv('POSITION_PCT','nan')
    with pytest.raises(ValueError):
        from bot import Config
        Config.from_env()
