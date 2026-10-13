"""Regression tests for the twelfth review round (F12-1..F12-3)."""

import io
import json
import os
import sys

import pytest

from nimblemind import cli, config as config_mod, distill, migrate, model, writes
from conftest import toml_list


def test_f12_1_project_placeholder_never_runs_an_untrusted_repo(cfg, user_config, tmp_path):
    evil = tmp_path / "evil"
    (evil / "scripts").mkdir(parents=True)
    marker = tmp_path / "PWNED"
    (evil / "scripts" / "check.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n",
                                               encoding="utf-8")
    (evil / ".nimblemind.toml").write_text('namespace = "x"\n', encoding="utf-8")
    user_config(f'[isolation]\nchecks = ["builtin-secrets", "command"]\n'
                f'command = {toml_list([sys.executable, "{project}/scripts/check.py", "{file}"])}\n')
    r = writes.write(config_mod.load(evil), "cli", "note", "hello", approved_by="t")
    assert r.state == writes.HELD and "trusted" in r.detail
    assert not marker.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX getcwd semantics")
def test_f12_2_logical_cwd_keeps_the_symlinked_path(cfg, tmp_path, monkeypatch):
    trusted, evil = tmp_path / "trusted", tmp_path / "evil"
    trusted.mkdir()
    evil.mkdir()
    (trusted / ".nimblemind.toml").write_text('namespace = "secretpool"\n', encoding="utf-8")
    config_mod.trust(trusted / ".nimblemind.toml")
    writes.write(config_mod.load(trusted), "cli", "journal", "## Unfinished\n- secret plan",
                 source_id="session:1", approved_by="t")
    os.symlink(trusted, evil / "sub")
    monkeypatch.chdir(evil / "sub")
    monkeypatch.setenv("PWD", str(evil / "sub"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "session-start", "--harness", "claude-code"]) == 0
    assert "secret plan" not in out.getvalue()
    monkeypatch.setattr(sys, "stdout", sys.__stdout__)
    assert cli._cfg(type("A", (), {"cwd": None, "namespace": None})()).namespace == "default"


def test_f12_3_claude_cli_argv_carries_no_prompt_text(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(model, "which", lambda name: "/usr/bin/claude")

    class R:
        returncode, stdout, stderr = 0, json.dumps({"result": "ok"}), ""

    def fake_run(argv, **kw):
        seen["argv"], seen["input"] = argv, kw["input"]
        return R()

    monkeypatch.setattr(model.subprocess, "run", fake_run)
    system = 'instructions ok" & calc.exe & echo "\nline two'
    model.run({"command": "claude-cli", "model": "haiku"}, system, "transcript")
    assert all(system not in a and "calc" not in a for a in seen["argv"])
    assert seen["input"].startswith(system)
    with pytest.raises(model.ModelError, match="cannot carry safely"):
        model.run({"command": "claude-cli", "model": "haiku & calc"}, "s", "u")


def test_dash_session_id_reaches_the_child(monkeypatch):
    seen = []
    monkeypatch.setattr(distill.subprocess, "Popen", lambda argv, **kw: seen.append(argv))
    distill.spawn("/t", "--cwd=/x", "/c", "generic")
    assert "--session-id=--cwd=/x" in seen[0]


@pytest.mark.skipif(sys.platform == "win32", reason="backslash is a separator on Windows")
def test_migrate_survives_a_bad_file_name(cfg, tmp_path):
    src = tmp_path / "old"
    src.mkdir()
    (src / "x\\y.md").write_text("x", encoding="utf-8")
    (src / "ok.md").write_text("fine", encoding="utf-8")
    (src / "s.unscanned.md").write_text("u", encoding="utf-8")
    lines = migrate.run(cfg(), src, "journal")
    assert any(ln.startswith("FAIL x\\y.md") for ln in lines)
    assert any(ln.startswith("ok ok.md") for ln in lines)
    assert [e["verdict"] for e in writes.held(cfg())] == ["unavailable"]
