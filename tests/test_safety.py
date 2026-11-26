import os
import tempfile

import pytest

from bot import (
    KLINE_LIMIT,
    Config,
    LiveBroker,
    ProcessLock,
    _rsi_value,
    atr_pct,
    rsi_series,
)


def test_kline_limit_is_1000():
    """Histori candle 1h per evaluasi: 1000 candle (cukup untuk RSI Wilder + ARM_LOOKBACK)."""
    assert KLINE_LIMIT == 1000


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
    tf_ms = 3_600_000
    history = {"1h": [{"timestamp": now_ms - 2 * tf_ms, "value": 41.5},
                      {"timestamp": now_ms - 40 * 60 * 60 * 1000, "value": 9.9}]}

    rows = update_rsi_history(history, "1h", [], 14, now_ms)

    assert rows == [{"timestamp": now_ms - 2 * tf_ms, "value": 41.5, "live": False}]


def test_scan_entry_records_diagnostics_and_rsi_history():
    from types import SimpleNamespace

    from bot import Bot, new_symbol_state

    now = 1_800_000_000.0
    now_ms = int(now * 1000)
    start = now_ms - 220 * 3_600_000
    candles = [[start + index * 3_600_000, 100 + index, 100 + index, 100 + index, 100 + index, 1]
               for index in range(220)]

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            assert symbol == "BTC/USDT"
            assert timeframe == "1h", "strategi hanya boleh meminta candle 1h"
            assert limit == KLINE_LIMIT, "histori candle 1h harus sepanjang KLINE_LIMIT"
            return candles

    symbol_state = new_symbol_state()
    state = {"symbols": {"BTC/USDT": symbol_state}}
    config = SimpleNamespace(period=14, arm=12, trigger=32, oversold=18, rearm=50, lockout=120,
                             window_hours=24, max_entries=2, max_open=3,
                             use_closed_candles=False, eval_seconds=60.0, history_refresh_seconds=5.0)
    bot = Bot(config, Exchange(), None, None, state)
    bot.save = lambda: None

    bot.scan_entry("BTC/USDT", symbol_state, now)

    diagnostic = symbol_state["diagnostics"]
    assert diagnostic["timeframe"] == "1h"
    assert diagnostic["candle_live"] is False
    assert diagnostic["rsi"] == 100.0
    assert diagnostic["rsi_previous"] == 100.0
    assert diagnostic["candle_ts"] == candles[-1][0]
    assert diagnostic["oversold_seen"] is False
    assert diagnostic["trigger_cross"] is False
    assert diagnostic["oversold"] == 18
    assert diagnostic["trigger"] == 32
    assert "Belum ada RSI 1h" in diagnostic["reason"]
    assert set(symbol_state["rsi_history"]) == {"1h"}
    assert symbol_state["rsi_history"]["1h"]


def test_new_state_drops_legacy_timeframe_history(tmp_path, monkeypatch):
    """State lama menyimpan histori 15m/4h; setelah upgrade hanya 1h yang boleh tersisa."""
    import json

    from bot import Config, new_state

    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("DRY_RUN", "true")
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("STATE_FILE", str(state_path))
    legacy = {"bot_mode": "dry", "symbols": {"BTC/USDT": {
        "rsi_history": {"15m": [{"timestamp": 1, "value": 10.0}],
                        "1h": [{"timestamp": 2, "value": 20.0}],
                        "4h": [{"timestamp": 3, "value": 30.0}]}}}}
    state_path.write_text(json.dumps(legacy), encoding="utf-8")

    state = new_state(str(state_path), Config.from_env())

    assert set(state["symbols"]["BTC/USDT"]["rsi_history"]) == {"1h"}


