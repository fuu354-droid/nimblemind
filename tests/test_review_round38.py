"""Regression tests for the thirty-eighth review round (F38-1)."""

import io
import sys

import pytest

from nimblemind import cli, paths


def test_f38_1_relative_nimblemind_home_is_refused(monkeypatch):
    monkeypatch.setenv("NIMBLEMIND_HOME", "nm-data")
    with pytest.raises(paths.LocationError):
        paths.data_home()
    assert cli.main(["config"]) == 2


def test_f38_1_relative_home_in_the_user_config_is_refused(monkeypatch, user_config):
    monkeypatch.delenv("NIMBLEMIND_HOME")
    user_config('home = "nm-data"\n')
    with pytest.raises(paths.LocationError):
        paths.data_home()


def test_f38_1_relative_nimblemind_config_is_refused(monkeypatch):
    monkeypatch.setenv("NIMBLEMIND_CONFIG", "cfg.toml")
    with pytest.raises(paths.LocationError):
        paths.user_config()


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="XDG locations are Linux defaults")
def test_f38_1_relative_xdg_values_are_ignored(monkeypatch, tmp_path):
    monkeypatch.delenv("NIMBLEMIND_HOME")
    monkeypatch.delenv("NIMBLEMIND_CONFIG")
    monkeypatch.setenv("XDG_CONFIG_HOME", "cfgrel")
    monkeypatch.setenv("XDG_DATA_HOME", "datarel")
    home = paths.Path.home()
    assert paths.user_config() == home / ".config" / "nimblemind" / "config.toml"
    assert paths.data_home() == home / ".local" / "share" / "nimblemind"


def test_f38_1_hooks_stay_silent_on_a_relative_home(monkeypatch, tmp_path):
    monkeypatch.setenv("NIMBLEMIND_HOME", "nm-data")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"cwd": "%s"}' % tmp_path.as_posix()))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "session-start"]) == 0
    assert out.getvalue() == "" and not (tmp_path / "nm-data").exists()
