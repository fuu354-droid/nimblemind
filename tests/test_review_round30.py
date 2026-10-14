"""Regression tests for the thirtieth review round (F30-1, O30-1)."""

import pytest

from nimblemind import config as config_mod


@pytest.mark.parametrize("text", ["[journal]\nmax_words = inf\n", "[journal]\nmin_turns = nan\n",
                                  "[journal]\nmax_words = \"500\"\n", "[journal]\nmin_turns = 0\n",
                                  "[curiosity]\nconfidence_floor = nan\n",
                                  "[curiosity]\nconfidence_floor = 101\n"])
def test_f30_1_non_finite_or_mistyped_values_are_config_errors(tmp_path, user_config, text):
    # `curiosity` needs trust in a project config since v0.2.0 (DESIGN.md
    # section 4), so its values are checked from the user's own config.
    if text.startswith("[curiosity]"):
        user_config(text)
    else:
        (tmp_path / ".nimblemind.toml").write_text(text, encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(tmp_path)


def test_f30_1_ordinary_values_load(tmp_path, user_config):
    user_config("[curiosity]\nconfidence_floor = 62.5\n")
    (tmp_path / ".nimblemind.toml").write_text(
        "[journal]\nmax_words = 300\nmin_turns = 2\n", encoding="utf-8")
    c = config_mod.load(tmp_path)
    assert c["journal"]["max_words"] == 300 and c["curiosity"]["confidence_floor"] == 62.5


@pytest.mark.parametrize("user, project", [
    ('[budget]\ntotal_chars = "6000"\n', "[budget]\ntotal_chars = 100\n"),
    ("[approval]\ndistill = 5\n", '[approval]\ndistill = "ask"\n'),
])
def test_o30_1_wrong_user_type_under_a_narrowing_project_is_a_config_error(tmp_path, user_config, user, project):
    user_config(user)
    (tmp_path / ".nimblemind.toml").write_text(project, encoding="utf-8")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(tmp_path)
