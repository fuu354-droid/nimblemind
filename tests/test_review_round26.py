"""Regression tests for the twenty-sixth review round (F26-1)."""

import json

from nimblemind import cli, writes

MEMILA_DOWN = '[backend]\nname = "memila"\nurl = "http://127.0.0.1:9"\ntimeout = 1\n'


def test_f26_1_the_repair_message_names_the_record_and_following_it_resends(cfg, user_config):
    user_config(MEMILA_DOWN)
    c = cfg()
    retry = c.layout().records.parent / "unsynced.json"
    retry.parent.mkdir(parents=True, exist_ok=True)
    retry.write_text("{bad", encoding="utf-8")
    r = writes.write(c, "cli", "note", "second", source_id="note:2", approved_by="t")
    assert '"note:2"' in r.detail and "add" in r.detail
    retry.write_text(json.dumps(["note:2"]), encoding="utf-8")  # the repair, as told
    assert writes.flush(c) == (0, 1)  # memila is still down, but the record is listed


def test_held_discard_unknown_id_fails(cfg, capsys):
    assert cli.main(["held", "discard", "nope"]) == 1
    assert "FAIL nope" in capsys.readouterr().out


def test_recall_with_a_raising_plugin_is_unavailable(cfg, user_config, tmp_path, monkeypatch, capsys):
    mod = tmp_path / "boomplugin.py"
    mod.write_text(
        "class B:\n    name = 'boom'\n    def __init__(self, **k): pass\n"
        "    def store(self, r): return r.source_id\n"
        "    def recall(self, q, ns, k): raise RuntimeError('plugin broke')\n"
        "    def delete(self, s, ns): return True\n    def list(self, ns): return []\n",
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    user_config('[backend]\nname = "plugin"\ntarget = "boomplugin:B"\n')
    assert cli.main(["recall", "x"]) == 1
    assert "unavailable" in capsys.readouterr().out
