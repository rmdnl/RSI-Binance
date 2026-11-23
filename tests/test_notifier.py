"""Uji jalur notifikasi Telegram tanpa jaringan.

`Notifier.send` meng-import `requests` di dalam fungsi, jadi modul palsu dapat disuntik lewat
`sys.modules` untuk memeriksa perilakunya secara deterministik.
"""
from __future__ import annotations

import ast
import json
import logging
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import bot as BOT  # noqa: E402


class Resp:
    def __init__(self, ok=True, status_code=200, text=""):
        self.ok, self.status_code, self.text = ok, status_code, text


class FakeRequests:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.resp


def install(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


def test_notifier_is_silent_without_credentials(monkeypatch, caplog):
    fake = install(monkeypatch, FakeRequests(resp=Resp()))
    with caplog.at_level(logging.WARNING):
        BOT.Notifier("", "").send("uji")
    assert fake.calls == []
    assert caplog.text == ""


def test_notifier_logs_http_failure_with_body(monkeypatch, caplog):
    fake = install(monkeypatch, FakeRequests(resp=Resp(ok=False, status_code=401, text="Unauthorized")))
    with caplog.at_level(logging.WARNING):
        BOT.Notifier("123:abc", "999").send("uji")
    assert len(fake.calls) == 1
    assert "401" in caplog.text and "Unauthorized" in caplog.text


def test_notifier_logs_network_exception(monkeypatch, caplog):
    fake = install(monkeypatch, FakeRequests(exc=ConnectionError("dns gagal")))
    with caplog.at_level(logging.WARNING):
        BOT.Notifier("123:abc", "999").send("uji")       # tidak boleh melempar
    assert len(fake.calls) == 1
    assert "ConnectionError" in caplog.text


def test_run_emits_start_then_stop_notification(tmp_path, monkeypatch):
    """Bot yang berhenti segera tetap mengirim notifikasi start dan stop."""
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("DRY_RUN_BALANCE", "1000")
    cfg = BOT.Config.from_env()
    state = BOT.new_state(cfg.state_file, cfg)

    sent: list = []

    class Capture:
        def send(self, text):
            sent.append(text)

    bot = BOT.Bot(cfg, None, BOT.PaperBroker(state, cfg.paper_fee), Capture(), state)
    bot.running = False
    bot.run()

    assert sent == ["Bot dimulai (DRY RUN)", "Bot dihentikan"]
    assert (tmp_path / "state.json").exists()

    # Event stop harus ikut tersimpan: kalau save() berjalan sebelum notify(), event
    # "Bot dihentikan" hanya ada di memori dan hilang saat proses keluar.
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert [event["message"] for event in saved["events"]] == ["Bot dimulai (DRY RUN)", "Bot dihentikan"]


def test_main_has_single_notifier_and_alerts_before_run():
    """Semua jalur keluar sebelum run() harus mengirim peringatan Telegram."""
    src = (ROOT / "bot.py").read_text(encoding="utf-8")
    assert src.count("notifier = Notifier(") == 1
    for marker in ("state perlu rekonsiliasi manual",
                   "gagal membaca saldo",
                   "gagal menyiapkan exchange",
                   "rekonsiliasi startup gagal"):
        assert f"notifier.send(" in src and marker in src, marker


def _cfg_for(tmp_path, monkeypatch, dry: str, testnet: str) -> "BOT.Config":
    monkeypatch.setenv("STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setenv("SYMBOLS", "BTC/USDT")
    monkeypatch.setenv("DRY_RUN", dry)
    monkeypatch.setenv("BINANCE_TESTNET", testnet)
    monkeypatch.setenv("BINANCE_API_KEY", "dummy-key")
    monkeypatch.setenv("BINANCE_API_SECRET", "dummy-secret")
    return BOT.Config.from_env()


@pytest.mark.parametrize("dry,testnet,label", [
    ("true", "false", "DRY RUN"),
    ("false", "true", "TESTNET"),
    ("false", "false", "LIVE"),
])
def test_start_notification_labels_actual_mode(tmp_path, monkeypatch, dry, testnet, label):
    """Notifikasi startup harus menyebut mode sebenarnya, bukan dry/live biner."""
    cfg = _cfg_for(tmp_path, monkeypatch, dry, testnet)
    state = BOT.new_state(cfg.state_file, cfg)
    assert BOT.state_mode(cfg) == ("dry" if dry == "true" else ("testnet" if testnet == "true" else "live"))
    sent = []

    class Capture:
        def send(self, text):
            sent.append(text)

    bot = BOT.Bot(cfg, None, BOT.PaperBroker(state, cfg.paper_fee), Capture(), state)
    bot.running = False
    bot.run()
    assert sent == [f"Bot dimulai ({label})", "Bot dihentikan"]


def test_new_state_rejects_mode_mismatch(tmp_path, monkeypatch):
    """State dry tidak boleh dipakai ulang oleh mode testnet (penyebab exit 2 saat startup)."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"bot_mode": "dry", "symbols": {}, "events": []}), encoding="utf-8")
    cfg = _cfg_for(tmp_path, monkeypatch, "false", "true")
    with pytest.raises(RuntimeError, match="tidak cocok dengan testnet"):
        BOT.new_state(cfg.state_file, cfg)
    assert BOT.state_mode(cfg) == "testnet"


def _make_bot(tmp_path, monkeypatch, sent):
    cfg = _cfg_for(tmp_path, monkeypatch, "true", "false")
    state = BOT.new_state(cfg.state_file, cfg)

    class Capture:
        def send(self, text):
            sent.append(text)

    return BOT.Bot(cfg, None, BOT.PaperBroker(state, cfg.paper_fee), Capture(), state)


def _failing_save():
    def boom():
        raise OSError("disk penuh")
    return boom


def test_repeated_save_failures_alert_once_per_streak(tmp_path, monkeypatch):
    """save() yang gagal berulang memicu tepat satu alert Telegram, bukan spam dan bukan senyap."""
    sent = []
    bot = _make_bot(tmp_path, monkeypatch, sent)
    monkeypatch.setattr(bot, "save", _failing_save())

    for _ in range(BOT.SAVE_ALERT_AFTER - 1):
        assert bot.persist("loop") is False
    assert sent == [], "alert dikirim sebelum ambang kegagalan tercapai"

    assert bot.persist("loop") is False
    assert len(sent) == 1 and "gagal menyimpan state" in sent[0]
    assert bot.persist("loop") is False
    assert len(sent) == 1, "kegagalan berikutnya tidak boleh menambah alert"

    monkeypatch.setattr(bot, "save", lambda: None)
    assert bot.persist("loop") is True
    assert bot.save_failures == 0
    assert len(sent) == 1


def test_shutdown_save_failure_alerts_and_does_not_crash(tmp_path, monkeypatch):
    """Kegagalan save saat shutdown dilaporkan; proses tidak boleh mati dengan exception."""
    sent = []
    bot = _make_bot(tmp_path, monkeypatch, sent)
    monkeypatch.setattr(bot, "save", _failing_save())
    bot.running = False

    bot.run()  # tidak boleh melempar

    assert sent[0].startswith("Bot dimulai")
    assert "Bot dihentikan" in sent
    assert any("gagal menyimpan state" in text for text in sent), sent
    assert bot.save_failures == 1


def test_exit_alert_survives_save_failure(tmp_path, monkeypatch):
    """Alert exit (TP/SL) tetap terkirim saat save() gagal: pesan operator tidak boleh tertelan."""
    sent = []
    bot = _make_bot(tmp_path, monkeypatch, sent)
    sent.clear()
    monkeypatch.setattr(bot, "save", _failing_save())

    st = bot.state["symbols"]["BTC/USDT"]
    st["status"] = "in_position"
    st["position"] = {"qty": 1.0, "cost": 100.0}
    bot.record_exit("BTC/USDT", st, "TP", {"qty": 1.0, "price": 110.0, "proceeds": 109.89}, 1_700_000_000.0)

    assert any(text.startswith("TP BTC/USDT: exit 110") for text in sent), sent
    assert bot.save_failures == 1
    assert st["position"] is None


def _top_call(stmt, attr: str) -> bool:
    """True bila pernyataan ini tepat sebuah pemanggilan `self.<attr>()` di level atas fungsi."""
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == attr)


def test_source_has_no_raw_save_pending_before_notify():
    """Guard struktur: save() mentah yang belum di-reset persist() di depan notify() menelan alert.

    Hanya pernyataan level atas yang diperiksa: `persist()` sendiri memakai `save()` lalu
    `notify()` di dalam penanganan exceptionnya, dan itu memang jalur aman yang dimaksud.
    """
    tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        raw_save_pending = False
        for stmt in node.body:
            if _top_call(stmt, "persist"):
                raw_save_pending = False  # penyimpanan aman menggantikan save() mentah
            elif _top_call(stmt, "save"):
                raw_save_pending = True
            if raw_save_pending and _top_call(stmt, "notify"):
                offenders.append(node.name)
                break
    assert offenders == [], f"pakai persist() sebelum notify(): {offenders}"
