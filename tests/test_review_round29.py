"""Regression tests for the twenty-ninth review round (F29-1)."""

import json
import os
import sys

import pytest

from nimblemind import cli


@pytest.mark.parametrize("flag", [[], ["--json"]])
def test_f29_1_config_with_a_toml_date_prints(tmp_path, monkeypatch, capsys, flag):
    (tmp_path / ".nimblemind.toml").write_text("[journal]\nupdated = 2026-10-01\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    assert cli.main(["config", *flag]) == 0
    assert json.loads(capsys.readouterr().out)["effective"]["journal"]["updated"] == "2026-10-01"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permissions, not root")
def test_migrate_of_an_unlistable_source_refuses_before_asking(cfg, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    src.chmod(0o000)
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or "t")
    try:
        with pytest.raises(SystemExit, match="cannot be listed"):
            cli.main(["migrate", str(src)])
        assert asked == []
    finally:
        src.chmod(0o700)
