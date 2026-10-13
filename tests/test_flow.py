"""Distill, hooks, the memila backend against a stub, migrate, the CLI."""

import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nimblemind import cli, distill, migrate, recall, writes
from nimblemind import config as config_mod
from nimblemind.backends.memila import MemilaBackend, is_forbidden
from nimblemind.backends import BackendError
from nimblemind.records import Record
from conftest import toml_list

SECTIONS = ["Decisions", "Rulings", "Pitfalls", "Unfinished", "Lessons learned", "Improvements found"]


def journal_text(unfinished="- resume the hook wiring"):
    return "\n".join(f"## {s}\n" + (unfinished if s == "Unfinished" else "- none") for s in SECTIONS)


def transcript(tmp_path, turns=4):
    lines = []
    for i in range(turns):
        lines.append(json.dumps({"type": "user", "origin": {"kind": "human"},
                                 "message": {"content": f"please do step {i}"}}))
        lines.append(json.dumps({"type": "assistant",
                                 "message": {"content": [{"type": "text", "text": f"did {i}"}]}}))
    lines.append(json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "ignored"}]}}))
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---- distill -------------------------------------------------------------

def test_distill_queues_a_valid_journal(tmp_path, user_config, fake_model, cfg):
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(journal_text()))}\n')
    c = cfg()
    assert distill.distill(c, str(transcript(tmp_path)), "deadbeef-1234") == "pending"
    [entry] = writes.pending(c)
    assert entry["path"] == "distill" and "-deadbeef-" in entry["record"]["source_id"]


def test_distill_allow_rule_stores_and_recall_shows_it(tmp_path, user_config, fake_model, cfg):
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(journal_text()))}\n')
    config_mod.set_grant("distill", True)
    c = cfg()
    assert distill.distill(c, str(transcript(tmp_path)), "s1") == "stored"
    out = recall.assemble(c)
    assert "resume the hook wiring" in out and out.startswith("[nimblemind default]")
    log = (c.layout().approvals).read_text(encoding="utf-8")
    assert '"rule": "allow"' in log and "approval.toml#distill" in log


def test_distill_skips_short_sessions(tmp_path, user_config, fake_model, cfg):
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(journal_text()))}\n')
    assert distill.distill(cfg(), str(transcript(tmp_path, turns=2)), "s") == "skip"


def test_distill_rejects_after_retry(tmp_path, user_config, fake_model, cfg):
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model("## Decisions only"))}\n')
    c = cfg()
    assert distill.distill(c, str(transcript(tmp_path)), "s") == "reject"
    assert writes.pending(c) == []


def test_journal_language_and_banned_chars(tmp_path, user_config, fake_model, cfg):
    bad = journal_text("- finish 中文 — now")
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(bad))}\n'
                '[journal]\nlanguage = "English"\nbanned_chars = ["—"]\n')
    from nimblemind import journal
    found = journal.problems(cfg(), bad)
    assert any("CJK" in p for p in found) and any("banned" in p for p in found)


def test_cjk_allowed_when_no_language_rule(cfg):
    from nimblemind import journal
    assert journal.problems(cfg(), journal_text("- 繼續")) == []


# ---- hooks ---------------------------------------------------------------

def run_cli(argv, stdin=""):
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = io.StringIO(stdin), io.StringIO()
    try:
        rc = cli.main(argv)
        return rc, sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = old_in, old_out


def test_session_start_hook_prints_claude_code_json(tmp_path, cfg):
    c = cfg()
    writes.write(c, "cli", "journal", journal_text(), source_id="session:x", approved_by="t")
    rc, out = run_cli(["hook", "session-start"], json.dumps({"cwd": str(tmp_path)}))
    assert rc == 0
    ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "resume the hook wiring" in ctx


def test_session_start_hook_is_silent_with_nothing_stored(tmp_path):
    assert run_cli(["hook", "session-start"], json.dumps({"cwd": str(tmp_path)})) == (0, "")


def test_hooks_never_fail(tmp_path, user_config):
    user_config("this is not toml [")
    assert run_cli(["hook", "session-start"], "{not json") == (0, "")
    assert run_cli(["hook", "session-end"], "{}") == (0, "")


