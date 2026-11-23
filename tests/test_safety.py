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


def test_update_rsi_history_keeps_persisted_rows_inside_window():
    # Regression: the persisted-row filter once compared int(bool) against the cutoff,
    # which silently discarded every stored sample on restart.
    from bot import update_rsi_history

    now_ms = 1_800_000_000_000
    tf_ms = 900_000
    history = {"15m": [{"timestamp": now_ms - 2 * tf_ms, "value": 41.5},
                       {"timestamp": now_ms - 40 * 60 * 60 * 1000, "value": 9.9}]}

    rows = update_rsi_history(history, "15m", [], 14, now_ms)

    assert rows == [{"timestamp": now_ms - 2 * tf_ms, "value": 41.5}]


def test_aggregate_candles_builds_higher_timeframe_ohlcv():
    from bot import aggregate_candles

    tf15 = 900_000
    start = 1_800_000_000_000 // tf15 * tf15
    candles = [[start + index * tf15, 10 + index, 12 + index, 9 + index, 11 + index, 1] for index in range(4)]

    hours = aggregate_candles(candles, 3_600_000)

    assert len(hours) == 1
    assert hours[0] == [start, 10.0, 15.0, 9.0, 14.0, 4.0]


def test_scan_entry_records_diagnostics_and_rsi_history():
    from types import SimpleNamespace

    from bot import Bot, new_symbol_state

    now = 1_800_000_000.0
    now_ms = int(now * 1000)
    start = now_ms - 80 * 900_000
    candles = [[start + index * 900_000, 100 + index, 100 + index, 100 + index, 100 + index, 1] for index in range(80)]

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            assert symbol == "BTC/USDT"
            if timeframe == "15m":
                return candles
            if timeframe in ("1h", "4h"):
                return []
            raise AssertionError(f"Unexpected timeframe: {timeframe}")

    symbol_state = new_symbol_state()
    state = {"symbols": {"BTC/USDT": symbol_state}}
    config = SimpleNamespace(period=14, arm=12, trigger=32, oversold=25, rearm=50, lockout=120,
                             window_hours=24, max_entries=2, max_open=3)
    bot = Bot(config, Exchange(), None, None, state)
    bot.save = lambda: None

    bot.scan_entry("BTC/USDT", symbol_state, now)

    diagnostic = symbol_state["diagnostics"]
    assert diagnostic["rsi_15m"] == 100.0
    assert diagnostic["rsi_1h"] == 100.0
    assert diagnostic["rsi_4h"] is None
    assert diagnostic["candle_ts"] == candles[-1][0]
    assert diagnostic["oversold_seen"] is False
    assert diagnostic["trigger_cross"] is False
    assert "Belum ada RSI 15m" in diagnostic["reason"]
    assert symbol_state["rsi_history"]["15m"]
    assert symbol_state["rsi_history"]["1h"]
    assert "4h" in symbol_state["rsi_history"]


def test_update_portfolio_dry_run_writes_dashboard_snapshot(tmp_path, monkeypatch):
    from bot import Bot, Notifier, PaperBroker, new_state

    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("DRY_RUN_BALANCE", "1000")
    cfg = Config.from_env()

    class Exchange:
        def fetch_ticker(self, symbol):
            return {"last": 61000}

    state = new_state(cfg.state_file, cfg)
    state["paper_quote"] = 400.0
    state["symbols"]["BTC/USDT"]["position"] = {"qty": 0.01, "entry": 60000, "cost": 600.0,
                                                "tp": 63000, "sl": 58500}
    bot = Bot(cfg, Exchange(), PaperBroker(state, cfg.paper_fee), Notifier("", ""), state)

    bot.update_portfolio()
    snapshot = state["portfolio_snapshot"]

    assert snapshot["mode"] == "dry"
    assert snapshot["quote"] == "USDT"
    assert snapshot["equity"] == pytest.approx(400.0 + 610.0)
    assert snapshot["unrealized_pnl"] == pytest.approx(10.0)
    assert snapshot["positions"][0]["mark"] == 61000
    assert snapshot["positions"][0]["value"] == pytest.approx(610.0)
    assert {row["asset"] for row in snapshot["assets"]} == {"USDT", "BTC"}
    assert snapshot["unpriced_assets"] == []
    assert snapshot["rate_limit_events"] == 0
    assert snapshot["realized_tracking_since"] is not None