def test_scan_entry_rsi_1h_cross_opens_paper_position():
    """Jalur entry penuh setelah migrasi ke RSI 1h: arming < 18 lalu cross-up > 32 -> BUY kertas."""
    from types import SimpleNamespace

    from bot import Bot, Notifier, PaperBroker, new_symbol_state

    closes = [60000.0]
    for _ in range(300):
        closes.append(closes[-1] * (1 - 0.004))
    for _ in range(2):
        closes.append(closes[-1] * (1 + 0.02))
    n = len(closes)
    tf = 3_600_000
    now_ms = 1_800_000_000_000
    start = now_ms - n * tf
    candles = [[start + i * tf, c, c * 1.001, c * 0.999, c, 1.0] for i, c in enumerate(closes)]
    now = now_ms / 1000

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            assert timeframe == "1h"
            assert limit == KLINE_LIMIT
            return candles

        def fetch_ticker(self, symbol):
            return {"last": closes[-1]}

        def market(self, symbol):
            return {"limits": {"cost": {"min": 5.0}}}

    state = {"symbols": {"BTC/USDT": new_symbol_state()}, "paper_quote": 1000.0}
    cfg = SimpleNamespace(period=14, arm=12, trigger=32.0, oversold=18.0, rearm=50.0, lockout=120.0,
                          window_hours=24.0, max_entries=2, max_open=3, position_pct=30.0,
                          tp_min=4.0, tp_max=4.0, tp_atr=1.5, sl_pct=5.0, dry_run=True,
                          use_oco=False, cooldown=240.0, quote="USDT", paper_fee=0.1,
                          use_closed_candles=False, eval_seconds=60.0, history_refresh_seconds=5.0)
    symbol_state = state["symbols"]["BTC/USDT"]
    bot = Bot(cfg, Exchange(), PaperBroker(state, cfg.paper_fee), Notifier("", ""), state)
    bot.save = lambda: None
    bot.persist = lambda *a, **k: None

    bot.scan_entry("BTC/USDT", symbol_state, now)

    assert symbol_state["diagnostics"]["trigger_cross"] is True
    assert symbol_state["diagnostics"]["oversold_seen"] is True
    assert symbol_state["status"] == "in_position"
    assert symbol_state["pending_order"] is None
    position = symbol_state["position"]
    assert position["entry"] == closes[-1]
    assert position["sl"] == closes[-1] * (1 - 0.05)
    assert position["tp_pct"] == 4.0
    assert state["paper_quote"] == 700.0
    assert "BUY berhasil dibuka" in symbol_state["diagnostics"]["reason"]


def _realtime_exchange(now_ms: int, live_extra: float, fetch_log: list):
    """Candle 1h: 300 turun + 1 naik (tertutup, RSI ~26.6) lalu satu candle BERJALAN (RSI ~43)."""
    tf = 3_600_000
    closes = [60000.0]
    for _ in range(300):
        closes.append(closes[-1] * (1 - 0.004))
    closes.append(closes[-1] * (1 + 0.02))
    closed_closes = closes[:-1]          # semua kecuali candle berjalan
    live_close = closes[-1] * (1 + live_extra)
    count = len(closed_closes)
    candles = [[now_ms - (count - i) * tf, c, c * 1.001, c * 0.999, c, 1.0]
               for i, c in enumerate(closed_closes)]
    candles.append([now_ms, live_close, live_close * 1.001, live_close * 0.999, live_close, 1.0])

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            fetch_log.append((symbol, timeframe, limit))
            assert limit == KLINE_LIMIT
            return list(candles)

        def fetch_ticker(self, symbol):
            return {"last": live_close}

        def market(self, symbol):
            return {"limits": {"cost": {"min": 5.0}}}

    return Exchange(), candles, live_close


