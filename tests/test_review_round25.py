"""Regression tests for the twenty-fifth review round (F25-1, F25-2)."""

import json
import os
import sys

import pytest

from nimblemind import config as config_mod, writes

MEMILA_DOWN = '[backend]\nname = "memila"\nurl = "http://127.0.0.1:9"\ntimeout = 1\n'


def retry_file(c):
    return c.layout().records.parent / "unsynced.json"


def test_f25_1_a_broken_retry_list_does_not_lose_the_approval(cfg, user_config):
    user_config(MEMILA_DOWN)
    c = cfg()
    writes.write(c, "cli", "note", "one", source_id="note:1", approved_by="t")
    retry_file(c).write_text("{bad", encoding="utf-8")
    r = writes.write(c, "cli", "note", "two", source_id="note:2", approved_by="t")
    assert r.state == writes.STORED and "retry list could not be updated" in r.detail
    assert writes.audit(c, "default") == []


def test_f25_1_approve_still_logs_and_consumes(cfg, user_config):
    user_config(MEMILA_DOWN)
    c = cfg()
    writes.write(c, "cli", "note", "one", source_id="note:1", approved_by="t")
    p = writes.write(c, "cli", "note", "two", source_id="note:2")
    retry_file(c).write_text("{bad", encoding="utf-8")
    assert writes.approve(c, p.entry_id, by="t").state == writes.STORED
    assert writes.pending(c) == [] and writes.audit(c, "default") == []


def test_f25_2_flush_under_a_local_backend_keeps_the_list(cfg, user_config, tmp_path):
    user_config(MEMILA_DOWN)
    c = cfg()
    writes.write(c, "cli", "note", "one", source_id="note:1", approved_by="t")
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text('[backend]\nname = "local"\n', encoding="utf-8")
    config_mod.trust(f)
    assert writes.flush(config_mod.load(proj)) == (0, 1)
    assert json.loads(retry_file(c).read_text()) == ["note:1"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_f25_2_an_unreadable_record_stays_on_the_list(cfg, user_config):
    user_config(MEMILA_DOWN)
    c = cfg()
    writes.write(c, "cli", "note", "one", source_id="note:1", approved_by="t")
    rec = writes.canonical(c)._file("default", "note:1")
    rec.chmod(0o000)
    try:
        assert writes.flush(c) == (0, 1)
        assert json.loads(retry_file(c).read_text()) == ["note:1"]
    finally:
        rec.chmod(0o600)


def test_f25_2_a_deleted_record_leaves_the_list(cfg, user_config):
    user_config(MEMILA_DOWN)
    c = cfg()
    writes.write(c, "cli", "note", "one", source_id="note:1", approved_by="t")
    writes.canonical(c).delete("note:1", "default")
    assert writes.flush(c) == (0, 0)
    assert json.loads(retry_file(c).read_text()) == []


def test_migrate_dry_run_holds_when_the_check_crashes(cfg, tmp_path, monkeypatch):
    from nimblemind import isolation, migrate
    monkeypatch.setattr(isolation, "check", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("x", encoding="utf-8")
    assert migrate.run(cfg(), src, "journal", dry_run=True) == ["would hold a.md as session:a"]
