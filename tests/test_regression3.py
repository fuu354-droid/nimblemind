"""Third regression round: pin what R3-1 and R3-2 found unpinned in the
post-prompt config re-read of `pending approve`, and the project timeout
boundary."""

import pytest

from nimblemind import cli, config as config_mod, writes


def test_r3_1_approve_with_namespace_lands_in_that_namespace(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "confirm", lambda q: "t")
    r = writes.write(cfg(), "cli", "note", "for ns2", namespace="ns2")
    assert cli.main(["pending", "approve", r.entry_id, "--namespace", "ns2"]) == 0
    c = cfg()
    assert [x.source_id for x in writes.canonical(c).list("ns2")] == [r.source_id]
    assert writes.canonical(c).list("default") == []


def test_r3_1_a_namespace_change_during_the_prompt_keeps_the_shown_namespace(cfg, user_config, monkeypatch):
    r = writes.write(cfg(), "cli", "note", "shown in default")

    def confirm_then_switch(q):
        user_config('namespace = "elsewhere"\n')
        return "t"

    monkeypatch.setattr(cli, "confirm", confirm_then_switch)
    assert cli.main(["pending", "approve", r.entry_id]) == 0
    assert [x.source_id for x in writes.canonical(cfg()).list("default")] == [r.source_id]


def test_r3_2_a_deny_in_the_cwd_project_during_the_prompt_applies(cfg, tmp_path, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    r = writes.write(config_mod.load(proj), "cli", "note", "queued")

    def confirm_then_tighten(q):
        (proj / ".nimblemind.toml").write_text('[approval]\ncli = "deny"\n', encoding="utf-8")
        return "t"

    monkeypatch.setattr(cli, "confirm", confirm_then_tighten)
    assert cli.main(["pending", "approve", r.entry_id, "--cwd", str(proj)]) == 1
    assert "is deny" in capsys.readouterr().out
    assert writes.canonical(cfg()).list("default") == []


@pytest.mark.parametrize("value, ok", [(86400, True), (86401, False)])
def test_trusted_project_timeout_cap_boundary(tmp_path, value, ok):
    f = tmp_path / ".nimblemind.toml"
    f.write_text(f"[isolation]\ntimeout = {value}\n", encoding="utf-8")
    config_mod.trust(f)
    if ok:
        assert config_mod.load(tmp_path)["isolation"]["timeout"] == 60.0  # min with the user's 60
    else:
        with pytest.raises(config_mod.ConfigError, match="at most 86400"):
            config_mod.load(tmp_path)
