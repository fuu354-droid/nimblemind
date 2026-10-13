"""Regression tests for the second adversarial round (F2-1..F2-4) and the
cheap observations taken with it."""

import json
import os
import subprocess
import sys
import threading

import pytest

from memila_brain import config as config_mod
from memila_brain import recall, writes
from memila_brain.records import Record


def test_f2_1_allow_in_a_config_the_agent_chose_is_ignored(cfg, tmp_path, monkeypatch):
    agent_cfg = tmp_path / "agent.toml"
    agent_cfg.write_text('[approval]\ncli = "allow"\n', encoding="utf-8")
    monkeypatch.setenv("MEMILA_BRAIN_CONFIG", str(agent_cfg))
    c = cfg()
    assert c["approval"]["cli"] == "ask"
    assert any("ignored" in n for n in c.notes)
    assert writes.write(c, "cli", "journal", "self-approved?").state == writes.PENDING


def test_f2_1_user_config_can_still_tighten(cfg, user_config):
    config_mod.set_grant("cli", True)
    user_config('[approval]\ncli = "deny"\n')
    assert cfg()["approval"]["cli"] == "deny"


def test_f2_1_grant_needs_a_terminal():
    r = subprocess.run([sys.executable, "-m", "memila_brain", "approval", "allow", "cli"],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "terminal" in r.stderr
    assert config_mod.grants() == set()


def test_f2_2_kind_or_time_rewritten_in_queue_is_refused(cfg):
    c = cfg()
    r = writes.write(c, "cli", "note", "remember this")
    shown = writes.load_pending(c, r.entry_id)["_record"].digest
    f = c.layout().pending / f"{r.entry_id}.json"
    e = json.loads(f.read_text(encoding="utf-8"))
    e["record"]["kind"] = "journal"
    e["record"]["created_at"] = "2099-01-01T00:00:00+00:00"
    f.write_text(json.dumps(e), encoding="utf-8")
    with pytest.raises(ValueError):
        writes.approve(c, r.entry_id, by="t", expect_sha256=shown)
    assert recall.assemble(c) == ""


def test_f2_2_audit_catches_a_kind_change_after_storing(cfg):
    c = cfg()
    writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    f = next(p for p in c.layout().records.glob("*.json") if p.name != "index.json")
    d = json.loads(f.read_text(encoding="utf-8"))
    d["kind"] = "journal"
    f.write_text(json.dumps(d), encoding="utf-8")
    assert writes.audit(c) == ["note:1: content differs from what was approved"]


def test_f2_3_trust_is_checked_on_the_bytes_that_are_parsed(tmp_path, monkeypatch):
    f = tmp_path / ".memila-brain.toml"
    f.write_text('namespace = "good"\n', encoding="utf-8")
    config_mod.trust(f)
    evil = b'namespace = "evil"\n'
    real_read = type(f).read_bytes
    calls = []

    def swapping_read(self):
        calls.append(self)
        data = real_read(self)
        if self.name == ".memila-brain.toml":
            f.write_bytes(evil)  # swapped right after this read
        return data

    monkeypatch.setattr(type(f), "read_bytes", swapping_read)
    c = config_mod.load(tmp_path)
    assert c.namespace == "good" and c.project_trusted
    c2 = config_mod.load(tmp_path)
    assert c2.namespace == "default" and not c2.project_trusted


def test_f2_4_hooks_survive_a_closed_stdin():
    for event in ("session-start", "session-end"):
        r = subprocess.run([sys.executable, "-c",
                            "import os, sys; os.close(0); sys.stdin = None; "
                            "from memila_brain.cli import main; raise SystemExit(main(['hook', '%s']))" % event],
                           capture_output=True, text=True, timeout=60)
        assert (r.returncode, r.stdout, r.stderr) == (0, "", ""), event


def test_approved_entry_cannot_be_replayed(cfg):
    c = cfg()
    r = writes.write(c, "cli", "note", "once")
    f = c.layout().pending / f"{r.entry_id}.json"
    saved = f.read_text(encoding="utf-8")
    writes.approve(c, r.entry_id, by="t")
    f.write_text(saved, encoding="utf-8")  # the exact entry, put back
    with pytest.raises(ValueError, match="does not match"):
        writes.approve(c, r.entry_id, by="t")


def test_negative_project_budget_is_ignored(tmp_path):
    (tmp_path / ".memila-brain.toml").write_text("[budget]\ntotal_chars = -5\n", encoding="utf-8")
    assert config_mod.load(tmp_path)["budget"]["total_chars"] == 6000


def test_memila_ignores_proxy_environment(monkeypatch):
    import importlib
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from memila_brain.backends import memila

    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            hits.append(self.path)
            body = b'{"hits": []}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    real, proxy = HTTPServer(("127.0.0.1", 0), H), HTTPServer(("127.0.0.1", 0), H)
    for s in (real, proxy):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy.server_address[1]}")
        monkeypatch.setenv("HTTP_PROXY", f"http://127.0.0.1:{proxy.server_address[1]}")
        monkeypatch.delenv("no_proxy", raising=False)
        monkeypatch.delenv("NO_PROXY", raising=False)
        # The opener is built at import; rebuild it under this environment.
        MemilaBackend = importlib.reload(memila).MemilaBackend
        MemilaBackend(f"http://127.0.0.1:{real.server_address[1]}").recall("q", "ns", 1)
        assert hits == ["/retrieve"]  # an absolute-URL path would mean the proxy got it
    finally:
        real.shutdown()
        proxy.shutdown()
