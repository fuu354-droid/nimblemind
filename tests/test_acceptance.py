"""DESIGN.md section 12, items 1 to 5, as tests."""

import os
import socket

import pytest

from memila_brain import recall, writes
from memila_brain.backends.local import LocalBackend
from memila_brain.records import Record


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the local flow opened a network connection")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_1_basic_flow_with_no_server(cfg, no_network, tmp_path):
    """Store, recall by query, delete: no memila, no Qdrant, no network."""
    c = cfg()
    b = LocalBackend(c.layout().home)
    b.store(Record("note:a", "default", "note", "the qdrant healthcheck lacks start_period"))
    b.store(Record("note:b", "default", "note", "board worker stale locks block a trunk"))
    hits = b.recall("qdrant start_period", "default", 5)
    assert [h.source_id for h in hits] == ["note:a"]
    assert hits[0].adapter == "local" and hits[0].scores["overlap"] == 2
    assert b.delete("note:a", "default") is True
    assert b.recall("qdrant", "default", 5) == []
    assert [r.source_id for r in b.list("default")] == ["note:b"]


def test_2_background_write_waits_for_approval(cfg):
    c = cfg()
    r = writes.write(c, "distill", "journal", "## Unfinished\n- wire the hook\n")
    assert r.state == writes.PENDING
    assert writes.canonical(c).list("default") == []
    assert recall.assemble(c) == ""

    stored = writes.approve(c, r.entry_id, by="tester")
    assert stored.state == writes.STORED
    assert writes.pending(c) == []
    assert "wire the hook" in recall.assemble(c)
    assert writes.audit(c) == []

    # Editing the stored record after approval is caught.
    rec_file = next(f for f in c.layout().records.glob("*.json") if f.name != "index.json")
    rec_file.write_text(rec_file.read_text(encoding="utf-8").replace("wire", "skip"), encoding="utf-8")
    problems = writes.audit(c)
    assert len(problems) == 1 and "differs from what was approved" in problems[0]


def test_2_pending_entry_tampered_before_approval_is_refused(cfg):
    c = cfg()
    r = writes.write(c, "distill", "journal", "honest content")
    f = c.layout().pending / f"{r.entry_id}.json"
    f.write_text(f.read_text(encoding="utf-8").replace("honest", "forged"), encoding="utf-8")
    with pytest.raises(ValueError, match="does not match what was queued"):
        writes.approve(c, r.entry_id, by="tester")


def test_3_refused_and_unscanned_writes_are_held(cfg, user_config, tmp_path):
    c = cfg()
    r = writes.write(c, "cli", "note", "token ghp_" + "a" * 36, approved_by="tester")
    assert r.state == writes.HELD and "GitHub token" in r.detail

    user_config('[isolation]\nchecks = ["builtin-secrets", "command"]\n'
                'command = ["definitely-not-a-real-binary-xyz", "{file}"]\n')
    r2 = writes.write(cfg(), "cli", "note", "harmless", approved_by="tester")
    assert r2.state == writes.HELD and r2.detail.startswith("unavailable")

    assert writes.canonical(c).list("default") == []
    assert len(writes.held(c)) == 2


def test_3_cross_namespace_identity_is_refused(cfg, user_config):
    user_config('[isolation]\nchecks = ["builtin-secrets", "builtin-patterns"]\n'
                '[isolation.identity]\nalpha = ["Acme Bakery"]\nbeta = ["Zenith Tea"]\n')
    c = cfg()
    leak = writes.write(c, "cli", "note", "draft for zenith tea launch",
                        namespace="alpha", approved_by="tester")
    assert leak.state == writes.HELD and "beta" in leak.detail
    own = writes.write(c, "cli", "note", "draft for Acme Bakery launch",
                       namespace="alpha", approved_by="tester")
    assert own.state == writes.STORED


def test_4_injection_never_exceeds_total_budget(cfg, user_config):
    for total, core, rec in [(6000, 3500, 2000), (800, 700, 300), (300, 250, 200)]:
        user_config(f"[budget]\ntotal_chars = {total}\ncore_chars = {core}\n"
                    f"recall_chars = {rec}\nprompt_chars = {min(total, 100)}\n"
                    '[approval]\ncli = "allow"\n')
        c = cfg()
        for i in range(6):
            body = "\n".join(f"## {s}\n" + ("- item %d " % i) * 400
                             for s in c["journal"]["sections"])
            writes.write(c, "cli", "journal", body, source_id=f"session:{total}-{i}")
        out = recall.assemble(c)
        assert 0 < len(out) <= total, (total, len(out))
        assert out.startswith("[memila-brain default]")
        assert "[memila-brain:" in out  # every cut is named


def test_5_nothing_written_under_dot_claude(cfg, fake_model, tmp_path, user_config):
    from memila_brain import distill
    from conftest import toml_list

    home = tmp_path / "home"
    sections = "\n".join(f"## {s}\n- none" for s in
                         ["Decisions", "Rulings", "Pitfalls", "Unfinished",
                          "Lessons learned", "Improvements found"])
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(sections))}\n')
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("\n".join(
        '{"type": "user", "message": {"content": "turn %d"}}' % i for i in range(4)), encoding="utf-8")
    c = cfg()
    assert distill.distill(c, str(transcript), "abc12345") == "pending"
    writes.write(c, "cli", "note", "something", approved_by="tester")
    recall.assemble(c)
    assert not (home / ".claude").exists()
    for root, _, files in os.walk(home):
        assert ".claude" not in root


def test_4_recall_is_dropped_before_core_is_cut(cfg, user_config):
    user_config("[budget]\ntotal_chars = 900\ncore_chars = 800\nrecall_chars = 800\n"
                'prompt_chars = 100\n[approval]\ncli = "allow"\n')
    c = cfg()
    writes.write(c, "cli", "journal", "## Unfinished\n- older task " + "o" * 600,
                 source_id="session:1")
    writes.write(c, "cli", "journal", "## Unfinished\n- newest task " + "n" * 600,
                 source_id="session:2")
    out = recall.assemble(c)
    assert len(out) <= 900
    assert "newest task" in out and "older task" not in out
    assert "recall dropped to fit the total budget" in out


def test_4_core_is_cut_to_fit_rather_than_dropping_everything(cfg, user_config):
    user_config("[budget]\ntotal_chars = 300\ncore_chars = 300\nrecall_chars = 100\n"
                'prompt_chars = 100\n[approval]\ncli = "allow"\n')
    c = cfg()
    writes.write(c, "cli", "journal", "## Unfinished\n- keep this " + "x" * 2000,
                 source_id="session:1")
    out = recall.assemble(c)
    assert 0 < len(out) <= 300
    assert "keep this" in out and "core memory cut" in out
