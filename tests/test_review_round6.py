"""Regression tests for the sixth review round (Codex, F6-1..F6-4)."""

import io
import json
import sys

from nimblemind import cli, config as config_mod, writes


def seed(cfg):
    writes.write(cfg(), "cli", "journal", "## Unfinished\n- private core",
                 source_id="session:1", approved_by="t")


def run_hook(monkeypatch, stdin, *extra):
    monkeypatch.setattr(sys, "stdin", stdin)
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "session-start", *extra]) == 0
    return out.getvalue()


def test_f6_1_explicit_user_ask_beats_a_grant(cfg, user_config):
    config_mod.set_grant("cli", True)
    assert cfg()["approval"]["cli"] == "allow"
    user_config('[approval]\ncli = "ask"\n')
    c = cfg()
    assert c["approval"]["cli"] == "ask"
    assert writes.write(c, "cli", "note", "x").state == writes.PENDING


def project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".nimblemind.toml").write_text("[budget]\ntotal_chars = 0\n", encoding="utf-8")
    return proj


def named_project(tmp_path):
    """A trusted project whose namespace shows up in the injected label, so
    a test sees the project config was loaded (not just that nothing was
    printed, which a failing hook also gives)."""
    proj = tmp_path / "named"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text('namespace = "projns"\n', encoding="utf-8")
    config_mod.trust(f)
    writes.write(config_mod.load(proj), "cli", "journal", "## Unfinished\n- project core",
                 source_id="session:p", approved_by="t")
    return proj


def test_f6_2_empty_generic_input_still_loads_the_project(cfg, user_config, tmp_path, monkeypatch):
    user_config('harness = "generic"\n')
    seed(cfg)
    proj = named_project(tmp_path)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("PWD", str(proj))
    for payload in ("", "[1, 2]"):
        out = run_hook(monkeypatch, io.StringIO(payload))
        assert "[nimblemind projns]" in out and "project core" in out, payload


def test_f6_2_claude_code_payload_without_cwd_uses_the_process_directory(cfg, tmp_path, monkeypatch):
    seed(cfg)
    proj = named_project(tmp_path)
    monkeypatch.chdir(proj)
    monkeypatch.setenv("PWD", str(proj))
    out = run_hook(monkeypatch, io.StringIO("{}"))
    assert "[nimblemind projns]" in out and "project core" in out


def test_f6_3_malformed_payload_injects_nothing(cfg, tmp_path, monkeypatch):
    seed(cfg)
    monkeypatch.chdir(tmp_path)
    assert "private core" in run_hook(monkeypatch, io.StringIO("{}"))
    assert run_hook(monkeypatch, io.StringIO("{broken")) == ""
    assert run_hook(monkeypatch, io.StringIO("[]")) == ""


def test_f6_3_unreadable_stdin_injects_nothing(cfg, tmp_path, monkeypatch):
    seed(cfg)
    monkeypatch.chdir(tmp_path)

    class Broken(io.StringIO):
        def isatty(self):
            return False

        def read(self, *a):
            raise OSError("broken pipe")

    assert run_hook(monkeypatch, Broken()) == ""


def test_f6_4_show_json_is_json(cfg, capsys):
    r = writes.write(cfg(), "cli", "note", "pending JSON probe")
    assert cli.main(["pending", "show", r.entry_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["record"]["content"] == "pending JSON probe"
    h = writes.write(cfg(), "cli", "note", "AKIAABCDEFGHIJKLMNOP", approved_by="t")
    assert cli.main(["held", "show", h.entry_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["verdict"] == "refused"
