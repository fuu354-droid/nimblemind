"""Regression tests for v0.2.0 adversarial round 13: transcript-keyed sessions."""
import io, json, sys
from nimblemind import cli, config as config_mod, distill, learn, recall, writes
from test_learning import L1, L1b, put, one, jtext, no_model  # noqa


def _prompt(monkeypatch, p, harness="claude-code"):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(p) if isinstance(p, dict) else p))
    out = io.StringIO(); monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", "prompt", "--harness", harness]) == 0
    sys.stdout = sys.__stdout__
    return out.getvalue()


PROMPT = "recall the newest journal in full at session start to resume work"


def test_r13_f1_two_idless_sessions_sharing_transcript(cfg, tmp_path, monkeypatch):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    sid = one(c, L1)["bid"]
    tr = tmp_path / "t.txt"; tr.write_text("USER: x\n")
    a = _prompt(monkeypatch, {"cwd": str(tmp_path), "transcript_path": str(tr), "prompt": PROMPT}, "generic")
    # a later, different session of a harness that reuses one transcript file
    tr.write_text("USER: y\n")
    b = _prompt(monkeypatch, {"cwd": str(tmp_path), "transcript_path": str(tr), "prompt": PROMPT}, "generic")
    # same with ids: a second session with its own id gets it
    c2 = _prompt(monkeypatch, {"cwd": str(tmp_path), "session_id": "s2", "transcript_path": str(tr), "prompt": PROMPT})
    assert sid in a and sid in b and sid in c2


from test_learning_round12 import _script, LESSON
from test_learning import _gap, draft
from conftest import toml_list
from nimblemind import research


def test_r13_o1_records_shown_transcript_keyed(cfg, user_config, tmp_path, monkeypatch):
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
    tr = tmp_path / "t.txt"
    tr.write_text("USER: a\nUSER: b\nUSER: c\nUSER: d\nUSER: e\n")
    out = _prompt(monkeypatch, {"cwd": str(tmp_path), "transcript_path": str(tr),
                                "prompt": "why do things fail on the first boot question answer"})
    assert ans in out and bid not in out
    assert distill.distill(c, str(tr), "") == "stored"
    meta = learn.stored(c, "journal")[-1].metadata
    assert ans in meta["records_shown"] and "shown_unknown" not in meta
    learn.run(c)
    assert one(c, "pin compose healthchecks with a start period")["for"] == 0





def test_r14_f1_a_real_id_that_looks_like_a_transcript_key_is_deduplicated(cfg, tmp_path, monkeypatch):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    seen = [bid in _prompt(monkeypatch, {"cwd": str(tmp_path), "session_id": "t:abc", "prompt": PROMPT})
            for _ in range(2)]
    assert seen == [True, False]


def test_r14_o1_session_start_records_under_a_transcript_key(cfg, tmp_path, monkeypatch):
    c = cfg()
    writes.write(c, "cli", "belief", learn.belief_text("B-0000aaaa", L1, "resuming work", "none"),
                 source_id="belief:B-0000aaaa", approved_by="t",
                 metadata={"origin": "person", "supersedes": "", "evidence": []})
    tr = tmp_path / "t.txt"
    tr.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"cwd": str(tmp_path), "transcript_path": str(tr)})))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    assert cli.main(["hook", "session-start", "--harness", "generic"]) == 0
    sys.stdout = sys.__stdout__
    assert "belief:B-0000aaaa" in recall.injected(recall.session_key_for("", str(tr)))


def test_r15_o1_distill_keeps_a_real_id_that_looks_like_a_transcript_key(cfg, user_config, fake_model, tmp_path):
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(jtext()))}\n[learn]\nauto = false\n')
    c = cfg()
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps({"type": "user", "origin": {"kind": "human"},
                                       "message": {"content": f"do {i}"}}) for i in range(4)), encoding="utf-8")
    assert distill.distill(c, str(tr), "t:abc") == "pending"
    meta = writes.pending(c)[0]["record"]["metadata"]
    assert meta["session_id"] == "t:abc" and "shown_unknown" not in meta
