def test_rsi_monotonic:
    from bot import rsi
    assert rsi([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115], 14)[-1] == 100.0


def test_config_validation(tmp_path, monkeypatch):
    from bot import Config
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("BINANCE_API_SECRET", raising=False)
    try:
        Config.from_env()
    except ValueError:
        return
    assert False, "Live config without credentials should fail"
