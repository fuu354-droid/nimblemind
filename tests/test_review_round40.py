"""Regression tests for the fortieth review round (F40-1, F40-2)."""

import os
import sys

import pytest

from nimblemind import cli
from nimblemind.fsutil import logical_cwd

posix = pytest.mark.skipif(sys.platform == "win32", reason="symlinked directories")


@posix
def test_f40_2_pwd_with_dotdot_past_a_link_is_not_another_directory(tmp_path, monkeypatch):
    (tmp_path / "y" / "deep").mkdir(parents=True)
    (tmp_path / "y" / "proj").mkdir()
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "proj").mkdir()
    os.symlink(tmp_path / "y" / "deep", tmp_path / "x" / "link")
    monkeypatch.chdir(tmp_path / "y" / "proj")
    monkeypatch.setenv("PWD", str(tmp_path / "x" / "link" / ".." / "proj"))
    assert os.path.samefile(logical_cwd(), tmp_path / "y" / "proj")


@posix
def test_f40_1_migrate_source_joins_the_logical_cwd(tmp_path, monkeypatch, capsys):
    (tmp_path / "physical" / "real").mkdir(parents=True)
    (tmp_path / "physical" / "old").mkdir()
    (tmp_path / "physical" / "old" / "a.md").write_text("## Unfinished\n- a\n", encoding="utf-8")
    (tmp_path / "logical" / "old").mkdir(parents=True)
    (tmp_path / "logical" / "old" / "b.md").write_text("## Unfinished\n- b\n", encoding="utf-8")
    os.symlink(tmp_path / "physical" / "real", tmp_path / "logical" / "link")
    monkeypatch.chdir(tmp_path / "logical" / "link")
    monkeypatch.setenv("PWD", str(tmp_path / "logical" / "link"))
    assert cli.main(["migrate", "../old", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "b.md" in out and "a.md" not in out


@posix
def test_f40_1_trust_path_is_judged_logically(tmp_path, monkeypatch, capsys):
    (tmp_path / "physical" / "real").mkdir(parents=True)
    (tmp_path / "physical" / "proj").mkdir()                       # a directory physically
    (tmp_path / "logical").mkdir()
    (tmp_path / "logical" / "proj").write_text("[budget]\ntotal_chars = 10\n", encoding="utf-8")
    os.symlink(tmp_path / "physical" / "real", tmp_path / "logical" / "link")
    monkeypatch.chdir(tmp_path / "logical" / "link")
    monkeypatch.setenv("PWD", str(tmp_path / "logical" / "link"))
    monkeypatch.setattr(cli, "confirm", lambda q: "t")
    assert cli.main(["trust", "../proj"]) == 0
    assert str(tmp_path / "logical" / "proj") in capsys.readouterr().out
