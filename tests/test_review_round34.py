"""Regression tests for the thirty-fourth review round (F34-1, O34-2)."""

import pytest

from nimblemind import cli, config as config_mod, writes
from nimblemind.backends import BackendError
from nimblemind.backends.memila import MemilaBackend

BIG = "9" * 401


def test_f34_1_huge_integer_floor_in_a_trusted_project_is_a_config_error(tmp_path, capsys):
    # `curiosity` applies from a project only once trusted (v0.2.0).
    f = tmp_path / ".nimblemind.toml"
    f.write_text(f"[curiosity]\nconfidence_floor = {BIG}\n", encoding="utf-8")
    config_mod.trust(f)
    with pytest.raises(config_mod.ConfigError, match="confidence_floor"):
        config_mod.load(tmp_path)
    assert cli.main(["config", "--cwd", str(tmp_path)]) == 2


@pytest.mark.parametrize("section", ["isolation", "distiller", "researcher"])
def test_f34_1_huge_integer_user_timeout_is_a_config_error(tmp_path, user_config, section):
    user_config(f"[{section}]\ntimeout = {BIG}\n")
    with pytest.raises(config_mod.ConfigError, match="timeout"):
        config_mod.load(tmp_path)


def test_f34_1_huge_integer_trusted_project_timeout_is_a_config_error(tmp_path):
    f = tmp_path / ".nimblemind.toml"
    f.write_text(f"[isolation]\ntimeout = {BIG}\n", encoding="utf-8")
    config_mod.trust(f)
    with pytest.raises(config_mod.ConfigError, match="timeout"):
        config_mod.load(tmp_path)


def test_f34_1_huge_integer_backend_timeout_is_refused():
    with pytest.raises(BackendError, match="at most"):
        MemilaBackend.from_config({"name": "memila", "url": "http://127.0.0.1:1", "timeout": int(BIG)})


def test_o34_2_a_deny_set_while_the_prompt_is_open_applies(cfg, user_config, monkeypatch, capsys):
    r = writes.write(cfg(), "cli", "note", "queued")

    def confirm_then_tighten(question):
        user_config('[approval]\ncli = "deny"\n')  # changed while the prompt is open
        return "t"

    monkeypatch.setattr(cli, "confirm", confirm_then_tighten)
    assert cli.main(["pending", "approve", r.entry_id]) == 1
    assert "is deny" in capsys.readouterr().out
    assert writes.canonical(cfg()).list("default") == []
