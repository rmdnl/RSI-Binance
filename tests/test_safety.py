import pytest
from bot import LiveBroker, rsi_series


def test_rsi_monotonic_up_is_100():
    assert rsi_series(list(range(100, 116)), 14)[-1] == 100.0


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
        from bot import Config
        Config.from_env()
