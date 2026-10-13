"""Regression tests for the fourth review round (Codex, F4-1..F4-7)."""

import json
import subprocess
import sys

import pytest

from nimblemind import cli, config as config_mod, distill, recall, writes
from conftest import toml_list


def test_f4_1_a_backend_that_fails_to_load_still_leaves_an_audited_retryable_record(cfg, user_config):
    user_config('[backend]\nname = "plugin"\ntarget = "missing_review_backend:Backend"\n')
    c = cfg()
    r = writes.write(c, "cli", "note", "x", source_id="note:1", approved_by="t")
    assert r.state == writes.STORED and "flush" in r.detail
    assert writes.audit(c) == []
    assert json.loads((c.layout().records.parent / "unsynced.json").read_text()) == ["note:1"]


def test_f4_2_session_start_survives_a_full_stdout(cfg, tmp_path):
    writes.write(cfg(), "cli", "journal", "## Unfinished\n- x", source_id="session:1", approved_by="t")
    with open("/dev/full", "w") if sys.platform != "win32" else open(tmp_path / "x", "w") as out:
        r = subprocess.run([sys.executable, "-m", "nimblemind", "hook", "session-start"],
                           input=json.dumps({"cwd": str(tmp_path)}), text=True,
                           stdout=out, stderr=subprocess.PIPE, timeout=60)
    assert (r.returncode, r.stderr) == (0, "")


def test_f4_3_journal_with_empty_recall_sections_adds_nothing(cfg, user_config):
    config_mod.set_grant("cli", True)
    c = cfg()
    sections = c["journal"]["sections"]
    older_body = "\n".join(f"## {s}\n" + ("- decisions ONLY" if s == "Decisions" else "- none")
                           for s in sections)
    writes.write(c, "cli", "journal", older_body, source_id="session:old")
    writes.write(c, "cli", "note", "a plain unfinished note", source_id="note:1")
    out = recall.older(c, "session:new", 3)
    assert not any("decisions ONLY" in o for o in out)
    assert any("plain unfinished note" in o for o in out)


def test_f4_3_zero_limit_returns_nothing(cfg):
    c = cfg()
    writes.write(c, "cli", "note", "unfinished thing", source_id="note:1", approved_by="t")
    assert len(recall.older(c, "", 1)) == 1
    assert recall.older(c, "", 0) == []


def test_f4_4_refused_content_is_never_put_to_the_approver(cfg, monkeypatch):
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or "t")
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = cli.main(["store", "--approve", "--text", "ghp_" + "a" * 36])
    assert asked == [] and rc == 1 and len(writes.held(cfg())) == 1
    cli.main(["store", "--approve", "--text", "fine"])
    assert len(asked) == 1


def test_f4_5_session_end_passes_the_harness_override_to_the_child(tmp_path, monkeypatch, cfg):
    spawned = []
    monkeypatch.setattr(distill, "spawn", lambda *a: spawned.append(a))
    t = tmp_path / "t.txt"
    t.write_text("USER: a\nUSER: b\nUSER: c\n", encoding="utf-8")
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(
        {"transcript_path": str(t), "session_id": "s", "cwd": str(tmp_path)})))
    cli.main(["hook", "session-end", "--harness", "generic"])
    assert spawned and spawned[0][3] == "generic"


def test_f4_5_child_distills_with_the_passed_harness(tmp_path, user_config, fake_model, cfg):
    sections = "\n".join(f"## {s}\n- none" for s in cfg()["journal"]["sections"])
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(sections))}\n')
    t = tmp_path / "t.txt"
    t.write_text("USER: a\nUSER: b\nUSER: c\n", encoding="utf-8")
    rc = cli.main(["distill", "--transcript", str(t), "--session-id", "s", "--cwd", str(tmp_path),
                   "--harness", "generic"])
    assert rc == 0 and len(writes.pending(cfg())) == 1


def test_f4_7_migrate_accepts_from(tmp_path, capsys):
    src = tmp_path / "old"
    src.mkdir()
    (src / "a.md").write_text("x", encoding="utf-8")
    assert cli.main(["migrate", "--from", str(src), "--dry-run"]) == 0
    assert "would import a.md" in capsys.readouterr().out


def test_f4_5_spawn_puts_the_harness_on_the_child_command(monkeypatch):
    seen = []
    monkeypatch.setattr(distill.subprocess, "Popen", lambda argv, **kw: seen.append(argv))
    distill.spawn("/t.jsonl", "s", "/cwd", "generic")
    assert seen[0][-1] == "--harness=generic"