def test_config_defaults_match_documented_env(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    for name in ("RSI_OVERSOLD", "RSI_TRIGGER", "ARM_LOOKBACK", "REARM_LEVEL", "POSITION_PCT",
                 "TP_MIN_PCT", "TP_MAX_PCT", "TP_ATR_MULT", "SL_PCT", "COOLDOWN_MINUTES",
                 "REENTRY_LOCKOUT_MINUTES", "MAX_ENTRIES_PER_WINDOW", "ENTRY_WINDOW_HOURS",
                 "PAPER_FEE_PCT", "MAX_OPEN_POSITIONS", "DRY_RUN_BALANCE"):
        monkeypatch.delenv(name, raising=False)

    cfg = Config.from_env()

    assert (cfg.oversold, cfg.trigger, cfg.arm, cfg.rearm) == (25, 32, 12, 50)
    assert (cfg.tp_min, cfg.tp_max, cfg.tp_atr, cfg.sl_pct) == (1.0, 3.0, 1.5, 2.0)
    assert (cfg.cooldown, cfg.lockout, cfg.max_entries, cfg.window_hours) == (240.0, 120.0, 2, 24.0)
    assert (cfg.position_pct, cfg.max_open, cfg.paper_fee, cfg.dry_balance) == (30.0, 3, 0.1, 1000.0)


def test_startup_dry_run_clears_stale_pending_order(tmp_path, monkeypatch):
    from bot import Bot, Notifier, new_state

    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    cfg = Config.from_env()

    state = new_state(cfg.state_file, cfg)
    state["symbols"]["BTC/USDT"]["pending_order"] = {"type": "buy", "client_id": "stale", "cost": 10.0}
    bot = Bot(cfg, None, None, Notifier("", ""), state)

    bot.reconcile_startup()

    assert state["symbols"]["BTC/USDT"]["pending_order"] is None
    assert any("pending order dry run" in event["message"] for event in state["events"])


def test_partial_oco_fill_fails_closed(tmp_path, monkeypatch):
    import time

    from bot import Bot, Notifier, new_state

    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("BINANCE_API_KEY", "key")
    monkeypatch.setenv("BINANCE_API_SECRET", "secret")
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    cfg = Config.from_env()

    class Broker:
        def poll_oco(self, symbol, oco):
            return "partial", {"leg": "TP", "qty": 0.004, "price": 63000.0, "proceeds": 251.6, "full": False}

    state = new_state(cfg.state_file, cfg)
    symbol_state = state["symbols"]["BTC/USDT"]
    symbol_state["status"] = "in_position"
    symbol_state["position"] = {"qty": 0.01, "entry": 60000.0, "cost": 600.0, "tp": 63000.0, "sl": 58500.0,
                                "oco": {"list_id": "1", "client_list_id": "cid", "tp_id": "11", "sl_id": "12"}}
    bot = Bot(cfg, None, Broker(), Notifier("", ""), state)

    # Leg OCO yang terisi sebagian bisa menyisakan kuantitas aktif di exchange:
    # kuantitas posisi tidak boleh dikurangi dan pair harus menunggu rekonsiliasi manual.
    bot.check_exit("BTC/USDT", symbol_state, time.time())

    assert symbol_state["reconciliation_required"] is True
    assert symbol_state["status"] == "reconciliation"
    assert symbol_state["position"]["oco_pending"] is True
    assert symbol_state["position"]["qty"] == 0.01
    assert symbol_state["position"]["oco"] is not None
    assert any("fill parsial" in event["message"] for event in state["events"])