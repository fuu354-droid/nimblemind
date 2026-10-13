"""Regression tests for the eleventh review round (F11-1..F11-3)."""

import io
import json
import os
import sys

import pytest

from nimblemind import cli, config as config_mod, model, writes
from conftest import toml_list


def test_f11_1_relative_isolation_command_is_unavailable_not_resolved_elsewhere(cfg, user_config, tmp_path, monkeypatch):
    tmp = tmp_path / "tmp"
    (tmp / "tools").mkdir(parents=True)
    planted = tmp / "tools" / "check"
    planted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.setenv("TMPDIR", str(tmp))
    user_config('[isolation]\nchecks = ["command"]\ncommand = ["tools/check", "{file}"]\n')
    config_mod.set_grant("cli", True)
    r = writes.write(cfg(), "cli", "note", "refuse me")
    assert r.state == writes.HELD and "not an absolute path" in r.detail


def test_f11_1_project_placeholder_names_the_project(cfg, user_config, tmp_path):
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    (proj / "scripts" / "check.py").write_text(
        "import sys\nsys.exit(1 if 'zebra' in open(sys.argv[1]).read() else 0)\n", encoding="utf-8")
    (proj / ".nimblemind.toml").write_text("", encoding="utf-8")
    config_mod.trust(proj / ".nimblemind.toml")  # {project} needs a trusted project (F12-1)
    user_config(f'[isolation]\nchecks = ["command"]\n'
                f'command = {toml_list([sys.executable, "{project}/scripts/check.py", "{file}"])}\n')
    c = config_mod.load(proj)
    assert writes.write(c, "cli", "note", "zebra", approved_by="t").state == writes.HELD
    assert writes.write(c, "cli", "note", "fine", approved_by="t").state == writes.STORED
    assert writes.write(cfg(), "cli", "note", "fine", approved_by="t").state == writes.HELD  # no project


def test_f11_1_children_run_in_a_private_dir_under_the_data_home(cfg, user_config, tmp_path):
    out = tmp_path / "cwd.txt"
    script = tmp_path / "m.py"
    script.write_text(f"import os, sys\nsys.stdin.read()\nopen({str(out)!r}, 'w').write(os.getcwd())\n"
                      "print('ok')\n", encoding="utf-8")
    model.run({"command": "argv", "argv": [sys.executable, str(script)]}, "s", "u")
    from nimblemind.paths import data_home
    assert os.path.realpath(out.read_text()).startswith(os.path.realpath(data_home() / "run"))


def test_f11_1_relative_model_command_is_refused():
    with pytest.raises(model.ModelError, match="not an absolute path"):
        model.run({"command": "argv", "argv": ["definitely-not-on-path-xyz"]}, "s", "u")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_f11_2_symlinked_directory_borrows_no_trust(cfg, tmp_path, monkeypatch):
    a, b = tmp_path / "trustedA", tmp_path / "evilB"
    a.mkdir()
    b.mkdir()
    (a / ".nimblemind.toml").write_text('namespace = "private-a"\n', encoding="utf-8")
    config_mod.trust(a / ".nimblemind.toml")
    writes.write(config_mod.load(a), "cli", "journal", "## Unfinished\n- secret plan of A",
                 source_id="session:1", approved_by="t")
    os.symlink(a, b / "link")
    monkeypatch.chdir(b)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(b / "link")})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "session-start"]) == 0
    assert "secret plan" not in out.getvalue()
    assert config_mod.load(str(b / "link")).namespace == "default"


def test_f11_3_core_over_total_is_refused_even_with_a_project(tmp_path, user_config):
    user_config("[budget]\ntotal_chars = 1000\ncore_chars = 5000\nrecall_chars = 100\nprompt_chars = 100\n")
    (tmp_path / ".nimblemind.toml").write_text("# empty\n", encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="core_chars cannot exceed"):
        config_mod.load(tmp_path)


def test_broken_config_in_the_process_dir_does_not_silence_a_valid_payload(cfg, tmp_path, monkeypatch):
    writes.write(cfg(), "cli", "journal", "## Unfinished\n- core", source_id="session:1", approved_by="t")
    bad, good = tmp_path / "bad", tmp_path / "good"
    bad.mkdir()
    good.mkdir()
    (bad / ".nimblemind.toml").write_text("not toml [", encoding="utf-8")
    monkeypatch.chdir(bad)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(good)})))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "session-start"]) == 0
    assert "core" in out.getvalue()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX PATH semantics")
def test_f11_1_which_refuses_a_relative_result_from_a_dot_path_entry(tmp_path, monkeypatch):
    from nimblemind.fsutil import which

    exe = tmp_path / "planted-tool"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", ".")
    assert which("planted-tool") is None
