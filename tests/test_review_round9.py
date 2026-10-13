"""Regression tests for the ninth review round (F9-1..F9-5)."""

import io
import json
import os
import sys

import pytest

from nimblemind import cli, config as config_mod, paths, writes
from conftest import toml_list


def refuser(tmp_path, word):
    script = tmp_path / f"refuse_{word}.py"
    script.write_text(f"import sys\nsys.exit(1 if {word!r} in open(sys.argv[1]).read() else 0)\n",
                      encoding="utf-8")
    return [sys.executable, str(script), "{file}"]


def test_f9_1_trusted_project_command_adds_to_the_users(tmp_path, user_config):
    user_config(f'[isolation]\nchecks = ["builtin-secrets", "command"]\n'
                f'command = {toml_list(refuser(tmp_path, "zebra"))}\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text(f'[isolation]\ncommand = {toml_list(refuser(tmp_path, "lion"))}\n', encoding="utf-8")
    config_mod.trust(f)
    c = config_mod.load(proj)
    assert writes.write(c, "cli", "note", "zebra rollout", approved_by="t").state == writes.HELD
    assert writes.write(c, "cli", "note", "lion rollout", approved_by="t").state == writes.HELD
    assert writes.write(c, "cli", "note", "plain", approved_by="t").state == writes.STORED


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_f9_2_a_symlinked_project_config_borrows_no_trust(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / ".nimblemind.toml").write_text('namespace = "a-private"\n', encoding="utf-8")
    config_mod.trust(a / ".nimblemind.toml")
    os.symlink(a / ".nimblemind.toml", b / ".nimblemind.toml")
    assert config_mod.load(a).namespace == "a-private"
    c = config_mod.load(b)
    assert not c.project_trusted and c.namespace == "default"
    with pytest.raises(ValueError, match="symlink"):
        config_mod.trust(b / ".nimblemind.toml")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_f9_3_hook_and_cli_agree_on_a_symlinked_or_dotdot_cwd(cfg, tmp_path, monkeypatch):
    writes.write(cfg(), "cli", "journal", "## Unfinished\n- core", source_id="session:1",
                 approved_by="t")
    g, f = tmp_path / "G", tmp_path / "F"
    g.mkdir()
    f.mkdir()
    (f / ".nimblemind.toml").write_text("[budget]\ntotal_chars = 0\n", encoding="utf-8")
    os.symlink(f, g / "link")
    monkeypatch.chdir(tmp_path)
    for cwd in (str(g / "link"), str(g / ".." / "F")):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": cwd})))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        assert cli.main(["hook", "session-start"]) == 0
        assert out.getvalue() == "", cwd


def test_f9_4_approval_allow_names_the_explicit_setting_that_wins(user_config, monkeypatch, capsys):
    user_config('[approval]\ncli = "ask"\n')
    monkeypatch.setattr(cli, "confirm", lambda q: "t")
    assert cli.main(["approval", "allow", "cli"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("cli: ask") and "explicit setting" in out


def test_f9_5_data_home_from_the_user_config(tmp_path, user_config, monkeypatch):
    monkeypatch.delenv("NIMBLEMIND_HOME")
    user_config(f'home = {toml_list([str(tmp_path / "cfghome")])[1:-1]}\n')
    assert paths.data_home() == tmp_path / "cfghome"
    monkeypatch.setenv("NIMBLEMIND_HOME", str(tmp_path / "envhome"))
    assert paths.data_home() == tmp_path / "envhome"


def test_pending_reject_unknown_id_is_a_clean_failure(cfg, capsys):
    assert cli.main(["pending", "reject", "nope"]) == 1
    assert "FAIL nope" in capsys.readouterr().out


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_f9_3_hook_loads_config_from_the_normalized_unresolved_cwd(tmp_path, monkeypatch):
    g, f = tmp_path / "G", tmp_path / "F"
    g.mkdir()
    f.mkdir()
    os.symlink(f, g / "link")
    seen = []
    real_load = config_mod.load
    monkeypatch.setattr(config_mod, "load", lambda cwd=None: seen.append(cwd) or real_load(cwd))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(g / "link")})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli.main(["hook", "session-start"]) == 0
    # Round 11 (F11-2): normalized like every CLI path, never resolved, so
    # the symlinked directory is not taken for the project it points to.
    assert str(g / "link") in [str(c) for c in seen]
    assert str(f.resolve()) not in [str(c) for c in seen]
