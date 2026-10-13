"""Regression tests for the first adversarial round's findings (F-1..F-11)."""

import hashlib
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nimblemind import config as config_mod
from nimblemind import migrate, paths, recall, writes
from nimblemind.backends import BackendError
from nimblemind.backends.memila import MemilaBackend, is_forbidden
from nimblemind.records import Record


def test_f1_forged_entry_with_its_own_hash_and_namespace_is_refused(cfg):
    c = cfg()
    r = writes.write(c, "cli", "note", "innocent")
    f = c.layout().pending / f"{r.entry_id}.json"
    e = json.loads(f.read_text(encoding="utf-8"))
    e["record"]["content"] = "forged"
    e["record"]["namespace"] = "victim"
    e["sha256"] = hashlib.sha256(b"forged").hexdigest()
    f.write_text(json.dumps(e), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match what was queued"):
        writes.approve(c, r.entry_id, by="t")
    assert writes.canonical(c).scan("victim") == ([], [])
    assert writes.canonical(c).scan("default") == ([], [])


def test_f1_approval_refuses_content_other_than_what_was_shown(cfg):
    c = cfg()
    r = writes.write(c, "cli", "note", "shown text")
    with pytest.raises(ValueError, match="changed after it was shown"):
        writes.approve(c, r.entry_id, by="t", expect_sha256="0" * 64)


def test_f2_migrate_honours_deny(cfg, user_config, tmp_path):
    user_config('[approval]\ncli = "deny"\n')
    src = tmp_path / "old"
    src.mkdir()
    (src / "a.md").write_text("plain journal", encoding="utf-8")
    lines = migrate.run(cfg(), src, "journal")
    assert lines[0].startswith("FAIL a.md: rejected")
    assert writes.canonical(cfg()).list("default") == []


def test_f3_approve_refuses_when_path_is_now_deny(cfg, user_config):
    r = writes.write(cfg(), "cli", "note", "queued")
    user_config('[approval]\ncli = "deny"\n')
    with pytest.raises(ValueError, match="is deny"):
        writes.approve(cfg(), r.entry_id, by="t")


class Redirector(BaseHTTPRequestHandler):
    target = ""
    hits: list = []

    def do_POST(self):
        Redirector.hits.append((self.server.server_address[1], self.path, self.headers.get("X-Api-Key")))
        if Redirector.target and self.server.server_address[1] != int(Redirector.target.rsplit(":", 1)[1]):
            self.send_response(307)
            self.send_header("Location", Redirector.target + self.path)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"hits": [{"source": "x", "text": "from forbidden host"}]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_f4_redirect_to_a_forbidden_host_is_not_followed():
    Redirector.hits = []
    servers = [HTTPServer(("127.0.0.1", 0), Redirector) for _ in range(2)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        allowed, forbidden = (f"http://127.0.0.1:{s.server_address[1]}" for s in servers)
        Redirector.target = forbidden
        b = MemilaBackend(allowed, api_key="SECRET", forbidden_endpoints=[forbidden.split("//")[1]])
        with pytest.raises(BackendError, match="redirect"):
            b.recall("q", "ns", 1)
        assert all(h[0] == servers[0].server_address[1] for h in Redirector.hits)
    finally:
        for s in servers:
            s.shutdown()


def test_f5_environment_cannot_grant_approval(cfg, monkeypatch):
    monkeypatch.setenv("NIMBLEMIND_APPROVAL_CLI", "allow")
    c = cfg()
    assert c["approval"]["cli"] == "ask"
    assert writes.write(c, "cli", "note", "x").state == writes.PENDING


def test_f6_allow_is_attributed_to_the_grants_file(cfg, tmp_path):
    config_mod.set_grant("cli", True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".nimblemind.toml").write_text("[journal]\nmax_words = 100\n", encoding="utf-8")
    c = config_mod.load(proj)
    writes.write(c, "cli", "note", "x")
    line = json.loads(c.layout().approvals.read_text(encoding="utf-8").splitlines()[-1])
    assert line["approved_by"] == f"grant:{config_mod.grants_file()}#cli" and line["rule"] == "allow"


def test_f7_trust_refuses_a_file_swapped_after_it_was_shown(tmp_path):
    f = tmp_path / ".nimblemind.toml"
    f.write_text('namespace = "a"\n', encoding="utf-8")
    shown = f.read_bytes()
    f.write_text('namespace = "swapped"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="changed after it was shown"):
        config_mod.trust(f, expect=shown)
    assert not config_mod.is_trusted(f)


def test_f8_environment_overrides_a_trusted_project(tmp_path, monkeypatch):
    f = tmp_path / ".nimblemind.toml"
    f.write_text('namespace = "projns"\n', encoding="utf-8")
    config_mod.trust(f)
    monkeypatch.setenv("NIMBLEMIND_NAMESPACE", "envns")
    assert config_mod.load(tmp_path).namespace == "envns"


def test_f9_project_lowering_total_alone_clamps_the_parts(tmp_path):
    (tmp_path / ".nimblemind.toml").write_text("[budget]\ntotal_chars = 1000\n", encoding="utf-8")
    b = config_mod.load(tmp_path)["budget"]
    assert b["total_chars"] == 1000
    assert max(b["core_chars"], b["recall_chars"], b["prompt_chars"]) <= 1000


def test_f10_hook_output_is_utf8_under_a_legacy_code_page(cfg, tmp_path):
    c = cfg()
    writes.write(c, "cli", "journal", "## Unfinished\n- 繼續整合", source_id="session:1",
                 approved_by="t")
    proj = tmp_path / "projé"
    proj.mkdir()
    env = dict(__import__("os").environ, PYTHONIOENCODING="cp1252")
    r = subprocess.run([sys.executable, "-m", "nimblemind", "hook", "session-start"],
                       input=json.dumps({"cwd": str(proj)}).encode("utf-8"),
                       capture_output=True, env=env, timeout=60)
    assert r.returncode == 0
    ctx = json.loads(r.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]
    assert "繼續整合" in ctx


def test_f11_long_namespace_is_a_bounded_unique_component(cfg, user_config):
    a, b = "N" * 80 + "a", "N" * 80 + "b"
    sa, sb = paths.safe_name(a), paths.safe_name(b)
    assert sa != sb and len(sa) <= 3 + paths.MAX_COMPONENT and len(sb) <= 3 + paths.MAX_COMPONENT
    assert writes.write(cfg(), "cli", "note", "x", namespace="日" * 60,
                        approved_by="t").state == writes.STORED


def test_audit_sees_a_record_the_index_lost(cfg):
    c = cfg()
    writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    writes.canonical(c).store(Record("note:2", "default", "note", "unapproved"))
    index_file = c.layout().records / "index.json"
    idx = json.loads(index_file.read_text(encoding="utf-8"))
    del idx["records"]["note:2"]
    index_file.write_text(json.dumps(idx), encoding="utf-8")
    assert [r.source_id for r in writes.canonical(c).list("default")] == ["note:1"]
    assert writes.audit(c, "default") == ["note:2: stored with no approval entry"]


def test_bracketed_ipv6_forbidden_entry_matches():
    assert is_forbidden("http://[::1]:6500/x", ["[::1]:6500"])
