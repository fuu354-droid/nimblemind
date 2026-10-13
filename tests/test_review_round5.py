"""Regression tests for the fifth review round (Codex, F5-1..F5-5)."""

import io
import json
import sys

import pytest

from nimblemind import cli, config as config_mod, distill, writes
from conftest import toml_list


def flipping_check(tmp_path):
    """An isolation command that refuses on its first run and passes after."""
    script = tmp_path / "flip.py"
    flag = tmp_path / "flip.flag"
    script.write_text(
        "import os, sys\n"
        f"f = {str(flag)!r}\n"
        "if os.path.exists(f): sys.exit(0)\n"
        "open(f, 'w').close(); print('refused'); sys.exit(1)\n", encoding="utf-8")
    return [sys.executable, str(script), "{file}"]


def test_f5_1_one_scan_decides_and_a_refusal_holds(cfg, user_config, tmp_path, monkeypatch):
    user_config(f'[isolation]\nchecks = ["command"]\ncommand = {toml_list(flipping_check(tmp_path))}\n')
    config_mod.set_grant("cli", True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    rc = cli.main(["store", "--approve", "--text", "gate test"])
    c = cfg()
    assert rc == 1 and writes.canonical(c).list("default") == [] and len(writes.held(c)) == 1


def test_f5_2_configured_generic_harness_loads_the_project(cfg, user_config, tmp_path, monkeypatch):
    user_config('harness = "generic"\n')
    writes.write(cfg(), "cli", "journal", "## Unfinished\n- core memory", source_id="session:1",
                 approved_by="t")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".nimblemind.toml").write_text("[budget]\ntotal_chars = 0\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    monkeypatch.setattr(sys, "stdin", io.StringIO("hello"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "session-start"]) == 0
    assert out.getvalue() == ""


def test_f5_3_sessions_sharing_a_prefix_in_one_second_do_not_collide():
    a = {distill.source_id_for("project-session-one") for _ in range(50)}
    b = {distill.source_id_for("project-session-two") for _ in range(50)}
    assert len(a) == 50 and len(b) == 50 and not (a & b)


def test_f5_4_undecodable_check_output_still_holds(cfg, user_config, tmp_path):
    script = tmp_path / "bad.py"
    script.write_text("import os, sys\nos.write(1, bytes([255, 254]))\nsys.exit(1)\n", encoding="utf-8")
    user_config(f'[isolation]\nchecks = ["command"]\ncommand = {toml_list([sys.executable, str(script), "{file}"])}\n')
    r = writes.write(cfg(), "cli", "note", "x", approved_by="t")
    assert r.state == writes.HELD and len(writes.held(cfg())) == 1


def test_f5_4_a_crashing_check_holds(cfg, monkeypatch):
    from nimblemind import isolation
    monkeypatch.setattr(isolation, "check", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    r = writes.write(cfg(), "cli", "note", "x", approved_by="t")
    assert r.state == writes.HELD and "boom" in r.detail


def test_f5_5_pending_approve_never_prompts_for_a_now_refused_entry(cfg, user_config, monkeypatch):
    r = writes.write(cfg(), "cli", "note", "newly forbidden")
    user_config('[isolation]\ndeny = ["newly forbidden"]\n')
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or "t")
    assert cli.main(["pending", "approve", r.entry_id]) == 1
    assert asked == []


def test_f5_5_pending_approve_never_prompts_under_deny(cfg, user_config, monkeypatch):
    r = writes.write(cfg(), "cli", "note", "queued")
    user_config('[approval]\ncli = "deny"\n')
    asked = []
    monkeypatch.setattr(cli, "confirm", lambda q: asked.append(q) or "t")
    assert cli.main(["pending", "approve", r.entry_id]) == 1
    assert asked == []
