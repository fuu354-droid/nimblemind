"""Regression tests for the twenty-eighth review round (F28-1)."""

import json

import pytest

from nimblemind import cli, writes


@pytest.mark.parametrize("cmd", ["pending", "held"])
def test_f28_1_show_json_stays_json_with_an_unknown_id(cfg, capsys, cmd):
    if cmd == "pending":
        known = writes.write(cfg(), "cli", "note", "x2").entry_id
    else:
        known = writes.write(cfg(), "cli", "note", "AKIAABCDEFGHIJKLMNOP", approved_by="t").entry_id
    assert cli.main([cmd, "show", known, "gone", "--json"]) == 1
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert [e["id"] for e in data] == [known] and "FAIL gone" in captured.err


def test_migrate_of_a_missing_directory_refuses_before_asking(cfg, tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or "t")
    with pytest.raises(SystemExit, match="not a directory"):
        cli.main(["migrate", str(tmp_path / "nope")])
    assert asked == []
