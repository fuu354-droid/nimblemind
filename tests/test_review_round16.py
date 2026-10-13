"""Regression tests for the sixteenth review round (F16-1, F16-2)."""

import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from nimblemind import cli, config as config_mod, paths, writes
from nimblemind.backends.memila import is_forbidden
from nimblemind.records import Record


def test_f16_1_a_grant_does_not_survive_a_different_user_config(cfg, user_config, tmp_path, monkeypatch):
    user_config('[approval]\ncli = "deny"\n[isolation]\nchecks = ["builtin-secrets", "builtin-patterns"]\n'
                'deny = ["(?i)codename-falcon"]\n')
    # grant recorded while some other config was in effect: cli is not
    # named there, so the grant would apply
    monkeypatch.setenv("NIMBLEMIND_CONFIG", str(tmp_path / "none.toml"))
    config_mod.set_grant("cli", True)
    assert cfg()["approval"]["cli"] == "allow"
    # the person's own config: deny wins
    monkeypatch.setenv("NIMBLEMIND_CONFIG", str(tmp_path / "user.toml"))
    assert cfg()["approval"]["cli"] == "deny"
    # an agent pointing at yet another config gets no grant at all
    monkeypatch.setenv("NIMBLEMIND_CONFIG", str(tmp_path / "agent.toml"))
    c = cfg()
    assert c["approval"]["cli"] == "ask"
    assert writes.write(c, "cli", "note", "note about codename-falcon").state == writes.PENDING


def test_f16_1_grant_records_the_user_config(tmp_path):
    config_mod.set_grant("distill", True)
    text = config_mod.grants_file().read_text(encoding="utf-8")
    import os
    assert os.fsencode(os.path.abspath(paths.user_config())).hex() in text and '"distill"' in text


@pytest.mark.parametrize("spelling", ["0.0.0.0", "[::ffff:127.0.0.1]", "127.1"])
def test_f16_2_other_spellings_of_a_forbidden_address_are_refused(spelling):
    assert is_forbidden(f"http://{spelling}:6500/x", ["127.0.0.1:6500"])


def test_f16_2_unspecified_ipv6_counts_as_local():
    if not socket.has_ipv6:
        pytest.skip("no IPv6")
    assert is_forbidden("http://[::]:6500/x", ["[::1]:6500"])


def test_f16_2_a_different_host_is_still_allowed():
    assert not is_forbidden("http://127.0.0.1:6500/x", ["10.255.255.1:6500"])


def test_wide_characters_escape_at_a_fixed_width():
    assert cli.visible("\U000e0100d") != cli.visible("\U0000e010" + "0d")
    assert cli.visible("\U000e0100d") == "\\U000e0100d"


def test_source_id_with_a_newline_is_refused():
    with pytest.raises(ValueError, match="unusable source_id"):
        Record("note:a\nb", "default", "note", "x")


def test_user_config_with_a_bom_is_read(tmp_path, user_config):
    p = user_config('namespace = "bom"\n')
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    assert config_mod.load(tmp_path).namespace == "bom"


def test_zero_or_bad_user_timeout_is_a_config_error(tmp_path, user_config):
    user_config("[isolation]\ntimeout = 0\n")
    with pytest.raises(config_mod.ConfigError, match="positive"):
        config_mod.load(tmp_path)


@pytest.mark.skipif(sys.platform == "win32" or __import__("os").geteuid() == 0,
                    reason="POSIX permissions, not as root")
def test_unreadable_search_path_is_a_clean_config_error(tmp_path, capsys):
    nox = tmp_path / "nox"
    (nox / "x").mkdir(parents=True)
    nox.chmod(0o600)
    try:
        assert cli.main(["list", "--cwd", str(nox / "x")]) == 2
        assert "cannot search" in capsys.readouterr().err
    finally:
        nox.chmod(0o700)
