"""Regression tests for the thirty-second review round (F32-1, F32-2)."""

import pytest

from nimblemind import config as config_mod
from nimblemind.backends.memila import MemilaBackend


@pytest.mark.parametrize("value", ['"x"', "inf", "true"])
def test_f32_1_mistyped_user_timeout_under_a_trusted_project_is_a_config_error(tmp_path, user_config, value):
    user_config(f"[isolation]\ntimeout = {value}\n")
    f = tmp_path / ".nimblemind.toml"
    f.write_text("[isolation]\ntimeout = 5\n", encoding="utf-8")
    config_mod.trust(f)
    with pytest.raises(config_mod.ConfigError, match="timeout"):
        config_mod.load(tmp_path)


def test_f32_2_a_huge_integer_score_is_kept_not_fatal(monkeypatch):
    b = MemilaBackend("http://127.0.0.1:1")
    big = 10 ** 400
    monkeypatch.setattr(b, "_call", lambda *a, **k: {"hits": [{"source": "a", "text": "hello",
                                                                "score": big}]})
    [ev] = b.recall("hello", "ns", 3)
    assert ev.content == "hello" and ev.scores == {"score": big}


@pytest.mark.parametrize("text", ["x = " + "9" * 5000 + "\n", "x = " + "[" * 900 + "]" * 900 + "\n"])
def test_unconvertible_project_toml_is_a_config_error(tmp_path, text):
    (tmp_path / ".nimblemind.toml").write_text(text, encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(tmp_path)


@pytest.mark.parametrize("text", ["[journal]\nsections = 5\n", "[journal]\nrecall_sections = [1]\n",
                                  "[journal]\nbanned_chars = \"x\"\n", "[journal]\nlanguage = 5\n",
                                  "[journal]\nmax_words = " + "9" * 400 + "\n"])
def test_malformed_journal_keys_are_config_errors(tmp_path, text):
    (tmp_path / ".nimblemind.toml").write_text(text, encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(tmp_path)
