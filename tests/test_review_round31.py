"""Regression tests for the thirty-first review round (F31-1, O31-1)."""

import json
import math

import pytest

from nimblemind import cli, config as config_mod
from nimblemind.backends.memila import MemilaBackend


def strict_loads(text):
    def refuse(name):
        raise ValueError(f"non-JSON constant {name}")
    return json.loads(text, parse_constant=refuse)


def test_f31_1_config_json_is_strict_with_non_finite_free_keys(tmp_path, monkeypatch, capsys):
    (tmp_path / ".nimblemind.toml").write_text("[journal]\nextra = nan\n[curiosity]\nmore = inf\n",
                                               encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    assert cli.main(["config", "--json"]) == 0
    data = strict_loads(capsys.readouterr().out)
    assert data["effective"]["journal"]["extra"] == "nan" and data["effective"]["curiosity"]["more"] == "inf"


def test_f31_1_config_text_is_strict_too(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NIMBLEMIND_BACKEND_TIMEOUT", "nan")
    assert cli.main(["config", "--cwd", str(tmp_path)]) == 0
    strict_loads(capsys.readouterr().out)


def test_f31_1_non_finite_backend_scores_are_dropped(monkeypatch):
    b = MemilaBackend("http://127.0.0.1:1")
    monkeypatch.setattr(b, "_call", lambda *a, **k: {"hits": [
        {"source": "x", "text": "hello", "score": float("nan"), "rerank_score": float("inf"),
         "dense_score": 0.5}]})
    [ev] = b.recall("hello", "ns", 3)
    assert ev.scores == {"dense_score": 0.5}


def test_jsonable_handles_every_shape():
    out = cli._jsonable({"a": [math.inf, -math.inf, math.nan, 1.5, None, True], "b": (1, "x")})
    assert out == {"a": ["inf", "-inf", "nan", 1.5, None, True], "b": [1, "x"]}


@pytest.mark.parametrize("user", ["[isolation]\nchecks = 5\n", "[isolation]\ndeny = 5\n",
                                  "[isolation]\nidentity = 5\n", "[isolation.identity]\na = 5\n",
                                  '[isolation]\nchecks = "builtin-secrets"\n'])
def test_o31_1_mistyped_user_isolation_under_a_project_is_a_config_error(tmp_path, user_config, user):
    user_config(user)
    (tmp_path / ".nimblemind.toml").write_text("# empty\n", encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="list of strings"):
        config_mod.load(tmp_path)
