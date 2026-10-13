"""Regression tests for the twenty-first review round (F21-1)."""

import sys

import pytest

from nimblemind import config as config_mod, writes
from conftest import toml_list
from test_review_round20 import REFUSE_BADWORD


def trusted_project(tmp_path, with_checks):
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    checks = 'checks = ["command"]\n' if with_checks else ""
    f.write_text(f'[isolation]\n{checks}command = {toml_list([sys.executable, "-c", REFUSE_BADWORD, "{file}"])}\n',
                 encoding="utf-8")
    config_mod.trust(f)
    return proj


@pytest.mark.parametrize("with_checks", [True, False])
def test_f21_1_project_command_check_runs_however_it_is_written(cfg, tmp_path, with_checks):
    proj = trusted_project(tmp_path, with_checks)
    c = config_mod.load(proj)
    assert writes.write(c, "cli", "note", "plain harmless note", approved_by="t").state == writes.STORED
    r = writes.write(c, "cli", "note", "has BADWORD", approved_by="t")
    assert r.state == writes.HELD and r.detail == "refused: found BADWORD"


def test_f21_1_user_command_check_without_a_command_still_holds(cfg, user_config, tmp_path):
    user_config('[isolation]\nchecks = ["builtin-secrets", "command"]\n')
    proj = trusted_project(tmp_path, with_checks=True)
    r = writes.write(config_mod.load(proj), "cli", "note", "x", approved_by="t")
    assert r.state == writes.HELD


@pytest.mark.parametrize("value", ["1e300", "86401"])
def test_huge_timeouts_are_refused(tmp_path, monkeypatch, value):
    monkeypatch.setenv("NIMBLEMIND_ISOLATION_TIMEOUT", value)
    with pytest.raises(config_mod.ConfigError, match="at most"):
        config_mod.load(tmp_path)
