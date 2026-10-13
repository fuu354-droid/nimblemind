"""Regression tests for the twentieth review round (F20-1)."""

import sys

import pytest

from nimblemind import config as config_mod, writes
from nimblemind.backends import BackendError
from nimblemind.backends.memila import MemilaBackend, _OPENER, is_forbidden
from conftest import toml_list

# No single quotes: toml_list writes TOML literal strings.
REFUSE_BADWORD = ('import sys; t = open(sys.argv[1], encoding="utf-8").read(); '
                  'print("found BADWORD") if "BADWORD" in t else None; sys.exit("BADWORD" in t)')


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/", "file:///etc/passwd", "data:,x", "gopher://h/"])
def test_f20_1_non_http_urls_are_forbidden_and_refused(url):
    assert is_forbidden(url, [])
    with pytest.raises(BackendError, match="http or https"):
        MemilaBackend.from_config({"name": "memila", "url": url})


def test_f20_1_opener_has_no_ftp_file_or_data_handler():
    names = {type(h).__name__ for h in _OPENER.handlers}
    assert not names & {"FTPHandler", "CacheFTPHandler", "FileHandler", "DataHandler"}
    assert {"HTTPHandler", "HTTPSHandler"} <= names


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0"])
@pytest.mark.parametrize("section", ["isolation", "distiller", "researcher"])
def test_timeouts_must_be_finite_and_positive(tmp_path, monkeypatch, section, value):
    monkeypatch.setenv(f"NIMBLEMIND_{section.upper()}_TIMEOUT", value)
    with pytest.raises(config_mod.ConfigError, match="positive"):
        config_mod.load(tmp_path)


def test_backend_nan_timeout_is_refused():
    with pytest.raises(BackendError, match="positive"):
        MemilaBackend.from_config({"name": "memila", "url": "http://127.0.0.1:1", "timeout": float("nan")})


def test_user_command_check_without_a_command_holds_even_with_a_project_command(cfg, user_config, tmp_path):
    user_config('[isolation]\nchecks = ["builtin-secrets", "command"]\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text(f'[isolation]\ncommand = {toml_list([sys.executable, "-c", "pass", "{file}"])}\n',
                 encoding="utf-8")
    config_mod.trust(f)
    r = writes.write(config_mod.load(proj), "cli", "note", "x", approved_by="t")
    assert r.state == writes.HELD and "isolation.command is empty" in r.detail


def test_project_added_command_alone_still_runs(cfg, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text(f'[isolation]\ncommand = {toml_list([sys.executable, "-c", REFUSE_BADWORD, "{file}"])}\n',
                 encoding="utf-8")
    config_mod.trust(f)
    c = config_mod.load(proj)
    assert writes.write(c, "cli", "note", "x", approved_by="t").state == writes.STORED
    r = writes.write(c, "cli", "note", "has BADWORD", approved_by="t")
    assert r.state == writes.HELD and r.detail == "refused: found BADWORD"
