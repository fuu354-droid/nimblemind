"""Regression tests for v0.2.0 adversarial round 12: a session with no id."""
import io, json, sys
import pytest
from conftest import toml_list
from nimblemind import cli, config as config_mod, distill, learn, recall, research, writes
from test_learning import _gap, _hook, draft, jtext, one, no_model  # noqa: F401

LESSON = "pin compose healthchecks with a start period for compose services"


def _script(tmp_path, name, reply):
    out = tmp_path / f"{name}.txt"
    out.write_text(reply, encoding="utf-8")
    s = tmp_path / f"{name}.py"
    s.write_text(f"import sys\nsys.stdin.read()\nsys.stdout.write(open({str(out)!r}).read())\n")
    return [sys.executable, str(s)]


@pytest.mark.parametrize("sid", [None, "s-real-1"])
def test_shown_answer_session_without_id(cfg, user_config, tmp_path, monkeypatch, sid):
    ans_reply = ("## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
                 + draft(statement="pin compose healthchecks with a start period", applies="compose services"))
    journal = jtext(Lessons_learned=f"- {LESSON}")
    user_config(f'harness = "generic"\n[distiller]\ncommand = "argv"\nargv = {toml_list(_script(tmp_path, "d", journal))}\n'
                f'[researcher]\ncommand = "argv"\nargv = {toml_list(_script(tmp_path, "r", ans_reply))}\n'
                '[learn]\nauto = false\n')
    config_mod.set_grant("research", True)
    config_mod.set_grant("distill", True)
    c = cfg()
    _gap(c, "why do compose services fail on the first start")
    research.run(c, 1)
    ans = learn.stored(c, "answer")[0].source_id
    learn.run(c)
    bid = learn.report(c)[0]["bid"]
    (tmp_path / "d.txt").write_text(journal + f"\n## Belief checks\n- {bid}: n/a: not relevant\n")
    p = {"cwd": str(tmp_path), "prompt": "why do compose services healthchecks fail on first start period"}
    if sid:
        p["session_id"] = sid
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
    out = io.StringIO(); monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "prompt", "--harness", "claude-code"]) == 0
    sys.stdout = sys.__stdout__
    assert ans in out.getvalue()
    tr = tmp_path / "t.txt"
    tr.write_text("USER: a\nUSER: b\nUSER: c\nUSER: d\nUSER: e\n")
    st = distill.distill(c, str(tr), sid or "unknown")
    j = [r for r in learn.stored(c, "journal")][-1]
    learn.run(c)
    assert one(c, "pin compose healthchecks with a start period")["for"] == 0


from test_learning import L1, L1b, put


def test_r12_a_session_without_an_id_is_keyed_by_its_transcript(cfg, tmp_path, monkeypatch):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    sid = one(c, L1)["source_id"]
    tr = tmp_path / "t.jsonl"
    tr.write_text("{}", encoding="utf-8")
    key = recall.session_key_for("", str(tr))
    assert key.startswith("t:") and recall.session_key_for("unknown", str(tr)) == key
    recall.remember(key, [sid])
    assert recall.injected(recall.session_key_for(" null ", str(tr))) == {sid}


@pytest.mark.parametrize("placeholder", ["", " ", "unknown", "UNKNOWN", "null", "None", "undefined"])
def test_r12_placeholder_ids_are_no_session(placeholder):
    from nimblemind.records import Record
    j = Record(source_id="session:x", namespace="d", kind="journal", content="",
               metadata={"session_id": placeholder})
    assert learn.session_key(j) == "j:session:x"


def test_r12_a_journal_whose_shown_set_is_unknown_gives_no_restatement(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    put(c, "session:2", metadata={"session_id": "unknown", "shown_unknown": True}, Lessons_learned=f"- {L1b}")
    learn.run(c)
    assert one(c, L1)["for"] == 1


def test_r12_a_session_known_only_by_its_transcript_keeps_its_shown_set(cfg, user_config, tmp_path, monkeypatch):
    journal = jtext(Lessons_learned=f"- {L1b}")
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(_script(tmp_path, "d", journal))}\n'
                '[learn]\nauto = false\n')
    config_mod.set_grant("distill", True)
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    (tmp_path / "d.txt").write_text(journal + f"\n## Belief checks\n- {bid}: n/a: x\n", encoding="utf-8")
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps({"type": "user", "origin": {"kind": "human"},
                                       "message": {"content": f"do {i}"}}) for i in range(4)), encoding="utf-8")
    p = {"cwd": str(tmp_path), "transcript_path": str(tr),
         "prompt": "recall the newest journal in full at session start to resume work"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli.main(["hook", "prompt"]) == 0
    sys.stdout = sys.__stdout__
    assert distill.distill(c, str(tr), "") == "stored"
    meta = learn.stored(c, "journal")[-1].metadata
    assert meta["session_id"].startswith("t:") and bid in meta.get("beliefs_shown", [])
    learn.run(c)
    assert one(c, L1)["for"] == 1


def test_r12_a_hook_that_injects_nothing_still_records_the_session(cfg, tmp_path, monkeypatch):
    cfg()
    tr = tmp_path / "t.jsonl"
    tr.write_text("{}", encoding="utf-8")
    p = {"cwd": str(tmp_path), "transcript_path": str(tr), "prompt": "ok"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli.main(["hook", "prompt"]) == 0
    sys.stdout = sys.__stdout__
    assert recall.recorded(recall.session_key_for("", str(tr)))


def test_g2_f1_an_unrecorded_id_less_session_distilled_twice_is_one_vote(cfg, user_config, fake_model, tmp_path):
    from test_learning import _person_belief
    reply = jtext() + "## Belief checks\n- B-0000aaaa: followed: did it\n"
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(reply))}\n[learn]\nauto = false\n')
    config_mod.set_grant("distill", True)
    c = cfg()
    _person_belief(c)
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps({"type": "user", "origin": {"kind": "human"},
                                       "message": {"content": f"do {i}"}}) for i in range(4)), encoding="utf-8")
    for _ in range(2):
        assert distill.distill(c, str(tr), "") == "stored"
    metas = [j.metadata for j in learn.stored(c, "journal")]
    assert all(m["session_id"].startswith("t:") and m["shown_unknown"] for m in metas)
    learn.run(c)
    assert learn.report(c)[0]["for"] == 1        # one session, one outcome vote


def test_g2_o1_a_relative_transcript_is_keyed_like_the_child_keys_it(cfg, tmp_path, monkeypatch):
    cfg()
    (tmp_path / "t.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path), "transcript_path": "t.jsonl",
                                                              "prompt": "ok"})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli.main(["hook", "prompt"]) == 0
    sys.stdout = sys.__stdout__
    assert recall.recorded(recall.session_key_for("", str(tmp_path / "t.jsonl")))
