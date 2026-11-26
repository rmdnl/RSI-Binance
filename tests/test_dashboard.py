"""Tes kontrak halaman dashboard: warna garis RSI 1h, alert oversold, dan status payload."""
import json
import re

import dashboard


def test_single_timeframe_color_is_hex():
    colors = dashboard.RSI_TF_COLORS
    assert set(colors) == {"1h"}
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", colors["1h"]), colors["1h"]


def test_pair_dash_palette_starts_solid_and_is_distinct():
    dashes = dashboard.RSI_PAIR_DASHES
    assert dashes[0] == "", "pair pertama tetap solid"
    assert len(set(dashes)) == len(dashes)
    assert all(re.fullmatch(r"(\d+( \d+)*)?", d) for d in dashes)


def test_page_injects_color_map_without_leftover_placeholder():
    assert "__TF_COLORS__" not in dashboard.PAGE.split("PAGE.replace", 1)[0]
    assert json.dumps(dashboard.RSI_TF_COLORS) in dashboard.PAGE
    assert json.dumps(dashboard.RSI_PAIR_DASHES) in dashboard.PAGE


def test_every_rsi_line_gets_explicit_stroke():
    """Regresi: create(R.Line, ...) tanpa prop stroke membuat semua garis sewarna (default Recharts)."""
    assert "stroke:TF_COLORS[tf]" in dashboard.PAGE
    assert "DASHES[i%DASHES.length]" in dashboard.PAGE
    assert re.search(r"create\(R\.Line,\{[^}]*stroke:line\.stroke", dashboard.PAGE)
    assert re.search(r"create\(R\.Line,\{[^}]*strokeDasharray:line\.dash", dashboard.PAGE)


def test_page_only_renders_one_hour_timeframe():
    assert "TF_ORDER=['1h']" in dashboard.PAGE
    assert "15m" not in dashboard.PAGE
    assert "4h" not in dashboard.PAGE


def _diag(rsi, oversold=18.0, trigger=32.0, **extra):
    row = {"rsi": rsi, "candle_ts": 1_800_000_000_000, "evaluated_at": 1_800_000_100.0,
           "oversold": oversold, "trigger": trigger, "oversold_seen": False}
    row.update(extra)
    return row


def _state(symbols):
    return {"bot_mode": "testnet", "symbols": symbols}


def test_alert_exposes_candle_mode_flag():
    state = _state({"BTC/USDT": {"diagnostics": _diag(16.0, candle_live=True)},
                    "ETH/USDT": {"diagnostics": _diag(16.5, candle_live=False)}})
    rows = {a["symbol"]: a for a in dashboard.rsi_oversold_alerts(state)}
    assert rows["BTC/USDT"]["candle_live"] is True
    assert rows["ETH/USDT"]["candle_live"] is False


def test_page_shows_candle_mode_column():
    assert "<th>Candle</th>" in dashboard.PAGE
    assert "z.candle_live===true?'berjalan'" in dashboard.PAGE
    assert "a.candle_live?" in dashboard.PAGE


def test_alert_flags_symbol_below_oversold():
    state = _state({"BTC/USDT": {"diagnostics": _diag(16.4)}})
    alerts = dashboard.rsi_oversold_alerts(state)
    assert [a["symbol"] for a in alerts] == ["BTC/USDT"]
    assert alerts[0]["rsi"] == 16.4
    assert alerts[0]["oversold"] == 18.0
    assert alerts[0]["source"] == "state"
    assert alerts[0]["candle_ts"] == 1_800_000_000_000


def test_alert_uses_strict_below_comparison():
    state = _state({
        "BTC/USDT": {"diagnostics": _diag(18.0)},
        "ETH/USDT": {"diagnostics": _diag(18.01)},
        "SOL/USDT": {"diagnostics": _diag(17.99)},
    })
    assert [a["symbol"] for a in dashboard.rsi_oversold_alerts(state)] == ["SOL/USDT"]


