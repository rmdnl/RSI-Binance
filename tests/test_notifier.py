"""Uji jalur notifikasi Telegram tanpa jaringan.

`Notifier.send` meng-import `requests` di dalam fungsi, jadi modul palsu dapat disuntik lewat
`sys.modules` untuk memeriksa perilakunya secara deterministik.
"""
from __future__ import annotations

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
