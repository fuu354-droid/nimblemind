"""Regression tests for the eighth review round (Codex, F8-1..F8-3)."""

import threading

from nimblemind import cli, config as config_mod, writes
from nimblemind.backends.local import LocalBackend
from conftest import toml_list


def test_f8_1_distill_without_cwd_applies_the_project(tmp_path, user_config, fake_model, cfg, monkeypatch):
    body = "\n".join(f"## {s}\n- blockedword" for s in cfg()["journal"]["sections"])
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(body))}\n')
    config_mod.set_grant("distill", True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".nimblemind.toml").write_text('[isolation]\ndeny = ["blockedword"]\n', encoding="utf-8")
    t = tmp_path / "t.txt"
    t.write_text("USER: a\nUSER: b\nUSER: c\n", encoding="utf-8")
    monkeypatch.chdir(proj)
    assert cli.main(["distill", "--transcript", str(t), "--harness", "generic"]) == 0
    c = config_mod.load(proj)
    assert writes.canonical(c).list("default") == [] and len(writes.held(c)) == 1


def test_f8_2_overlapping_writes_to_one_id_stay_auditable(cfg, monkeypatch):
    config_mod.set_grant("cli", True)
    c = cfg()
    gate, entered = threading.Event(), threading.Event()
    real_store = LocalBackend.store

    def slow_store(self, rec):
        out = real_store(self, rec)
        if rec.content == "first":
            entered.set()
            gate.wait(5)
        return out

    monkeypatch.setattr(LocalBackend, "store", slow_store)
    a = threading.Thread(target=writes.write, args=(c, "cli", "note", "first"),
                         kwargs={"source_id": "shared"})
    a.start()
    assert entered.wait(5)
    b = threading.Thread(target=writes.write, args=(c, "cli", "note", "second"),
                         kwargs={"source_id": "shared"})
    b.start()
    b.join(0.5)  # B must wait for A's commit to finish
    gate.set()
    a.join(5)
    b.join(5)
    assert writes.canonical(c).get("shared", "default").content == "second"
    assert writes.audit(c) == []


def test_f8_3_backend_scalars_come_from_the_environment(cfg, user_config, monkeypatch):
    user_config('[backend]\nname = "memila"\nurl = "http://127.0.0.1:1234"\ntimeout = 30\n')
    monkeypatch.setenv("NIMBLEMIND_BACKEND_URL", "http://127.0.0.1:9999")
    monkeypatch.setenv("NIMBLEMIND_BACKEND_TIMEOUT", "2.5")
    b = cfg()["backend"]
    assert b["url"] == "http://127.0.0.1:9999" and b["timeout"] == 2.5
