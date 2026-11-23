"""Tes kontrak halaman dashboard: tiap timeframe RSI harus punya warna garis sendiri."""
import json
import re

import dashboard


def test_timeframe_colors_are_distinct_hex():
    colors = dashboard.RSI_TF_COLORS
    assert set(colors) == {"15m", "1h", "4h"}
    values = [colors[tf] for tf in ("15m", "1h", "4h")]
    assert len(set(values)) == 3, "warna tiap timeframe harus berbeda"
    for value in values:
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", value), value


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
