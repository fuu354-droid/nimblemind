"""Second regression round: pin what R2-1 and its observations found
unpinned (tests only, no behavior change)."""

import json

import pytest

from nimblemind import cli, config as config_mod, paths, writes


def test_r2_1_audit_json_reports_a_tampered_record(cfg, capsys):
    c = cfg()
    writes.write(c, "cli", "note", "original", source_id="note:1", approved_by="t")
    f = next(p for p in c.layout().records.glob("*.json") if p.name != "index.json")
    d = json.loads(f.read_text(encoding="utf-8"))
    d["content"] = "tampered"
    f.write_text(json.dumps(d), encoding="utf-8")
    assert cli.main(["audit", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["clean"] is False
    assert len(out["problems"]) == 1 and "note:1" in out["problems"][0]


@pytest.mark.parametrize("text", ["x = " + "9" * 5000 + "\n", "x = " + "[" * 900 + "]" * 900 + "\n"])
def test_unconvertible_user_toml_is_a_config_error(tmp_path, user_config, capsys, text):
    user_config(text)
    with pytest.raises(config_mod.ConfigError):
        config_mod.load(tmp_path)
    assert cli.main(["config", "--cwd", str(tmp_path)]) == 2
    assert "config error" in capsys.readouterr().err


def test_unconvertible_user_toml_does_not_break_the_data_home_lookup(tmp_path, user_config, monkeypatch):
    monkeypatch.delenv("NIMBLEMIND_HOME")
    user_config("home = " + "9" * 5000 + "\n")
    assert paths.data_home() == paths.default_data_home()
