"""Regression tests for the twenty-seventh review round (F27-1) and the CLI
observations taken with it."""

import json
import os
import sys

import pytest

from nimblemind import cli, config as config_mod, writes


def test_f27_1_audit_json(cfg, capsys):
    writes.write(cfg(), "cli", "note", "x", source_id="note:1", approved_by="t")
    assert cli.main(["audit", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"clean": True, "problems": []}


def test_f27_1_approval_show_json(cfg, capsys):
    assert cli.main(["approval", "show", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["levels"]["distill"] == "ask" and data["grants_file"].endswith("approval.toml")


def test_held_discard_without_ids_is_an_error(cfg):
    with pytest.raises(SystemExit, match="needs at least one id"):
        cli.main(["held", "discard"])


@pytest.mark.parametrize("cmd", ["pending", "held"])
def test_show_of_an_unknown_id_names_it(cfg, capsys, cmd):
    assert cli.main([cmd, "show", "nosuch"]) == 1
    assert "FAIL nosuch" in capsys.readouterr().out


def test_migrate_without_a_source_does_not_ask_first(cfg, monkeypatch):
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or "t")
    with pytest.raises(SystemExit, match="needs a source"):
        cli.main(["migrate"])
    assert asked == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_trust_of_a_symlink_refuses_before_asking(tmp_path, monkeypatch):
    a = tmp_path / "a.toml"
    a.write_text('namespace = "x"\n', encoding="utf-8")
    os.symlink(a, tmp_path / ".nimblemind.toml")
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or "t")
    with pytest.raises(SystemExit, match="symlink"):
        cli.main(["trust", str(tmp_path / ".nimblemind.toml")])
    assert asked == []