def test_session_end_hook_skips_inside_a_child(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(distill, "spawn", lambda *a: called.append(a))
    payload = json.dumps({"transcript_path": str(transcript(tmp_path)), "session_id": "s",
                          "cwd": str(tmp_path)})
    monkeypatch.setenv("NIMBLEMIND_CHILD", "1")
    run_cli(["hook", "session-end"], payload)
    assert called == []
    monkeypatch.delenv("NIMBLEMIND_CHILD")
    run_cli(["hook", "session-end"], payload)
    assert len(called) == 1


# ---- CLI approval needs a terminal --------------------------------------

def test_cli_approval_refused_without_a_terminal(cfg):
    r = writes.write(cfg(), "distill", "journal", "x")
    rc = None
    with pytest.raises(SystemExit, match="person at a terminal"):
        rc = run_cli(["pending", "approve", r.entry_id])
    assert rc is None and len(writes.pending(cfg())) == 1
    with pytest.raises(SystemExit, match="person at a terminal"):
        run_cli(["store", "--approve", "--text", "y"])
    with pytest.raises(SystemExit, match="person at a terminal"):
        run_cli(["migrate", "."])


def test_cli_store_without_approve_queues(cfg):
    rc, out = run_cli(["store", "--text", "a note"])
    assert rc == 0 and out.startswith("pending")


def test_cli_recall_json(cfg):
    writes.write(cfg(), "cli", "note", "alpha beta", source_id="note:1", approved_by="t")
    rc, out = run_cli(["recall", "beta", "--json"])
    data = json.loads(out)
    assert rc == 0 and data["state"] == "ok" and data["hits"][0]["source_id"] == "note:1"


# ---- memila backend ------------------------------------------------------

class Stub(BaseHTTPRequestHandler):
    calls: list = []
    reply: dict = {}

    def _handle(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
        Stub.calls.append((self.command, self.path, body, self.headers.get("X-Api-Key")))
        data = json.dumps(Stub.reply.get(self.path, {})).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_POST = do_DELETE = _handle

    def log_message(self, *a):
        pass


@pytest.fixture
def stub():
    Stub.calls, Stub.reply = [], {}
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_memila_backend_round_trip(stub):
    b = MemilaBackend(stub, api_key="k")
    b.store(Record("session:1", "ns", "journal", "text"))
    Stub.reply["/retrieve"] = {"hits": [{"source": "session:1", "text": "text", "score": 0.9},
                                        {"source": None, "text": "dropped"}, "junk"]}
    hits = b.recall("q", "ns", 3)
    assert [(h.source_id, h.scores) for h in hits] == [("session:1", {"score": 0.9})]
    assert b.delete("session:1", "ns")
    methods = [(c[0], c[1]) for c in Stub.calls]
    assert methods == [("POST", "/ingest"), ("POST", "/retrieve"), ("DELETE", "/source")]
    assert all(c[3] == "k" and c[2]["namespace"] == "ns" for c in Stub.calls)


def test_memila_send_failure_is_kept_for_flush(tmp_path, user_config, cfg, stub):
    user_config('[backend]\nname = "memila"\nurl = "http://127.0.0.1:9"\ntimeout = 1\n')
    c = cfg()
    r = writes.write(c, "cli", "note", "kept", source_id="note:k", approved_by="t")
    assert r.state == "stored" and "flush" in r.detail
    assert writes.canonical(c).get("note:k", "default") is not None
    user_config(f'[backend]\nname = "memila"\nurl = "{stub}"\n')
    assert writes.flush(cfg()) == (1, 0)
    assert Stub.calls[0][1] == "/ingest"


def test_forbidden_endpoint_by_address_not_spelling(stub):
    port = stub.rsplit(":", 1)[1]
    assert is_forbidden(f"http://127.1:{port}", [f"127.0.0.1:{port}"])
    assert is_forbidden(f"http://localhost:{port}", [f":{port}"])
    assert not is_forbidden(f"http://127.0.0.1:{port}", ["127.0.0.1:1"])
    with pytest.raises(BackendError, match="forbidden"):
        MemilaBackend(stub, forbidden_endpoints=[f"localhost:{port}"]).recall("q", "ns", 1)
    assert Stub.calls == []


# ---- migrate -------------------------------------------------------------

def test_migrate_imports_plain_and_holds_marked(tmp_path, cfg):
    src = tmp_path / "old"
    src.mkdir()
    (src / "20260901-1200-abcd1234.md").write_text(journal_text(), encoding="utf-8")
    (src / "20260902-1200-abcd1234.quarantine.md").write_text("bad", encoding="utf-8")
    (src / "20260903-1200-abcd1234.md").write_text("AKIAABCDEFGHIJKLMNOP", encoding="utf-8")
    c = cfg()
    lines = migrate.run(c, src, "journal")
    assert sum(ln.startswith("ok") for ln in lines) == 1
    assert sum(ln.startswith("held") for ln in lines) == 2
    assert [r.source_id for r in writes.canonical(c).list("default")] == ["session:20260901-1200-abcd1234"]
    assert writes.audit(c) == []
    assert all(ln.startswith(("skip", "held")) for ln in migrate.run(c, src, "journal"))
    assert (src / "20260901-1200-abcd1234.md").exists()  # source untouched


def test_distill_hook_itself_refuses_inside_a_child(tmp_path, monkeypatch, cfg):
    called = []
    monkeypatch.setattr(distill, "spawn", lambda *a: called.append(a))
    monkeypatch.setenv("NIMBLEMIND_CHILD", "1")
    distill.hook(cfg(), {"transcript_path": str(transcript(tmp_path)), "session_id": "s"})
    assert called == []
