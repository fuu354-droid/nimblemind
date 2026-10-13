"""Regression tests for the twenty-fourth review round (F24-1) and the
observations taken with it."""

import json
import os
import sys

import pytest

from nimblemind import cli, migrate, writes

posix_only = pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                                reason="POSIX permissions, not root")


@posix_only
def test_f24_1_unreadable_migrate_source_is_a_failure(cfg, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "j1.md").write_text("## Unfinished\n- a\n", encoding="utf-8")
    assert migrate.run(cfg(), src, "journal", dry_run=True)[0].startswith("would import")
    src.chmod(0o000)
    try:
        lines = migrate.run(cfg(), src, "journal", dry_run=True)
        assert lines == [f"FAIL {src}: cannot be listed: Permission denied"]
        assert cli.main(["migrate", "--dry-run", str(src)]) == 1
    finally:
        src.chmod(0o700)


@posix_only
def test_unreadable_namespaces_directory_fails_audit(cfg):
    c = cfg()
    writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    root = c.layout().home / "namespaces"
    root.chmod(0o000)
    try:
        problems = writes.audit(c)
        assert len(problems) == 1 and "cannot be listed" in problems[0]
    finally:
        root.chmod(0o700)


def test_corrupt_retry_list_is_not_rewritten_as_empty(cfg, user_config, capsys):
    user_config('[backend]\nname = "memila"\nurl = "http://127.0.0.1:9"\ntimeout = 1\n')
    c = cfg()
    writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    f = c.layout().records.parent / "unsynced.json"
    assert json.loads(f.read_text()) == ["note:1"]
    f.write_text("{not json", encoding="utf-8")
    assert cli.main(["flush"]) == 1
    assert "not a readable retry list" in capsys.readouterr().err
    assert f.read_text(encoding="utf-8") == "{not json"


@posix_only
def test_unreadable_approval_log_is_named_by_audit(cfg):
    c = cfg()
    writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    log = c.layout().approvals
    log.chmod(0o000)
    try:
        problems = writes.audit(c)
        assert len(problems) == 1 and "cannot be read" in problems[0]
    finally:
        log.chmod(0o600)