def test_alert_ignores_missing_and_non_numeric_rsi():
    state = _state({
        "BTC/USDT": {"diagnostics": _diag(None)},
        "ETH/USDT": {"diagnostics": _diag("16")},
        "SOL/USDT": {"diagnostics": _diag(True)},
        "NEAR/USDT": {"diagnostics": _diag(float("nan"))},
        "ADA/USDT": {"diagnostics": {}},
        "XRP/USDT": {},
    })
    assert dashboard.rsi_oversold_alerts(state) == []


def test_alert_sorted_by_rsi_ascending():
    state = _state({
        "BTC/USDT": {"diagnostics": _diag(16.0)},
        "ETH/USDT": {"diagnostics": _diag(9.5)},
        "SOL/USDT": {"diagnostics": _diag(12.3)},
    })
    assert [a["symbol"] for a in dashboard.rsi_oversold_alerts(state)] == ["ETH/USDT", "SOL/USDT", "BTC/USDT"]


def test_threshold_falls_back_when_state_lacks_parameter():
    state = _state({"BTC/USDT": {"diagnostics": {"rsi": 27.0}}})
    assert dashboard.oversold_threshold(state) == (18.0, "default")
    assert dashboard.rsi_oversold_alerts(state) == []


def test_fallback_override_is_honored_when_state_lacks_parameter(monkeypatch):
    monkeypatch.setattr(dashboard, "OVERSOLD_FALLBACK", 30.0)
    state = _state({"BTC/USDT": {"diagnostics": {"rsi": 27.0}}})
    alerts = dashboard.rsi_oversold_alerts(state)
    assert alerts[0]["rsi"] == 27.0
    assert alerts[0]["oversold"] == 30.0
    assert alerts[0]["source"] == "default"


def test_bot_parameter_wins_over_fallback(monkeypatch):
    monkeypatch.setattr(dashboard, "OVERSOLD_FALLBACK", 10.0)
    state = _state({"BTC/USDT": {"diagnostics": _diag(16.0, oversold=30.0)}})
    alerts = dashboard.rsi_oversold_alerts(state)
    assert alerts[0]["oversold"] == 30.0
    assert alerts[0]["source"] == "state"


def test_invalid_oversold_parameter_falls_back_to_default():
    state = _state({"BTC/USDT": {"diagnostics": {"rsi": 15.0, "oversold": 250.0}}})
    assert dashboard.oversold_threshold(state) == (18.0, "default")
    assert dashboard.rsi_oversold_alerts(state)[0]["oversold"] == 18.0


def test_status_payload_exposes_alert_contract():
    state = _state({"BTC/USDT": {"status": "idle", "diagnostics": _diag(16.0)}})
    payload = dashboard.status_payload(state)
    assert payload["mode"] == "testnet"
    assert payload["oversold"] == 18.0
    assert payload["oversold_source"] == "state"
    assert [a["symbol"] for a in payload["alerts"]] == ["BTC/USDT"]
    assert payload["symbols"][0]["symbol"] == "BTC/USDT"


def test_status_payload_tolerates_empty_state():
    payload = dashboard.status_payload({})
    assert payload["alerts"] == []
    assert payload["oversold"] == dashboard.OVERSOLD_FALLBACK
    assert payload["oversold_source"] == "default"
    assert payload["symbols"] == []


def test_page_renders_alert_banner_and_uses_state_threshold():
    assert "alert-card" in dashboard.PAGE
    assert "const alerts=d.alerts||[]" in dashboard.PAGE
    assert "alertSet.has(x.symbol)" in dashboard.PAGE
    assert "d.oversold==null" in dashboard.PAGE, "ambang grafik harus ikut nilai dari state"
    assert "__OVERSOLD_FALLBACK__" not in dashboard.PAGE
    assert f"OVERSOLD_FALLBACK={json.dumps(dashboard.OVERSOLD_FALLBACK)}" in dashboard.PAGE
    assert re.search(r"create\(R\.ReferenceLine,\{[^}]*y:ov", dashboard.PAGE), "garis oversold harus memakai ambang"
    assert "y:18," not in dashboard.PAGE, "ambang tidak boleh hardcoded"