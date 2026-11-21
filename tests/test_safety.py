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

def test_scan_entry_records_rsi_diagnostic_when_oversold_is_absent(monkeypatch):
    from types import SimpleNamespace

    from bot import Bot, new_symbol_state

    now = 1_800_000_000.0
    now_ms = int(now * 1000)
    start = now_ms - 40 * 900_000
    candles = []
    for index in range(40):
        close = 100 + index
        candles.append([start + index * 900_000, close, close, close, close, 1])

    class FakeExchange:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            assert symbol == "BTC/USDT"
            assert timeframe == "15m"
            return candles

    symbol_state = new_symbol_state()
    state = {"symbols": {"BTC/USDT": symbol_state}}
    config = SimpleNamespace(period=14, arm=12, trigger=32, oversold=25)
    bot = Bot(config, FakeExchange(), None, None, state)
    bot.save = lambda: None

    bot.scan_entry("BTC/USDT", symbol_state, now)

    diagnostic = symbol_state["diagnostics"]
    assert diagnostic["rsi_15m"] == 100.0
    assert diagnostic["rsi_1h"] is None
    assert diagnostic["rsi_4h"] is None
    assert diagnostic["oversold_seen"] is False
    assert "Belum ada RSI 15m" in diagnostic["reason"]

