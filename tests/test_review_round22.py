"""Regression tests for the twenty-second review round (F22-1, F22-2)."""

import os
import sys

import pytest

from nimblemind import config as config_mod, fsutil, paths, writes
from nimblemind.backends import BackendError
from nimblemind.backends.local import LocalBackend
from nimblemind.backends.memila import MemilaBackend


def deny_stat_under(monkeypatch, root):
    real = os.stat

    def stat(p, *a, **k):
        if str(p).startswith(str(root)) and str(p) != str(root):
            raise PermissionError(13, "Permission denied", str(p))
        return real(p, *a, **k)

    monkeypatch.setattr(os, "stat", stat)


def test_f22_1_permission_error_is_raised_not_read_as_absent(tmp_path, monkeypatch):
    deny_stat_under(monkeypatch, tmp_path)
    with pytest.raises(PermissionError):
        fsutil.path_kind(tmp_path / "x")
    with pytest.raises(PermissionError):
        paths.find_project_config(tmp_path / "a" / "b")


def test_f22_1_unreadable_search_path_is_a_config_error_on_any_python(tmp_path, monkeypatch):
    deny_stat_under(monkeypatch, tmp_path / "nox")
    with pytest.raises(config_mod.ConfigError, match="cannot search"):
        config_mod.load(tmp_path / "nox" / "x")


def test_f22_1_unreadable_records_directory_is_not_audit_clean(cfg, monkeypatch):
    c = cfg()
    writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    records = c.layout().records
    real = os.stat

    def stat(p, *a, **k):
        if str(p) == str(records):
            raise PermissionError(13, "Permission denied", str(p))
        return real(p, *a, **k)

    monkeypatch.setattr(os, "stat", stat)
    problems = writes.audit(c, "default")
    assert len(problems) == 1 and "cannot be listed" in problems[0]


def test_path_kind_answers_plainly(tmp_path):
    (tmp_path / "f").write_text("x", encoding="utf-8")
    assert fsutil.path_kind(tmp_path / "f") == "file"
    assert fsutil.path_kind(tmp_path) == "dir"
    assert fsutil.path_kind(tmp_path / "nope") is None
    assert fsutil.path_kind(tmp_path / "f" / "under-a-file") is None


@pytest.mark.parametrize("t", [1e300, 86401, 0, float("nan")])
def test_f22_2_backend_timeout_is_capped(t):
    with pytest.raises(BackendError, match="at most"):
        MemilaBackend.from_config({"name": "memila", "url": "http://127.0.0.1:1", "timeout": t})


def test_user_budget_unknown_key_is_an_error(tmp_path, user_config):
    user_config("[budget]\ntotal_charz = 10\n")
    with pytest.raises(config_mod.ConfigError, match="unknown keys"):
        config_mod.load(tmp_path)


def test_refusal_report_is_stdout(cfg, user_config, tmp_path):
    import sys
    from conftest import toml_list
    script = tmp_path / "r.py"
    script.write_text("import sys\nprint('the reason')\nsys.stderr.write('noise')\nsys.exit(1)\n",
                      encoding="utf-8")
    user_config(f'[isolation]\nchecks = ["command"]\ncommand = {toml_list([sys.executable, str(script), "{file}"])}\n')
    r = writes.write(cfg(), "cli", "note", "x", approved_by="t")
    assert r.state == writes.HELD and r.detail == "refused: the reason"



@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_r1_really_unreadable_records_directory_fails_audit(cfg, capsys):
    from nimblemind import cli

    c = cfg()
    writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    records = c.layout().records
    records.chmod(0o000)
    try:
        assert any("cannot be listed" in p for p in writes.audit(c))
        assert any("cannot be listed" in p for p in writes.audit(c, "default"))
        assert cli.main(["audit"]) == 1
        assert cli.main(["list"]) == 1
        assert "error:" in capsys.readouterr().err
    finally:
        records.chmod(0o700)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_r1_unreadable_pending_directory_is_an_error_not_empty(cfg):
    c = cfg()
    writes.write(c, "cli", "note", "x")
    pending = c.layout().pending
    pending.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            writes.pending(c)
    finally:
        pending.chmod(0o700)
