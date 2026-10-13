"""Regression tests for the seventh review round (Codex, F7-1..F7-3)."""

import io
import json
import sys

from nimblemind import cli, config as config_mod, writes
from nimblemind.backends import BackendError
from nimblemind.backends.local import LocalBackend


def seed(cfg):
    writes.write(cfg(), "cli", "journal", "## Unfinished\n- private core",
                 source_id="session:1", approved_by="t")


def run_hook(monkeypatch, stdin_text, *extra):
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "session-start", *extra]) == 0
    return out.getvalue()


def restricted_project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".nimblemind.toml").write_text(
        '[approval]\ncli = "deny"\n[budget]\ntotal_chars = 0\n', encoding="utf-8")
    return proj


def test_f7_1_relative_cli_cwd_still_applies_the_project(tmp_path, monkeypatch, capsys):
    proj = restricted_project(tmp_path)
    monkeypatch.chdir(proj)
    rc = cli.main(["store", "--text", "probe", "--cwd", "."])
    assert rc == 1 and capsys.readouterr().out.startswith("rejected")


def test_f7_1_relative_hook_cwd_injects_nothing(cfg, tmp_path, monkeypatch):
    seed(cfg)
    restricted_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert "private core" in run_hook(monkeypatch, json.dumps({"cwd": str(tmp_path)}))
    assert run_hook(monkeypatch, json.dumps({"cwd": "proj"})) == ""
    assert run_hook(monkeypatch, json.dumps({"cwd": "."})) == ""


def test_f7_2_recall_failure_injects_nothing(cfg, tmp_path, monkeypatch):
    seed(cfg)
    monkeypatch.chdir(tmp_path)

    def boom(self, *a):
        raise BackendError("backend unavailable")

    monkeypatch.setattr(LocalBackend, "recall", boom)
    assert run_hook(monkeypatch, json.dumps({"cwd": str(tmp_path)})) == ""


def test_f7_3_trusted_project_harness_reads_the_payload(cfg, tmp_path, monkeypatch):
    seed(cfg)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text('harness = "generic"\n', encoding="utf-8")
    config_mod.trust(f)
    monkeypatch.chdir(proj)
    assert "private core" in run_hook(monkeypatch, "plain text prompt")


def test_f7_3_cli_harness_still_wins(cfg, tmp_path, monkeypatch):
    seed(cfg)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text('harness = "generic"\n', encoding="utf-8")
    config_mod.trust(f)
    monkeypatch.chdir(proj)
    out = run_hook(monkeypatch, json.dumps({"cwd": str(proj)}), "--harness", "claude-code")
    assert "hookSpecificOutput" in out


def test_f7_3_payload_project_harness_is_used_when_the_process_is_elsewhere(cfg, tmp_path, monkeypatch):
    seed(cfg)
    proj = tmp_path / "proj"
    proj.mkdir()
    f = proj / ".nimblemind.toml"
    f.write_text('harness = "generic"\n', encoding="utf-8")
    config_mod.trust(f)
    monkeypatch.chdir(tmp_path)  # not the project: the first guess is claude-code
    out = run_hook(monkeypatch, json.dumps({"cwd": str(proj)}))
    assert "private core" in out and "hookSpecificOutput" not in out  # generic prints plain text
