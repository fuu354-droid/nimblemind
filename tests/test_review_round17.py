"""Regression tests for the seventeenth review round (F17-1, F17-2)."""

import pytest

from nimblemind import config as config_mod
from nimblemind.backends.memila import is_forbidden


@pytest.mark.parametrize("url", ["http://127.0.0.1%3A6500/x", "http://127.0.0.1%3a6500/x",
                                 "http://%6c%6fcalhost:6500/x", "http://[::1]%3A6500/x"])
def test_f17_1_percent_encoded_hosts_are_refused(url):
    assert is_forbidden(url, ["127.0.0.1:6500"])


def test_f17_1_plain_allowed_url_still_passes():
    assert not is_forbidden("http://127.0.0.1:6452/x", ["127.0.0.1:6500"])


@pytest.mark.parametrize("url, entry", [("http://127.0.0.1:1234/x", "127.0.0.1"),
                                        ("http://127.0.0.1:1234/x", "localhost"),
                                        ("http://[::1]:1234/x", "[::1]")])
def test_host_only_entry_means_every_port(url, entry):
    assert is_forbidden(url, [entry])
    assert not is_forbidden(url, [entry + ":1"] if not entry.startswith("[") else ["[::1]:1"])


def test_f17_2_grant_applies_with_an_unusual_user_config_path(tmp_path, monkeypatch):
    odd = tmp_path / "cfg\U0001F600" / "config.toml"
    odd.parent.mkdir()
    odd.write_text("", encoding="utf-8")
    monkeypatch.setenv("NIMBLEMIND_CONFIG", str(odd))
    assert config_mod.set_grant("cli", True) == {"cli"}
    assert config_mod.grants() == {"cli"}
    assert config_mod.load(tmp_path)["approval"]["cli"] == "allow"


def test_float_timeout_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("NIMBLEMIND_ISOLATION_TIMEOUT", "0.5")
    assert config_mod.load(tmp_path)["isolation"]["timeout"] == 0.5


def test_user_deny_must_be_a_list(tmp_path, user_config):
    user_config('[isolation]\ndeny = "(secret"\n')
    with pytest.raises(config_mod.ConfigError, match="list of strings"):
        config_mod.load(tmp_path)
