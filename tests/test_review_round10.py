"""Regression tests for the tenth review round (F10-1..F10-3)."""

import json
import os
import subprocess
import sys
import time

import pytest

from nimblemind import cli, config as config_mod, distill, writes


def test_f10_1_distill_child_ignores_a_package_planted_in_the_project(tmp_path, monkeypatch):
    repo = tmp_path / "evilrepo"
    (repo / "nimblemind").mkdir(parents=True)
    marker = tmp_path / "PWNED"
    payload = f"open({str(marker)!r}, 'w').write('ran')\n"
    (repo / "nimblemind" / "__init__.py").write_text(payload, encoding="utf-8")
    (repo / "nimblemind" / "__main__.py").write_text(payload, encoding="utf-8")
    (repo / "json.py").write_text(payload, encoding="utf-8")
    t = repo / "t.jsonl"
    t.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")
    monkeypatch.chdir(repo)
    distill.spawn(str(t), "s1", str(repo), "claude-code")
    time.sleep(3)
    assert not marker.exists()


def test_f10_1_spawn_runs_outside_the_project_without_cwd_on_sys_path(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(distill.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, **kw))
    monkeypatch.chdir(tmp_path)
    distill.spawn("/t", "s", str(tmp_path), "generic")
    assert seen["argv"][1] == "-P"
    assert os.path.realpath(seen["cwd"]) != os.path.realpath(tmp_path)


def test_f10_2_audit_covers_every_namespace(cfg):
    c = cfg()
    writes.write(c, "cli", "note", "a", source_id="note:a", approved_by="t")
    writes.write(c, "cli", "note", "b", source_id="note:b", namespace="other", approved_by="t")
    assert writes.audit(c) == []
    f = next(p for p in c.layout("other").records.glob("*.json") if p.name != "index.json")
    d = json.loads(f.read_text(encoding="utf-8"))
    d["content"] = "tampered"
    f.write_text(json.dumps(d), encoding="utf-8")
    assert writes.audit(c, "default") == []
    assert writes.audit(c) == ["other/note:b: content differs from what was approved"]
    assert cli.main(["audit"]) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_f10_3_trust_of_a_symlinked_file_trusts_nothing(tmp_path, monkeypatch):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / ".nimblemind.toml").write_text('namespace = "a"\n', encoding="utf-8")
    os.symlink(a / ".nimblemind.toml", b / ".nimblemind.toml")
    monkeypatch.setattr(cli, "confirm", lambda q: "t")
    monkeypatch.chdir(b)
    with pytest.raises(SystemExit, match="symlink"):
        cli.main(["trust", ".nimblemind.toml"])
    assert not config_mod.is_trusted(a / ".nimblemind.toml")


def test_user_config_scalar_section_is_a_clean_error(user_config, capsys):
    user_config("budget = 3\n")
    assert cli.main(["list"]) == 2
    assert "must be a table" in capsys.readouterr().err


def test_store_with_a_bad_source_id_is_a_clean_error(cfg):
    with pytest.raises(SystemExit, match="store: unusable source_id"):
        cli.main(["store", "--text", "x", "--source-id", "a/b"])


def test_f10_1_isolation_command_runs_outside_the_project(cfg, user_config, tmp_path, monkeypatch):
    from conftest import toml_list

    out = tmp_path / "cwd.txt"
    script = tmp_path / "where.py"
    script.write_text(f"import os\nopen({str(out)!r}, 'w').write(os.getcwd())\n", encoding="utf-8")
    user_config(f'[isolation]\nchecks = ["command"]\ncommand = {toml_list([sys.executable, str(script), "{file}"])}\n')
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    writes.write(config_mod.load(proj), "cli", "note", "x", approved_by="t")
    assert os.path.realpath(out.read_text()) != os.path.realpath(proj)