def _realtime_cfg(**kw):
    from types import SimpleNamespace

    base = dict(period=14, arm=12, trigger=32.0, oversold=18.0, rearm=50.0, lockout=120.0,
                window_hours=24.0, max_entries=2, max_open=3, position_pct=30.0,
                tp_min=4.0, tp_max=4.0, tp_atr=1.5, sl_pct=5.0, dry_run=True,
                use_oco=False, cooldown=240.0, quote="USDT", paper_fee=0.1,
                use_closed_candles=False, eval_seconds=60.0, history_refresh_seconds=5.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_scan_entry_uses_running_candle_and_marks_history_live():
    from bot import Bot, Notifier, PaperBroker, new_symbol_state

    now_ms = 1_800_000_000_000
    now = now_ms / 1000
    fetches: list = []
    exchange, candles, live_close = _realtime_exchange(now_ms, 0.02, fetches)
    state = {"symbols": {"BTC/USDT": new_symbol_state()}, "paper_quote": 1000.0}
    symbol_state = state["symbols"]["BTC/USDT"]
    cfg = _realtime_cfg()
    bot = Bot(cfg, exchange, PaperBroker(state, cfg.paper_fee), Notifier("", ""), state)
    bot.save = lambda: None
    bot.persist = lambda *a, **k: None

    bot.scan_entry("BTC/USDT", symbol_state, now)

    diagnostic = symbol_state["diagnostics"]
    assert diagnostic["candle_live"] is True
    assert diagnostic["candle_ts"] == now_ms
    assert symbol_state["status"] == "in_position", diagnostic["reason"]
    assert symbol_state["position"]["entry"] == live_close
    history = symbol_state["rsi_history"]["1h"]
    assert history[-1]["timestamp"] == now_ms, "titik terakhir = candle 1 jam berjalan"
    assert history[-1]["live"] is True, "titik berjalan harus bertanda live"
    assert all(row["live"] is False for row in history[:-1]), "candle tertutup tidak bertanda live"


def test_scan_entry_throttles_and_closed_mode_waits_for_next_candle():
    from bot import Bot, new_symbol_state

    now_ms = 1_800_000_000_000
    now = now_ms / 1000
    for cfg in (_realtime_cfg(), _realtime_cfg(use_closed_candles=True)):
        fetches: list = []
        exchange, _, _ = _realtime_exchange(now_ms, 0.0, fetches)
        state = {"symbols": {"BTC/USDT": new_symbol_state()}, "paper_quote": 1000.0}
        symbol_state = state["symbols"]["BTC/USDT"]
        bot = Bot(cfg, exchange, None, None, state)
        bot.save = lambda: None

        bot.scan_entry("BTC/USDT", symbol_state, now)
        assert len(fetches) == 1
        bot.scan_entry("BTC/USDT", symbol_state, now + 5)  # dalam EVAL_SECONDS
        assert len(fetches) == 1, "evaluasi harus dibatasi EVAL_SECONDS"

        later = now + cfg.eval_seconds + 1
        bot.scan_entry("BTC/USDT", symbol_state, later)
        if cfg.use_closed_candles:
            assert len(fetches) == 1, "mode closed tidak fetch sebelum candle 1 jam baru close"
        else:
            assert len(fetches) == 2, "mode realtime mengevaluasi ulang candle berjalan"


def test_update_rsi_history_marks_live_point_then_drops_flag_after_close():
    from bot import update_rsi_history

    tf = 3_600_000
    base = 1_800_000_000_000 // tf * tf
    candles = [[base - (3 - i) * tf, 100 + i, 101 + i, 99 + i, 100 + i, 1.0] for i in range(3)]
    hist: dict = {}

    points = update_rsi_history(hist, "1h", candles, 2, base + 30 * 60 * 1000, live=(base, 61.5))
    assert points[-1] == {"timestamp": base, "value": 61.5, "live": True}
    assert points[-2]["live"] is False

    closed = candles + [[base, 100.0, 105.0, 99.0, 104.0, 1.0]]
    points = update_rsi_history(hist, "1h", closed, 2, base + tf, live=None)
    assert points[-1]["timestamp"] == base
    assert points[-1]["live"] is False, "setelah candle close penanda live harus hilang"
    assert points[-1]["value"] != 61.5


def test_refresh_live_history_updates_running_point_without_klines():
    from bot import Bot, Notifier, PaperBroker, new_symbol_state

    now_ms = 1_800_000_000_000
    now = now_ms / 1000
    fetches: list = []
    exchange, _, _ = _realtime_exchange(now_ms, 0.02, fetches)
    ticker_calls: list = []
    original_ticker = exchange.fetch_ticker
    exchange.fetch_ticker = lambda symbol: (ticker_calls.append(symbol), original_ticker(symbol))[1]
    state = {"symbols": {"BTC/USDT": new_symbol_state()}, "paper_quote": 1000.0}
    st = state["symbols"]["BTC/USDT"]
    cfg = _realtime_cfg()
    bot = Bot(cfg, exchange, PaperBroker(state, cfg.paper_fee), Notifier("", ""), state)
    bot.save = lambda: None
    bot.persist = lambda *a, **k: None

    bot.scan_entry("BTC/USDT", st, now)
    klines_after_scan = len(fetches)

    bot.refresh_live_history("BTC/USDT", st, now + 5)

    history = st["rsi_history"]["1h"]
    assert history[-1]["timestamp"] == now_ms and history[-1]["live"] is True
    assert len(fetches) == klines_after_scan, "refresh tidak boleh menambah request klines"
    assert ticker_calls, "refresh memakai harga ticker"
    value_after_refresh = history[-1]["value"]

    bot.refresh_live_history("BTC/USDT", st, now + 7)  # < HISTORY_REFRESH_SECONDS
    assert st["rsi_history"]["1h"][-1]["value"] == value_after_refresh

    # evaluasi berikutnya tidak boleh menghapus jejak refresh histori
    bot.scan_entry("BTC/USDT", st, now + cfg.eval_seconds + 1)
    assert st["diagnostics"].get("rsi_live_at") == now + 5

    closed_bot = Bot(_realtime_cfg(use_closed_candles=True), exchange, None, None, state)
    closed_bot.save = lambda: None
    before = len(ticker_calls)
    closed_bot.refresh_live_history("BTC/USDT", st, now + 100)
    assert len(ticker_calls) == before, "mode closed tidak memperbarui titik berjalan"


def test_refresh_live_history_skips_without_cached_candles():
    from bot import Bot, new_symbol_state

    state = {"symbols": {"BTC/USDT": new_symbol_state()}, "paper_quote": 1000.0}
    st = state["symbols"]["BTC/USDT"]
    bot = Bot(_realtime_cfg(), object(), None, None, state)
    bot.save = lambda: None

    bot.refresh_live_history("BTC/USDT", st, 1_800_000_000.0)  # belum ada evaluasi/scan

    assert "rsi_history" not in st or not st["rsi_history"].get("1h")


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


def test_exchange_credentials_never_sent_in_dry_run(monkeypatch):
    """DRY RUN harus bebas kredensial: key invalid pernah menggagalkan load_markets() di VPS."""
    from types import SimpleNamespace

    from bot import exchange_credentials

    dry = SimpleNamespace(dry_run=True, key="k", secret="s")
    assert exchange_credentials(dry) == {"apiKey": None, "secret": None}

    live = SimpleNamespace(dry_run=False, key="k", secret="s")
    assert exchange_credentials(live) == {"apiKey": "k", "secret": "s"}

    live_empty = SimpleNamespace(dry_run=False, key="", secret="")
    assert exchange_credentials(live_empty) == {"apiKey": None, "secret": None}


def test_config_defaults_match_documented_env(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    for name in ("RSI_OVERSOLD", "RSI_TRIGGER", "ARM_LOOKBACK", "REARM_LEVEL", "SIGNAL_USE_CLOSED",
                 "EVAL_SECONDS", "POSITION_PCT",
                 "TP_MIN_PCT", "TP_MAX_PCT", "TP_ATR_MULT", "SL_PCT", "COOLDOWN_MINUTES",
                 "REENTRY_LOCKOUT_MINUTES", "MAX_ENTRIES_PER_WINDOW", "ENTRY_WINDOW_HOURS",
                 "PAPER_FEE_PCT", "MAX_OPEN_POSITIONS", "DRY_RUN_BALANCE"):
        monkeypatch.delenv(name, raising=False)

    cfg = Config.from_env()

    assert (cfg.oversold, cfg.trigger, cfg.arm, cfg.rearm) == (18, 32, 12, 50)
    assert (cfg.tp_min, cfg.tp_max, cfg.tp_atr, cfg.sl_pct) == (4.0, 4.0, 1.5, 5.0)
    assert (cfg.use_closed_candles, cfg.eval_seconds) == (False, 60.0)
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

def test_save_restricts_state_permissions_before_replace(tmp_path, monkeypatch):
    """State memuat saldo/posisi/order: file pengganti tidak boleh mewarisi 0644."""
    import os as os_mod

    from bot import Bot, new_symbol_state

    state_file = str(tmp_path / "state.testnet.json")
    state = {"symbols": {"BTC/USDT": new_symbol_state()}, "paper_quote": 1000.0}
    cfg = _realtime_cfg()
    cfg.state_file = state_file
    bot = Bot(cfg, object(), None, None, state)

    dipanggil = []
    asli = os_mod.chmod

    def chmod_spy(path, mode):
        dipanggil.append((path, mode))
        return asli(path, mode)

    monkeypatch.setattr("bot.os.chmod", chmod_spy)
    bot.save()

    assert dipanggil == [(state_file + ".tmp", 0o600)],         "chmod 0600 harus terjadi pada file sementara sebelum os.replace"
    assert os_mod.path.exists(state_file)


def test_save_state_file_mode_is_600_on_posix(tmp_path):
    import os as os_mod
    import sys as sys_mod

    import pytest as pytest_mod

    if sys_mod.platform == "win32":
        pytest_mod.skip("mode POSIX tidak berlaku di Windows")

    from bot import Bot, new_symbol_state

    state_file = str(tmp_path / "state.testnet.json")
    cfg = _realtime_cfg()
    cfg.state_file = state_file
    bot = Bot(cfg, object(), None, None, {"symbols": {"BTC/USDT": new_symbol_state()}})
    bot.save()
    bot.save()  # save kedua lewat os.replace tidak boleh mengembalikan 0644

    assert os_mod.stat(state_file).st_mode & 0o777 == 0o600

def test_scan_entry_waits_for_rsi_warmup_history():
    """Regresi M1: RSI Wilder belum stabil pada histori pendek, jadi jangan bersinyal."""
    from types import SimpleNamespace

    from bot import RSI_WARMUP, Bot, new_symbol_state

    now = 1_800_000_000.0
    now_ms = int(now * 1000)
    start = now_ms - 120 * 3_600_000
    candles = [[start + index * 3_600_000, 100.0, 100.0, 100.0, 100.0, 1]
               for index in range(120)]
    panggilan = []

    class Exchange:
        def fetch_ohlcv(self, symbol, timeframe, limit):
            panggilan.append(symbol)
            return candles

    symbol_state = new_symbol_state()
    state = {"symbols": {"BTC/USDT": symbol_state}}
    config = SimpleNamespace(period=14, arm=12, trigger=32, oversold=18, rearm=50, lockout=120,
                             window_hours=24, max_entries=2, max_open=3,
                             use_closed_candles=False, eval_seconds=60.0, history_refresh_seconds=5.0)
    bot = Bot(config, Exchange(), None, None, state)
    bot.save = lambda: None

    bot.scan_entry("BTC/USDT", symbol_state, now)

    assert RSI_WARMUP >= 200, "ambang histori harus >= 200 candle"
    assert symbol_state["position"] is None
    assert "belum cukup" in symbol_state["diagnostics"]["reason"]
    assert str(RSI_WARMUP) in symbol_state["diagnostics"]["reason"]
    assert symbol_state["last_eval_at"] == now, "waktu evaluasi harus dicatat agar tidak memanggil klines tiap loop"

    bot.scan_entry("BTC/USDT", symbol_state, now + 1)  # masih dalam EVAL_SECONDS
    assert len(panggilan) == 1, "tidak boleh meminta klines lagi sebelum EVAL_SECONDS"


def test_rsi_warmup_constant_matches_documented_value():
    from bot import RSI_WARMUP

    assert RSI_WARMUP == 200
