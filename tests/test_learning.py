"""The learning loop, DESIGN.md section 15, and acceptance 8 to 13."""

import io
import json
import sys
import time

import pytest

from conftest import toml_list
from nimblemind import api, approval, cli, config as config_mod, distill, learn, recall, research, writes

SECTIONS = ["Decisions", "Rulings", "Pitfalls", "Surprises", "Changes", "Unfinished",
            "Lessons learned", "Improvements found"]


def jtext(**parts):
    """A journal with the named sections filled (keys use _ for spaces)."""
    filled = {k.replace("_", " "): v for k, v in parts.items()}
    return "\n".join(f"## {s}\n" + (filled.get(s) or "- none") for s in SECTIONS) + "\n"


def put(cfg, sid, metadata=None, **parts):
    time.sleep(0.002)  # distinct creation stamps, in order
    r = writes.write(cfg, "cli", "journal", jtext(**parts), source_id=sid,
                     metadata=metadata or {}, approved_by="t")
    assert r.state == "stored"


def report(c):
    return {r["statement"]: r for r in learn.report(c)}


def one(c, statement):
    return report(c)[statement]


def draft(**fields):
    names = {"statement": "Statement", "applies": "Applies when", "not_when": "Not when",
             "revises": "Revises"}
    return "".join(f"{names[k]}: {v}\n" for k, v in fields.items())


@pytest.fixture
def no_model(user_config):
    user_config('[distiller]\ncommand = "none"\n')


@pytest.fixture
def reviser(user_config, fake_model):
    """A distiller that answers every revision with the same narrowed text."""
    def make(**fields):
        user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(draft(**fields)))}\n')
    return make


L1 = "recall the newest journal in full at session start to resume work"
L1b = "at session start recall the newest journal in full to resume the work"
L1c = "resume work by recalling the newest journal in full at session start"
L1d = "the newest journal recalled in full at session start resumes work"
COUNTER = "- recall of the newest journal in full at session start missed the work that mattered"


# ---- acceptance 8: evolution end to end -----------------------------------

def test_a8_first_observation_is_a_hypothesis(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    assert learn.run(c)["beliefs_created"] == 1
    b = one(c, L1)
    assert b["status"] == "hypothesis" and b["for"] == 1 and b["origin"] == "lessons"
    assert "B-" not in recall.assemble(c)          # a hypothesis is not injected at start


def test_a8_independent_sessions_raise_confidence_then_establish(cfg, no_model):
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c, L1d], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
        learn.run(c)
    b = one(c, L1)
    assert b["for"] == 4 and b["status"] == "established" and b["confidence"] >= 0.8
    assert len(learn.report(c)) == 1               # restatements did not make new beliefs
    assert b["bid"] in recall.assemble(c)


def test_a8_a_session_shown_the_belief_cannot_support_it_by_repeating_it(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    for i in (2, 3, 4):
        put(c, f"session:{i}", metadata={"beliefs_listed": [bid]}, Lessons_learned=f"- {L1b}")
    learn.run(c)
    assert one(c, L1)["for"] == 1 and one(c, L1)["status"] == "hypothesis"


def test_a8_one_session_is_one_vote(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}\n- {L1b}\n- {L1c}")
    learn.run(c)
    assert len(learn.report(c)) == 1 and one(c, L1)["for"] == 1


def test_a8_counter_examples_challenge_revise_and_the_next_session_uses_the_revision(cfg, reviser):
    reviser(statement="recall only the unfinished part of the newest journal at session start",
            applies="resuming the same task as the last session",
            not_when="a session that starts a different task")
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    old = one(c, L1)
    assert old["status"] == "supported"
    put(c, "session:4", metadata={"beliefs_listed": [old["bid"]],
                                  "belief_checks": {old["source_id"]: "followed"}},
        Pitfalls=COUNTER)
    summary = learn.run(c)
    assert summary["beliefs_revised"] == 1
    rows = report(c)
    assert rows[L1]["status"] == "retired" and rows[L1]["against"] == 1
    new = rows["recall only the unfinished part of the newest journal at session start"]
    assert new["supersedes"] == old["source_id"] and new["origin"] == "revision"
    assert new["for"] == 3 and new["against"] == 0  # what held still held
    # The next session works from the corrected belief, not the old one.
    block = recall.assemble(c)
    assert new["bid"] in block and old["bid"] not in block
    assert [b for b, _ in distill._listed_beliefs(c)[0]] == [new["bid"]]
    assert "revised: verifying" in block
    # The history keeps both versions and why it changed.
    chain = learn.history(c, new["source_id"])
    assert [v["source_id"] for v in chain] == [old["source_id"], new["source_id"]]
    assert any(h["status"] == "challenged" for h in chain[0]["history"])
    assert chain[0]["history"][-1]["status"] == "retired"


def test_a8_without_a_drafter_a_challenged_belief_stays_marked_and_asks_why(cfg, no_model):
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    put(c, "session:4", metadata={"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    learn.run(c)
    assert one(c, L1)["status"] == "challenged"
    assert "challenged" in recall.assemble(c)
    gaps = [e["record"] for e in writes.pending(c) if e["record"]["kind"] == "gap"]
    assert [g["metadata"]["signal"] for g in gaps] == ["contradiction"]


def test_a8_repeated_counter_examples_retire_a_belief(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    for i in (2, 3, 4):
        put(c, f"session:{i}", metadata={"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    learn.run(c)
    b = one(c, L1)
    # s = 1, c = 3: confidence 2/9, under 0.25 with two or more against.
    assert b["against"] == 3 and b["status"] == "retired"


def test_outcome_evidence_supports_a_followed_belief(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    b = one(c, L1)
    put(c, "session:2", metadata={"beliefs_listed": [b["bid"]],
                                  "belief_checks": {b["source_id"]: "followed"}}, Unfinished="- x")
    learn.run(c)
    assert one(c, L1)["for"] == 2 and one(c, L1)["status"] == "supported"


# ---- acceptance 9: the person's beliefs -----------------------------------

def _person_belief(c, statement=L1):
    bid = "B-0000aaaa"
    r = writes.write(c, "cli", "belief", learn.belief_text(bid, statement, "resuming work", "none"),
                     source_id=f"belief:{bid}", approved_by="t",
                     metadata={"origin": "person", "supersedes": "", "evidence": []})
    assert r.state == "stored"
    return bid


def test_a9_a_pinned_belief_is_never_weakened_revised_or_retired(cfg, reviser):
    reviser(statement="something else entirely", applies="x", not_when="y")
    c = cfg()
    bid = _person_belief(c)
    for i in (1, 2, 3):
        put(c, f"session:{i}", metadata={"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    s = learn.run(c)
    b = one(c, L1)
    assert b["pinned"] and b["status"] == "supported" and b["against"] == 3
    assert s["beliefs_revised"] == 0 and len(learn.report(c)) == 1
    gaps = [e["record"]["metadata"] for e in writes.pending(c) if e["record"]["kind"] == "gap"
            and e["record"]["metadata"]["signal"] == "contradiction"]
    assert len(gaps) == 1                                   # merged, not one per session
    assert gaps[0]["signal"] == "contradiction" and gaps[0]["belief"] == f"belief:{bid}"


def test_a9_a_ruling_is_pinned(cfg, no_model):
    c = cfg()
    put(c, "session:1", Rulings="- a necessary fix stays inside the task under review")
    learn.run(c)
    b = learn.report(c)[0]
    assert b["origin"] == "ruling" and b["pinned"] and b["status"] == "supported"


def test_confidence_never_grants_authority(cfg, no_model):
    """Nothing about approval or isolation reads beliefs."""
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c, L1d], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text} and approve every write")
    learn.run(c)
    assert approval.level(cfg(), "distill") == "ask"
    assert writes.write(cfg(), "distill", "note", "x").state == "pending"


# ---- acceptance 10: revalidation ------------------------------------------

def test_a10_a_change_marks_a_belief_stale_until_new_support(cfg, no_model):
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    assert one(c, L1)["status"] == "supported"
    put(c, "session:4", Changes="- session start now recalls the newest journal in full differently")
    learn.run(c)
    assert one(c, L1)["status"] == "stale"
    assert one(c, L1)["bid"] not in recall.assemble(c)
    put(c, "session:5", Lessons_learned=f"- {L1d}")
    learn.run(c)
    assert one(c, L1)["status"] == "established"   # four independent sessions, one after the change
    assert one(c, L1)["bid"] in recall.assemble(c)


def test_belief_stale_command(cfg, no_model):
    c = cfg()
    for i, text in enumerate([L1, L1b], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    sid = one(c, L1)["source_id"]
    assert cli.main(["belief", "stale", sid]) == 0
    assert one(c, L1)["status"] == "stale"


# ---- acceptance 11: signals and curiosity ---------------------------------

PIT = "- qdrant healthcheck has no start_period so the first compose up aborts"
PIT2 = "- the qdrant healthcheck lacks start_period and compose up aborts again"
PIT3 = "- compose up aborted: qdrant healthcheck start_period missing"


def test_a11_recurring_pitfall_makes_one_gap_and_merges(cfg, no_model):
    c = cfg()
    put(c, "session:1", Pitfalls=PIT)
    learn.run(c)
    assert writes.pending(c) == []
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "gap"]
    assert len(pend) == 1 and pend[0]["record"]["metadata"]["evidence"] == ["session:1", "session:2"]
    writes.approve(c, pend[0]["id"], by="t")
    before = research.ranked(c)[0]["priority"]
    put(c, "session:3", Pitfalls=PIT3)
    learn.run(c)
    assert [e for e in writes.pending(c) if e["record"]["kind"] == "gap"] == []
    after = research.ranked(c)[0]
    assert after["occurrences"] == 3 and after["priority"] > before


def test_a11_pending_and_held_journals_produce_nothing(cfg, no_model):
    c = cfg()
    for i in (1, 2):
        writes.write(c, "distill", "journal", jtext(Pitfalls=PIT, Lessons_learned=f"- {L1}"),
                     source_id=f"session:{i}")
    learn.run(c)
    assert learn.report(c) == [] and len(writes.pending(c)) == 2


def _gap(c, question, signal="manual", **meta):
    gid = learn.new_id("G", set())
    m = {"signal": signal, "evidence": [], **meta}
    return writes.write(c, "cli", "gap", learn.gap_text(gid, question, signal),
                        source_id=f"gap:{gid}", metadata=m, approved_by="t")


def test_a11_contradiction_outranks_an_uncertain_unimportant_question(cfg, no_model):
    c = cfg()
    _gap(c, "what is the exact colour name of the logo", confidence=5)
    _gap(c, "belief B-1 contradicted", signal="contradiction", evidence=["session:x"], belief="belief:B-1")
    assert [g["signal"] for g in research.ranked(c)] == ["contradiction", "manual"]


def _researcher(user_config, fake_model, reply, extra=""):
    user_config('[distiller]\ncommand = "none"\n[researcher]\ncommand = "argv"\n'
                f"argv = {toml_list(fake_model(reply))}\n" + extra)


def test_a11_parks_after_max_attempts_and_reopens_on_new_evidence(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nnot sure\n\n## Confidence\n20\n")
    for p in ("gap", "research"):
        config_mod.set_grant(p, True)
    c = cfg()
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    assert research.run(c, 1)["results"][0]["status"] == "open"
    assert research.run(c, 1)["results"][0]["status"] == "parked"
    assert research.run(c, 1)["results"] == []
    put(c, "session:3", Pitfalls=PIT3)
    learn.run(c)
    assert research.ranked(c)[0]["status"] == "open"


def test_a11_answered_verifies_after_window_and_recurs_on_signal(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nadd start_period\n\n## Confidence\n90\n",
                extra="[curiosity]\nverify_window = 2\n")
    for p in ("research", "gap"):
        config_mod.set_grant(p, True)
    c = cfg()
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    assert research.run(c, 1)["results"][0]["status"] == "answered"
    put(c, "session:3", Pitfalls=PIT3)
    learn.run(c)
    assert research.ranked(c)[0]["status"] == "recurred"
    assert research.run(c, 1)["results"][0]["status"] == "answered"
    put(c, "session:4", Unfinished="- other")
    put(c, "session:5", Unfinished="- other again")
    learn.run(c)
    assert research.ranked(c)[0]["status"] == "verified"


def test_research_corrects_the_belief_its_gap_is_about(cfg, user_config, fake_model):
    user_config('[distiller]\ncommand = "none"\n')
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    old = one(c, L1)
    reply = ("## Answer\nfull recall crowds out the relevant context\n\n## Confidence\n85\n\n## Belief\n"
             + draft(statement="recall by the task at hand, not by recency", applies="any session start",
                     not_when="none", revises=old["bid"]))
    _researcher(user_config, fake_model, reply)
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "is full recall of the newest journal right", signal="contradiction",
         evidence=["session:3"], belief=old["source_id"])
    out = research.run(c, 1)["results"][0]
    assert out["status"] == "answered"
    rows = report(c)
    assert rows[L1]["status"] == "retired" and rows[L1]["against"] == 1
    assert rows["recall by the task at hand, not by recency"]["supersedes"] == old["source_id"]


def test_research_answers_are_untrusted_and_pending_by_default(cfg, user_config, fake_model):
    _researcher(user_config, fake_model,
                "## Answer\nbecause\n\n## Confidence\n80\n\n## Belief\n" + draft(statement="s", applies="a"))
    c = cfg()
    _gap(c, "why is the sky blue at noon")
    out = research.run(c, 1)["results"][0]
    assert out["answer"].startswith("pending") and learn.report(c) == []   # not stored: no belief
    ans = [e for e in writes.pending(c) if e["record"]["kind"] == "answer"][0]
    assert ans["record"]["content"].startswith("Untrusted evidence")


def test_a_research_hypothesis_needs_independent_support(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
                + draft(statement="pin compose healthchecks with a start period", applies="compose services"))
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "why do compose services fail on the first start")
    research.run(c, 1)
    b = learn.report(c)[0]
    assert b["origin"] == "research" and b["status"] == "hypothesis" and b["for"] == 0


# ---- belief checks in distill ---------------------------------------------

def test_belief_checks_parse_only_listed_ids():
    text = jtext() + "## Belief checks\n- B-1a2b3c4d: violated: skipped\n- B-99999999: followed\n" \
        "- B-1a2b3c4e: maybe\n"
    assert learn.belief_checks(text, {"B-1a2b3c4d", "B-1a2b3c4e"}) == {"belief:B-1a2b3c4d": "violated"}


def _transcript(tmp_path):
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps({"type": "user", "origin": {"kind": "human"},
                                       "message": {"content": f"do {i}"}}) for i in range(4)),
                 encoding="utf-8")
    return t


def test_distill_lists_applying_beliefs_and_stores_checks(cfg, user_config, fake_model, tmp_path):
    reply = jtext() + "## Belief checks\n- B-0000aaaa: violated: committed before claiming\n"
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(reply))}\n')
    c = cfg()
    _person_belief(c)
    assert distill.distill(c, str(_transcript(tmp_path)), "s1") == "pending"
    meta = [e for e in writes.pending(c) if e["record"]["kind"] == "journal"][0]["record"]["metadata"]
    assert meta["beliefs_listed"] == ["B-0000aaaa"]
    assert meta["belief_checks"] == {"belief:B-0000aaaa": "violated"}


def test_distill_rejects_a_journal_missing_checks_when_beliefs_apply(cfg, user_config, fake_model, tmp_path):
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(jtext()))}\n')
    c = cfg()
    _person_belief(c)
    assert distill.distill(c, str(_transcript(tmp_path)), "s1") == "reject"


def test_violated_raises_not_holding_and_is_not_counter_evidence(cfg, no_model):
    c = cfg()
    bid = _person_belief(c)
    put(c, "session:1", metadata={"beliefs_listed": [bid], "belief_checks": {f"belief:{bid}": "violated"}},
        Unfinished="- x")
    learn.run(c)
    gaps = [e["record"]["metadata"] for e in writes.pending(c) if e["record"]["kind"] == "gap"]
    assert [g["signal"] for g in gaps] == ["not-holding"]
    assert one(c, L1)["against"] == 0


# ---- acceptance 12: prompt hook -------------------------------------------

def _hook(monkeypatch, event, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", event]) == 0
    return out.getvalue()


def test_a12_prompt_hook_injects_matches_once_per_session(cfg, tmp_path, monkeypatch):
    c = cfg()
    _person_belief(c, "claim a doing task on the board before an engineering commit")
    writes.write(c, "cli", "note", "the qdrant healthcheck needs a start_period on first compose up",
                 source_id="note:q", approved_by="t")
    p = {"session_id": "s-1", "cwd": str(tmp_path), "prompt": "the qdrant compose healthcheck is failing again"}
    ctx = json.loads(_hook(monkeypatch, "prompt", p))["hookSpecificOutput"]["additionalContext"]
    assert "note:q" in ctx and "prompt]" in ctx
    assert _hook(monkeypatch, "prompt", p) == ""
    p2 = {**p, "prompt": "about to make an engineering commit, board task claimed?"}
    ctx2 = json.loads(_hook(monkeypatch, "prompt", p2))["hookSpecificOutput"]["additionalContext"]
    assert "B-0000aaaa" in ctx2


def test_a12_prompt_hook_marks_hypotheses(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    block, _ = recall.prompt_block(c, "should I recall the newest journal in full at session start?", "s")
    assert "[hypothesis" in block


def test_a12_prompt_hook_respects_its_cap(cfg, user_config):
    user_config("[budget]\nprompt_chars = 300\n")
    c = cfg()
    for i in range(5):
        writes.write(c, "cli", "note", f"qdrant healthcheck compose note {i} " + "z" * 200,
                     source_id=f"note:{i}", approved_by="t")
    block, _ = recall.prompt_block(c, "qdrant healthcheck compose trouble", "s")
    assert block and len(block) <= 300 and "cut to fit" in block


def test_a12_short_prompt_and_failures_are_silent(cfg, tmp_path, monkeypatch):
    c = cfg()
    writes.write(c, "cli", "note", "qdrant healthcheck", source_id="note:q", approved_by="t")
    assert _hook(monkeypatch, "prompt", {"session_id": "s", "cwd": str(tmp_path), "prompt": "ok"}) == ""
    f = learn._state_file(c)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{broken", encoding="utf-8")
    assert _hook(monkeypatch, "prompt", {"session_id": "s", "cwd": str(tmp_path),
                                         "prompt": "qdrant healthcheck compose failing"}) == ""


def test_session_start_ids_are_not_repeated_by_the_prompt_hook(cfg, tmp_path, monkeypatch):
    c = cfg()
    _person_belief(c, "claim a doing task on the board before an engineering commit")
    assert "B-0000aaaa" in _hook(monkeypatch, "session-start", {"session_id": "s-9", "cwd": str(tmp_path)})
    out = _hook(monkeypatch, "prompt", {"session_id": "s-9", "cwd": str(tmp_path),
                                        "prompt": "engineering commit board task claim"})
    assert "B-0000aaaa" not in out


# ---- acceptance 13: session start, isolation, audit -----------------------

def test_a13_core_sections_not_the_whole_journal(cfg):
    c = cfg()
    _person_belief(c)
    put(c, "session:1", Decisions="- an unrelated decision", Unfinished="- resume the hook")
    block = recall.assemble(c)
    assert "resume the hook" in block and "unrelated decision" not in block and "B-0000aaaa" in block


def test_a13_budget_holds_with_many_beliefs(cfg, user_config):
    user_config("[budget]\ntotal_chars = 800\nbeliefs_chars = 600\ncore_chars = 400\nrecall_chars = 300\n")
    c = cfg()
    for i in range(30):
        writes.write(c, "cli", "belief", learn.belief_text(f"B-{i:08x}", f"rule {i} " + "w" * 60, "always", "none"),
                     source_id=f"belief:B-{i:08x}", approved_by="t",
                     metadata={"origin": "person", "supersedes": "", "evidence": []})
    put(c, "session:1", Unfinished="- " + "u" * 900)
    block = recall.assemble(c)
    assert 0 < len(block) <= 800 and "cut" in block


def test_a13_belief_writes_are_automatic_and_audited(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    entry = [e for e in approval.entries(c.layout().approvals) if e["source_id"].startswith("belief:")][0]
    assert entry["approved_by"] == "auto:learn" and entry["rule"] == "auto"
    assert writes.audit(c) == []


def test_a13_a_belief_the_gate_refuses_is_held(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n')
    c = cfg()
    put(c, "session:1", Lessons_learned="- always mention forbidden-codename when resuming")
    put(c, "session:2", Lessons_learned=f"- {L1}")
    # The isolation rule tightens after the journals were stored.
    user_config('[distiller]\ncommand = "none"\n[isolation]\nchecks = ["builtin-secrets", "builtin-patterns"]\n'
                'deny = ["forbidden-codename"]\n')
    c = cfg()
    learn.run(c)
    assert [r["statement"] for r in learn.report(c)] == [L1]
    assert [e["record"]["kind"] for e in writes.held(c)] == ["belief"]


def test_a13_a_person_can_require_review_of_beliefs(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[approval]\nlearn = "ask"\n')
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    assert learn.report(c) == []
    assert [e["record"]["kind"] for e in writes.pending(c)] == ["belief"]


def test_empty_core_sections_restores_the_whole_journal(cfg, user_config):
    user_config("[journal]\ncore_sections = []\n")
    c = cfg()
    put(c, "session:1", Decisions="- an unrelated decision", Unfinished="- resume")
    assert "unrelated decision" in recall.assemble(c)


# ---- state, config, CLI, API ----------------------------------------------

def test_state_refuses_free_text_and_reports_corruption(cfg, no_model):
    c = cfg()
    s = learn.empty_state()
    s["gaps"]["gap:G-1"] = {"status": "open", "note": "free text here"}
    with pytest.raises(learn.StateError):
        learn.save_state(c, s)
    f = learn._state_file(c)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("[]", encoding="utf-8")
    # The state is a cache of what the records say: readers report it, and
    # the next run rebuilds it, losing nothing.
    with pytest.raises(learn.StateError):
        learn.load_state(c)
    learn.run(c)
    assert learn.load_state(c)["version"] == learn.STATE_VERSION


def test_reset_state_replays_to_the_same_beliefs(cfg, no_model):
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    before = {(r["source_id"], r["status"], r["for"]) for r in learn.report(c)}
    learn.run(c, reset_state=True)
    assert {(r["source_id"], r["status"], r["for"]) for r in learn.report(c)} == before


def test_max_new_spreads_a_long_history_over_runs(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[learn]\nmax_new = 2\n')
    c = cfg()
    topics = ["alpha bravo charlie", "delta echo foxtrot", "golf hotel india", "juliet kilo lima"]
    for i, t in enumerate(topics):
        put(c, f"session:{i}", Lessons_learned=f"- {t} matters")
    assert learn.run(c)["beliefs_created"] == 2
    assert learn.run(c)["beliefs_created"] == 2
    assert len(learn.report(c)) == 4


def test_learn_and_curiosity_need_trust_in_a_project(cfg, tmp_path):
    (tmp_path / ".nimblemind.toml").write_text("[learn]\nauto = false\n[curiosity]\nmax_attempts = 9\n",
                                               encoding="utf-8")
    c = cfg()
    assert c["learn"]["auto"] is True and c["curiosity"]["max_attempts"] == 2
    assert any("not trusted" in n for n in c.notes)


@pytest.mark.parametrize("text", ["[learn]\nsimilarity = 0\n", "[learn]\nsimilarity = nan\n",
                                  "[learn]\nrecur_min = 0\n", "[learn]\nwhat = 1\n",
                                  "[learn]\nmax_new = 0\n", "[learn]\nchange_sections = 'x'\n",
                                  "[curiosity]\ncost = {surprise = 9}\n",
                                  "[curiosity]\ncost = {other = 1}\n",
                                  "[curiosity]\ngaps_file = 'x'\n",
                                  "[researcher]\ntools = ['Web Search']\n",
                                  "[budget]\nbeliefs_chars = 99999\n"])
def test_learning_config_is_validated(cfg, user_config, text):
    user_config(text)
    with pytest.raises(config_mod.ConfigError):
        cfg()


def test_cli_round_trip(cfg, no_model, capsys):
    assert cli.main(["gap", "add", "why do sessions hit the usage limit early", "--impact", "4"]) == 0
    assert "pending gap:G-" in capsys.readouterr().out
    assert cli.main(["belief", "add", "--statement", "fix it inside the task",
                     "--applies-when", "a review round finds a defect"]) == 0
    capsys.readouterr()
    assert cli.main(["learn", "--json"]) == 0
    assert "beliefs_created" in json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit):
        cli.main(["gap", "add", "x", "--impact", "9"])


def test_belief_history_and_report_cli(cfg, no_model, capsys):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    sid = one(c, L1)["source_id"]
    assert cli.main(["belief", "history", sid]) == 0
    assert "-> hypothesis" in capsys.readouterr().out
    assert cli.main(["belief", "report", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "hypothesis"


def test_belief_retire_needs_a_terminal(cfg, monkeypatch):
    c = cfg()
    bid = _person_belief(c)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(cli.NotATerminal):
        cli.main(["belief", "retire", bid])
    assert one(c, L1)["status"] == "supported"


def test_api_writes_go_through_provider_approval(cfg):
    assert api.store("a note from another tool about qdrant")["state"] == "pending"
    config_mod.set_grant("provider", True)
    assert api.store("a stored note about qdrant healthcheck", source_id="note:api")["state"] == "stored"
    assert api.recall("qdrant healthcheck")[0]["source_id"] == "note:api"
    assert api.gaps_add("why is it slow", impact=3)["state"] == "stored"
    assert api.gaps_list()[0]["signal"] == "manual"
    with pytest.raises(ValueError):
        api.store("x", kind="belief")
    assert api.beliefs_list() == []
    assert api.delete("note:api") is True


def test_a7_hermes_provider_stub():
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "integrations" / "hermes"))
    try:
        from provider import NimblemindMemoryProvider
    finally:
        sys.path.pop(0)
    config_mod.set_grant("provider", True)
    p = NimblemindMemoryProvider()
    saved = p.save("hermes remembers the qdrant start_period fix", tags=["infra"])
    assert saved["state"] == "stored"
    hits = p.search("qdrant start_period")
    assert hits and hits[0]["id"] == saved["source_id"]
    assert p.forget(saved["source_id"]) is True and p.search("qdrant start_period") == []


# ---- v0.2.0 round 1 findings ----------------------------------------------

def test_r1_f1_a_belief_shown_by_the_prompt_hook_gets_no_support_from_that_session(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    # The prompt hook showed it to session S2; distill records that.
    put(c, "session:2", metadata={"session_id": "S2", "beliefs_shown": [bid]}, Lessons_learned=f"- {L1b}")
    learn.run(c)
    assert one(c, L1)["for"] == 1


def test_r1_f1_distill_records_what_the_hooks_showed(cfg, user_config, fake_model, tmp_path):
    reply = jtext() + "## Belief checks\n- B-0000aaaa: n/a: nothing relevant\n"
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(reply))}\n')
    c = cfg()
    _person_belief(c)
    recall.remember("S9", ["belief:B-0000aaaa", "note:x"])
    assert distill.distill(c, str(_transcript(tmp_path)), "S9") == "pending"
    meta = [e for e in writes.pending(c) if e["record"]["kind"] == "journal"][0]["record"]["metadata"]
    assert meta["beliefs_shown"] == ["B-0000aaaa"]


def test_r1_f2_one_session_distilled_twice_is_one_vote(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    for n in (2, 3, 4):
        put(c, f"session:{n}", metadata={"session_id": "S3"}, Lessons_learned=f"- {L1b}")
    learn.run(c)
    assert one(c, L1)["for"] == 2


def _person_journal(c, sid, **parts):
    time.sleep(0.002)
    assert writes.write(c, "cli", "journal", jtext(**parts), source_id=sid,
                        approved_by="a-person").state == "stored"


def test_r1_f3_a_ruling_never_adopts_a_learned_belief_that_says_the_opposite(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned="- always retry flaky network calls in the deploy script")
    _person_journal(c, "session:2", Rulings="- never retry flaky network calls in the deploy script")
    learn.run(c)
    rows = report(c)
    learned = rows["always retry flaky network calls in the deploy script"]
    ruled = rows["never retry flaky network calls in the deploy script"]
    assert not learned["pinned"] and ruled["pinned"] and ruled["origin"] == "ruling"
    assert "[yours]: always" not in recall.assemble(c)


def test_r1_o7_a_ruling_in_a_granted_journal_is_not_the_persons(cfg, no_model):
    config_mod.set_grant("distill", True)
    c = cfg()
    writes.write(c, "distill", "journal", jtext(Rulings="- ship on fridays"), source_id="session:1")
    learn.run(c)
    b = learn.report(c)[0]
    assert b["origin"] == "lessons" and not b["pinned"]


def test_r1_f4_an_answer_never_revises_a_persons_belief(cfg, user_config, fake_model):
    c = cfg()
    bid = _person_belief(c)
    reply = ("## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
             + draft(statement="a replacement", applies="always", not_when="none", revises=bid))
    _researcher(user_config, fake_model, reply)
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "does it hold", signal="contradiction", evidence=["session:2"], belief=f"belief:{bid}")
    research.run(c, 1)
    rows = report(c)
    assert rows[L1]["status"] == "supported" and rows[L1]["pinned"]
    assert rows["a replacement"]["origin"] == "research" and rows["a replacement"]["supersedes"] == ""


def test_r1_f5_reset_replays_retire_revisions_and_research_identically(cfg, reviser, user_config, fake_model):
    reviser(statement="recall only the unfinished part at session start",
            applies="resuming the same task", not_when="a different task")
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    old = one(c, L1)
    put(c, "session:4", metadata={"beliefs_listed": [old["bid"]]}, Pitfalls=COUNTER)
    learn.run(c)
    put(c, "session:5", Lessons_learned="- delta echo foxtrot matters")
    learn.run(c)
    other = one(c, "delta echo foxtrot matters")
    learn.retire(c, other["source_id"])
    n_records = len(learn.stored(c, "belief"))

    def snapshot():
        return sorted((r["source_id"], r["status"], r["for"], r["against"]) for r in learn.report(c))
    before = snapshot()
    hist_before = [len(v["history"]) for v in learn.history(c, old["source_id"])]
    def transitions():
        return [[(h["status"], h["cause"]) for h in v["history"]] for v in learn.history(c, old["source_id"])]
    hb = transitions()
    assert [s for s, _ in hb[0]] == ["hypothesis", "supported", "challenged", "retired"]
    learn.run(c, reset_state=True)
    assert snapshot() == before
    assert len(learn.stored(c, "belief")) == n_records          # replay writes nothing new
    assert transitions() == hb and [len(v["history"]) for v in learn.history(c, old["source_id"])] == hist_before


def test_r1_f5_reset_replays_gap_lifecycle_from_answers(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nadd start_period\n\n## Confidence\n90\n")
    for p in ("research", "gap"):
        config_mod.set_grant(p, True)
    c = cfg()
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    assert research.run(c, 1)["results"][0]["status"] == "answered"
    learn.run(c, reset_state=True)
    g = research.ranked(c)[0]
    assert g["status"] == "answered" and g["attempts"] == 0


def test_r1_f6_ask_mode_revision_is_drafted_once_and_inherits_when_approved(cfg, user_config, fake_model):
    user_config('[distiller]\ncommand = "argv"\n'
                f"argv = {toml_list(fake_model(draft(statement='narrower', applies='same task', not_when='other task')))}\n"
                '[approval]\nlearn = "ask"\n')
    c = cfg()
    # Seed an approved hypothesis with support.
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    for e in writes.pending(c):
        writes.approve(c, e["id"], by="t")
    learn.run(c, reset_state=True)
    old = one(c, L1)
    assert old["for"] == 3
    put(c, "session:4", metadata={"beliefs_listed": [old["bid"]]}, Pitfalls=COUNTER)
    for _ in range(3):
        learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "belief"]
    assert len(pend) == 1
    writes.approve(c, pend[0]["id"], by="t")
    learn.run(c)
    new = one(c, "narrower")
    assert new["for"] == 3 and new["verifying"] and one(c, L1)["status"] == "retired"


def test_r1_f7_checks_for_beliefs_never_shown_are_ignored(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    sid = one(c, L1)["source_id"]
    for n in (2, 3, 4):
        put(c, f"session:{n}", metadata={"belief_checks": {sid: "followed"}}, Unfinished="- x")
    learn.run(c)
    assert one(c, L1)["for"] == 1


def test_r1_o1_pinned_beliefs_are_never_stale(cfg, no_model):
    c = cfg()
    bid = _person_belief(c)
    put(c, "session:1", Changes="- session start now recalls the newest journal in full differently")
    learn.run(c)
    assert one(c, L1)["status"] == "supported"
    assert learn.mark_stale(c, f"belief:{bid}") is False


def test_r1_o4_research_on_a_not_holding_gap_is_not_evidence(cfg, user_config, fake_model):
    c = cfg()
    bid = _person_belief(c)
    _researcher(user_config, fake_model, "## Answer\nit was skipped under time pressure\n\n## Confidence\n90\n")
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "why was it skipped", signal="not-holding", evidence=["session:1"], belief=f"belief:{bid}")
    learn.run(c)
    research.run(c, 1)
    assert one(c, L1)["for"] == 0


def test_r1_o5_state_rejects_non_numeric_confidence(cfg):
    s = learn.empty_state()
    s["beliefs"]["belief:B-1"] = {"status": "supported", "confidence": "high", "evidence": [], "history": []}
    with pytest.raises(learn.StateError):
        learn.validate_state(s)


# ---- v0.2.0 round 2 findings ----------------------------------------------

def test_r2_f1_a_late_approved_journal_supports_instead_of_duplicating(cfg, no_model):
    c = cfg()
    writes.write(c, "distill", "journal", jtext(Lessons_learned=f"- {L1}"), source_id="session:1")
    put(c, "session:2", Lessons_learned=f"- {L1b}")
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["source_id"] == "session:1"]
    writes.approve(c, pend[0]["id"], by="t")
    learn.run(c)
    rows = learn.report(c)
    assert len(rows) == 1 and rows[0]["for"] == 2 and rows[0]["status"] == "supported"
    before = learn.load_state(c)
    learn.run(c, reset_state=True)
    assert learn.load_state(c) == before


def test_r2_f2_a_max_new_cut_does_not_change_what_a_revision_inherits(cfg, user_config, fake_model):
    reply = draft(statement="narrower", applies="same task", not_when="other task")
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(reply))}\n[learn]\nmax_new = 1\n')
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    put(c, "session:4", metadata={"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    put(c, "session:5", Lessons_learned="- alpha bravo charlie matters")
    put(c, "session:6", Lessons_learned="- delta echo foxtrot matters")
    for _ in range(4):
        learn.run(c)
    before = learn.load_state(c)
    learn.run(c, reset_state=True)
    assert learn.load_state(c) == before and one(c, "narrower")["for"] == 3


def test_r2_f3_ask_mode_lesson_keeps_its_first_evidence_and_is_proposed_once(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[approval]\nlearn = "ask"\n')
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    put(c, "session:2", Lessons_learned=f"- {L1b}")
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "belief"]
    assert len(pend) == 1
    writes.approve(c, pend[0]["id"], by="t")
    learn.run(c)
    assert one(c, L1)["for"] == 2 and one(c, L1)["status"] == "supported"


def test_r2_f4_a_persons_stale_survives_reset_after_an_older_change(cfg, no_model):
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    put(c, "session:2", Changes="- session start now recalls the newest journal in full differently")
    put(c, "session:3", Lessons_learned=f"- {L1b}")
    learn.run(c)
    assert one(c, L1)["status"] == "supported"
    learn.mark_stale(c, one(c, L1)["source_id"])
    assert one(c, L1)["status"] == "stale"
    learn.run(c, reset_state=True)
    assert one(c, L1)["status"] == "stale"


def test_r2_f5_an_answer_approved_later_counts_and_brings_its_belief(cfg, user_config, fake_model):
    c = cfg()
    bid = _person_belief(c)
    for i, text in enumerate([L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    before = one(c, L1)["for"]
    reply = ("## Answer\nholds\n\n## Confidence\n90\n\n## Belief\n"
             + draft(statement="pin the start period on compose services", applies="compose services"))
    _researcher(user_config, fake_model, reply)
    c = cfg()
    _gap(c, "does it hold", signal="contradiction", evidence=["session:1"], belief=f"belief:{bid}")
    research.run(c, 1)                                   # answer pending: research is ask
    assert one(c, L1)["for"] == before
    ans = [e for e in writes.pending(c) if e["record"]["kind"] == "answer"][0]
    writes.approve(c, ans["id"], by="t")
    learn.run(c)
    assert one(c, L1)["for"] == before + 1
    assert one(c, "pin the start period on compose services")["origin"] == "research"


def test_r2_f6_an_answer_never_supports_the_revision_it_wrote(cfg, user_config, fake_model):
    c = cfg()
    for i, text in enumerate([L1, L1b], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    old = one(c, L1)
    reply = ("## Answer\nno\n\n## Confidence\n90\n\n## Belief\n"
             + draft(statement="recall by the task", applies="session start", revises=old["bid"]))
    _researcher(user_config, fake_model, reply)
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "does it hold", signal="contradiction", evidence=["session:2"], belief=old["source_id"])
    research.run(c, 1)
    rows = report(c)
    assert rows[L1]["against"] == 1 and rows[L1]["status"] == "retired"
    assert rows["recall by the task"]["for"] == 2                # inherited, not the answer itself


def test_r2_f7_a_stale_revision_is_not_injected_at_start(cfg, reviser):
    reviser(statement="recall only the unfinished part at session start",
            applies="resuming the same task", not_when="a different task")
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    put(c, "session:4", metadata={"beliefs_listed": [one(c, L1)["bid"]]}, Pitfalls=COUNTER)
    learn.run(c)
    new = one(c, "recall only the unfinished part at session start")
    assert new["bid"] in recall.assemble(c)
    learn.mark_stale(c, new["source_id"])
    assert new["bid"] not in recall.assemble(c)


def test_r2_f8_a_revision_in_hypothesis_stays_injected_after_its_first_test(cfg, reviser):
    reviser(statement="recall only the unfinished part at session start",
            applies="resuming the same task", not_when="a different task")
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    for i in (2, 3):
        put(c, f"session:{i}", metadata={"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    learn.run(c)
    new = one(c, "recall only the unfinished part at session start")
    assert new["status"] == "hypothesis"
    put(c, "session:4", metadata={"beliefs_listed": [new["bid"]],
                                  "belief_checks": {new["source_id"]: "followed"}}, Unfinished="- x")
    learn.run(c)
    assert new["bid"] in recall.assemble(c)


def test_r2_f9_reset_writes_nothing_and_a_rejected_gap_is_not_asked_again(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n')
    c = cfg()
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    gap = [e for e in writes.pending(c) if e["record"]["kind"] == "gap"][0]
    writes.reject(c, gap["id"])
    learn.run(c, reset_state=True)
    learn.run(c)
    assert writes.pending(c) == []


def test_r2_f9_revision_of_a_revision_history_is_in_order(cfg, reviser):
    reviser(statement="recall only the unfinished part at session start",
            applies="resuming the same task", not_when="a different task")
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    put(c, "session:2", metadata={"beliefs_listed": [one(c, L1)["bid"]]}, Pitfalls=COUNTER)
    put(c, "session:3", metadata={"beliefs_listed": [one(c, L1)["bid"]]}, Pitfalls=COUNTER)
    learn.run(c)
    r1 = one(c, "recall only the unfinished part at session start")
    chain = learn.history(c, r1["source_id"])
    first = [h["status"] for h in chain[1]["history"]]
    assert first[0] == "hypothesis" and "retired" not in first[:-1]


def test_r2_o2_a_held_revision_is_not_redrafted_every_run(cfg, user_config, fake_model):
    calls = cfg.__class__  # placeholder to keep fixtures in signature order
    reply = draft(statement="ghp_" + "a" * 36 + " is the fix", applies="x", not_when="y")
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(reply))}\n')
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    for i in (2, 3):
        put(c, f"session:{i}", metadata={"beliefs_listed": [one(c, L1)["bid"]]}, Pitfalls=COUNTER)
    for _ in range(3):
        learn.run(c)
    assert len([e for e in writes.held(c) if e["record"]["kind"] == "belief"]) == 1
    del calls


def test_r2_o3_rulings_in_migrated_journals_are_not_the_persons(cfg, no_model, tmp_path):
    from nimblemind import migrate
    src = tmp_path / "old"
    src.mkdir()
    (src / "j1.md").write_text(jtext(Rulings="- ship on fridays"), encoding="utf-8")
    migrate.run(cfg(), src, "journal")
    c = cfg()
    learn.run(c)
    b = learn.report(c)[0]
    assert b["origin"] == "lessons" and not b["pinned"]


def test_r2_o6_a_session_shown_the_answer_does_not_restate_its_belief(cfg, user_config, fake_model):
    reply = ("## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
             + draft(statement="pin compose healthchecks with a start period", applies="compose services"))
    _researcher(user_config, fake_model, reply)
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "why do compose services fail on the first start")
    research.run(c, 1)
    ans = learn.stored(c, "answer")[0].source_id
    put(c, "session:9", metadata={"records_shown": [ans]},
        Lessons_learned="- pin compose healthchecks with a start period for compose services")
    learn.run(c)
    assert one(c, "pin compose healthchecks with a start period")["for"] == 0


def test_r2_an_answers_belief_is_written_once(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
                + draft(statement="pin compose healthchecks with a start period", applies="compose services"))
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "why do compose services fail on the first start")
    research.run(c, 1)
    for _ in range(3):
        learn.run(c)
    assert len(learn.report(c)) == 1



# ---- v0.2.0 round 3 findings ----------------------------------------------

def test_r3_f1_restating_a_retired_belief_does_not_resurrect_it(cfg, reviser):
    reviser(statement="recall only the unfinished part at session start",
            applies="resuming the same task", not_when="a different task")
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    bid = one(c, L1)["bid"]
    for i in (4, 5):
        put(c, f"session:{i}", metadata={"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    learn.run(c)
    put(c, "session:6", Lessons_learned=f"- {L1d}")
    learn.run(c)
    rows = learn.report(c)
    assert len(rows) == 2 and one(c, L1)["status"] == "retired"
    assert bid not in recall.assemble(c)


def test_r3_f1_a_person_retired_belief_stays_retired(cfg, no_model):
    c = cfg()
    for i, text in enumerate([L1, L1b], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    learn.retire(c, one(c, L1)["source_id"])
    put(c, "session:3", Lessons_learned=f"- {L1c}")
    learn.run(c)
    assert len(learn.report(c)) == 1 and one(c, L1)["status"] == "retired"


def test_r3_f2_signals_past_the_per_run_cap_are_asked_later(cfg, no_model):
    c = cfg()
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf"]
    for i, w_ in enumerate(words):
        put(c, f"session:{i}", Surprises=f"- the {w_} {w_}x {w_}y result came out unexpectedly")
    learn.run(c)
    assert len([e for e in writes.pending(c) if e["record"]["kind"] == "gap"]) == 5
    learn.run(c)
    assert len([e for e in writes.pending(c) if e["record"]["kind"] == "gap"]) == 7
    learn.run(c)
    assert len([e for e in writes.pending(c) if e["record"]["kind"] == "gap"]) == 7


def test_r3_f3_a_late_approved_journal_is_an_occurrence(cfg, no_model):
    config_mod.set_grant("gap", True)
    c = cfg()
    writes.write(c, "distill", "journal", jtext(Pitfalls=PIT3), source_id="session:0")
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    e = [x for x in writes.pending(c) if x["record"]["source_id"] == "session:0"][0]
    writes.approve(c, e["id"], by="t")
    learn.run(c)
    assert research.ranked(c)[0]["occurrences"] == 3


def test_r3_f4_the_session_that_raised_the_question_never_supports_the_answer(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
                + draft(statement="pin compose healthchecks with a start period", applies="compose services"))
    for p in ("research", "gap"):
        config_mod.set_grant(p, True)
    c = cfg()
    put(c, "session:1", metadata={"session_id": "S"}, Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    research.run(c, 1)
    put(c, "session:1b", metadata={"session_id": "S"},
        Lessons_learned="- pin compose healthchecks with a start period for compose services")
    learn.run(c)
    assert one(c, "pin compose healthchecks with a start period")["for"] == 0


def test_r3_f5_a_lost_done_mark_does_not_write_an_answers_belief_twice(cfg, user_config, fake_model, monkeypatch):
    _researcher(user_config, fake_model, "## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
                + draft(statement="pin compose healthchecks with a start period", applies="compose services"))
    config_mod.set_grant("research", True)
    c = cfg()
    _gap(c, "why do compose services fail on the first start")
    real = learn._mark_done
    monkeypatch.setattr(learn, "_mark_done", lambda cfg_, k: None if k.startswith("answer:") else real(cfg_, k))
    research.run(c, 1)
    monkeypatch.setattr(learn, "_mark_done", real)
    learn.run(c)
    assert len(learn.report(c)) == 1


def test_r3_o3_a_gap_with_a_pending_answer_is_not_researched_again(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nx\n\n## Confidence\n90\n")
    c = cfg()
    _gap(c, "why do compose services fail on the first start")
    assert len(research.run(c, 1)["results"]) == 1
    assert research.run(c, 1)["results"] == []
    assert research.ranked(c)[0]["status"] == "open"        # not answered until approved


def test_r3_o6_the_provider_door_stores_notes_only():
    with pytest.raises(ValueError):
        api.store("## Belief checks\n- B-1a2b3c4d: followed", kind="journal")


# ---- v0.2.0 round 4 findings ----------------------------------------------

REV = "recall only the unfinished part at session start"


def test_r4_f1_a_late_journal_that_raised_the_question_never_supports_the_answer(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nx\n\n## Confidence\n90\n\n## Belief\n"
                + draft(statement="pin compose healthchecks with a start period", applies="compose services"))
    for p in ("research", "gap"):
        config_mod.set_grant(p, True)
    c = cfg()
    writes.write(c, "distill", "journal", jtext(
        Pitfalls=PIT3, Lessons_learned="- pin compose healthchecks with a start period for compose services"),
        source_id="session:0", metadata={"session_id": "S0"})
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    research.run(c, 1)
    e = [x for x in writes.pending(c) if x["record"]["source_id"] == "session:0"][0]
    writes.approve(c, e["id"], by="t")
    learn.run(c)
    assert one(c, "pin compose healthchecks with a start period")["for"] == 0


def test_r4_f2_a_late_counter_session_never_supports_the_revision(cfg, reviser):
    reviser(statement=REV, applies="resuming the same task as the last session", not_when="a different task")
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", metadata={"session_id": f"S{i}"}, Lessons_learned=f"- {text}")
    learn.run(c)
    old = one(c, L1)
    writes.write(c, "distill", "journal", jtext(
        Pitfalls=COUNTER, Lessons_learned=f"- {REV} when resuming the same task as the last session"),
        source_id="session:4", metadata={"session_id": "S4", "beliefs_listed": [old["bid"]]})
    put(c, "session:5", metadata={"session_id": "S5", "beliefs_listed": [old["bid"]]}, Pitfalls=COUNTER)
    learn.run(c)
    new = one(c, REV)
    e = [x for x in writes.pending(c) if x["record"]["source_id"] == "session:4"][0]
    writes.approve(c, e["id"], by="t")
    learn.run(c)
    ev = learn.load_state(c)["beliefs"][new["source_id"]]["evidence"]
    assert not any(x["session"] == "s:S4" for x in ev)


def test_r4_f3_a_surprise_and_a_pitfall_are_separate_questions(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nadd start_period\n\n## Confidence\n90\n")
    for p in ("research", "gap"):
        config_mod.set_grant(p, True)
    c = cfg()
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    research.run(c, 1)
    put(c, "session:3", Surprises="- qdrant healthcheck has no start_period so compose up aborts first time")
    learn.run(c)
    assert sorted(g["signal"] for g in research.ranked(c)) == ["recurring", "surprise"]


def test_r4_f4_a_late_old_journal_does_not_duplicate_a_verified_gap(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nadd start_period\n\n## Confidence\n90\n",
                extra="[curiosity]\nverify_window = 2\n")
    for p in ("research", "gap"):
        config_mod.set_grant(p, True)
    c = cfg()
    writes.write(c, "distill", "journal", jtext(Pitfalls=PIT3), source_id="session:0")
    put(c, "session:1", Pitfalls=PIT)
    put(c, "session:2", Pitfalls=PIT2)
    learn.run(c)
    research.run(c, 1)
    put(c, "session:3", Unfinished="- other")
    put(c, "session:4", Unfinished="- other again")
    learn.run(c)
    assert research.ranked(c)[0]["status"] == "verified"
    e = [x for x in writes.pending(c) if x["record"]["source_id"] == "session:0"][0]
    writes.approve(c, e["id"], by="t")
    learn.run(c)
    gaps = research.ranked(c)
    assert len(gaps) == 1 and gaps[0]["status"] == "verified" and gaps[0]["occurrences"] == 3


# ---- v0.2.0 round 6 findings ----------------------------------------------

def test_r6_f1_a_provider_deletes_only_its_own_notes(cfg):
    c = cfg()
    bid = _person_belief(c)
    with pytest.raises(ValueError):
        api.delete(f"belief:{bid}")
    assert one(c, L1)["pinned"]
    config_mod.set_grant("provider", True)
    saved = api.store("a provider note about qdrant")
    assert api.delete(saved["source_id"]) is True
    writes.write(c, "cli", "note", "the person's note", source_id="note:p", approved_by="t")
    with pytest.raises(ValueError):
        api.delete("note:p")


def test_r6_f2_a_provider_cannot_overwrite_another_record(cfg):
    config_mod.set_grant("provider", True)
    c = cfg()
    bid = _person_belief(c)
    put(c, "session:1", Unfinished="- x")
    for sid in (f"belief:{bid}", "session:1", "journal:j1"):
        with pytest.raises(ValueError):
            api.store("x", source_id=sid)
    writes.write(c, "cli", "note", "the person's note", source_id="note:p", approved_by="t")
    with pytest.raises(ValueError):
        api.store("x", source_id="note:p")
    first = api.store("v1", source_id="note:mine")
    assert api.store("v2", source_id=first["source_id"])["state"] == "stored"


def test_r6_f3_malformed_check_verdicts_are_dropped():
    text = jtext() + ("## Belief checks\n- B-1a2b3c4d: followed-ish: x\n- B-1a2b3c4e: violated?!\n"
                      "- B-1a2b3c4f: n/a.b\n- B-1a2b3c40: followed/violated: unsure\n- B-1a2b3c41: violated\n")
    listed = {"B-1a2b3c4d", "B-1a2b3c4e", "B-1a2b3c4f", "B-1a2b3c40", "B-1a2b3c41"}
    assert learn.belief_checks(text, listed) == {"belief:B-1a2b3c41": "violated"}


def test_r6_f4_every_hook_run_prunes_old_session_files(cfg, tmp_path, monkeypatch):
    import os
    cfg()
    old = recall._session_file("ancient")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('{"ids": []}', encoding="utf-8")
    past = time.time() - 8 * 86400
    os.utime(old, (past, past))
    assert _hook(monkeypatch, "prompt", {"session_id": "s", "cwd": str(tmp_path), "prompt": "hi"}) == ""
    assert not old.exists()


def test_r6_a_pinned_beliefs_history_starts_supported(cfg, no_model):
    c = cfg()
    bid = _person_belief(c)
    learn.run(c)
    h = learn.history(c, f"belief:{bid}")[0]["history"]
    assert [x["status"] for x in h] == ["supported"]


# ---- v0.2.0 round 7 findings ----------------------------------------------

def test_r7_f1_session_end_keeps_what_an_idle_session_was_shown(cfg, tmp_path, monkeypatch):
    import os
    c = cfg()
    put(c, "session:1", Lessons_learned=f"- {L1}")
    learn.run(c)
    sid = one(c, L1)["source_id"]
    recall.remember("S", [sid])
    f = recall._session_file("S")
    past = time.time() - 8 * 86400
    os.utime(f, (past, past))
    seen = []
    monkeypatch.setattr(distill, "spawn", lambda *a, **k: seen.append(recall.injected("S")))
    t = tmp_path / "t.jsonl"
    t.write_text("{}", encoding="utf-8")
    _hook(monkeypatch, "session-end", {"session_id": "S", "cwd": str(tmp_path), "transcript_path": str(t)})
    assert seen == [{sid}]


def test_r7_f2_a_ruling_like_a_retired_belief_of_the_persons_is_a_new_belief(cfg, no_model):
    c = cfg()
    bid = _person_belief(c)
    learn.retire(c, f"belief:{bid}")
    _person_journal(c, "session:1", Rulings="- never recall the newest journal in full at session start to resume work")
    assert learn.run(c)["beliefs_created"] == 1
    new = one(c, "never recall the newest journal in full at session start to resume work")
    assert new["origin"] == "ruling" and new["pinned"]


def test_r7_f3_a_stale_revision_is_marked_stale_at_the_prompt_hook(cfg, reviser):
    reviser(statement=REV, applies="resuming the same task", not_when="a different task")
    c = cfg()
    for i, text in enumerate([L1, L1b, L1c], 1):
        put(c, f"session:{i}", Lessons_learned=f"- {text}")
    learn.run(c)
    put(c, "session:4", metadata={"beliefs_listed": [one(c, L1)["bid"]]}, Pitfalls=COUNTER)
    learn.run(c)
    learn.mark_stale(c, one(c, REV)["source_id"])
    block, _ = recall.prompt_block(c, "recall only the unfinished part at session start when resuming", "s")
    assert "[stale" in block and "revised: verifying" not in block


def test_r7_o1_hermes_forget_refuses_records_that_are_not_its_own(cfg):
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "integrations" / "hermes"))
    try:
        from provider import NimblemindMemoryProvider
    finally:
        sys.path.pop(0)
    c = cfg()
    bid = _person_belief(c)
    assert NimblemindMemoryProvider().forget(f"belief:{bid}") is False
    assert one(c, L1)["pinned"]


def test_r7_o4_one_journal_never_exceeds_max_new(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[learn]\nmax_new = 2\n')
    c = cfg()
    put(c, "session:1", Lessons_learned="- alpha bravo charlie matters\n- delta echo foxtrot matters\n"
                                        "- golf hotel india matters")
    assert learn.run(c)["beliefs_created"] == 2
    assert learn.run(c)["beliefs_created"] == 1
    assert len(learn.report(c)) == 3


# ---- v0.2.0 round 8 findings ----------------------------------------------

def test_r8_f1_a_split_journal_never_reproposes_and_a_refused_item_does_not_stall(cfg, user_config, tmp_path):
    user_config('[distiller]\ncommand = "none"\n[learn]\nmax_new = 1\n')
    c = cfg()
    put(c, "session:1", Lessons_learned="- always pin the zebracorp mirror before running the deploy\n"
                                        "- keep the release notes in the changelog directory\n"
                                        "- run the linter before pushing any branch")
    user_config('[distiller]\ncommand = "none"\n[learn]\nmax_new = 1\n'
                '[isolation]\nchecks = ["builtin-secrets", "builtin-patterns"]\ndeny = ["zebracorp"]\n')
    c = cfg()
    for _ in range(4):
        learn.run(c)
    assert len(learn.report(c)) == 2 and len(writes.held(c)) == 1


def test_r8_f1_a_rejected_lesson_is_not_asked_again(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[approval]\nlearn = "ask"\n[learn]\nmax_new = 1\n')
    c = cfg()
    put(c, "session:1", Lessons_learned="- alpha bravo charlie matters a lot\n- delta echo foxtrot matters a lot")
    learn.run(c)
    first = [e for e in writes.pending(c) if e["record"]["kind"] == "belief"]
    assert len(first) == 1 and "alpha" in first[0]["record"]["content"]
    writes.reject(c, first[0]["id"])
    learn.run(c)
    learn.run(c)
    texts = [e["record"]["content"] for e in writes.pending(c) if e["record"]["kind"] == "belief"]
    assert len(texts) == 1 and "delta" in texts[0]


@pytest.mark.parametrize("max_new", [1, 25])
def test_r8_f2_split_or_not_a_journal_derives_the_same_beliefs(cfg, user_config, max_new):
    user_config(f'[distiller]\ncommand = "none"\n[learn]\nmax_new = {max_new}\n'
                'lesson_sections = ["Rulings", "Lessons learned"]\n')
    c = cfg()
    _person_journal(c, "session:1", Rulings="- never retry flaky network calls in the deploy script",
                    Lessons_learned="- never retry the flaky network calls in the deploy script")
    for _ in range(4):
        learn.run(c)
    assert [r["origin"] for r in learn.report(c)] == ["ruling"]


def test_r8_o1_a_standing_persons_belief_covers_their_ruling(cfg, no_model):
    c = cfg()
    _person_belief(c)
    _person_journal(c, "session:1", Rulings=f"- {L1b}")
    assert learn.run(c)["beliefs_created"] == 0


# ---- v0.2.0 round 9 findings ----------------------------------------------

def test_r9_f1_reordering_sections_mid_split_drops_nothing(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[learn]\nmax_new = 1\n')
    c = cfg()
    _person_journal(c, "session:1", Lessons_learned="- alpha bravo charlie matters a lot\n- delta echo foxtrot matters a lot",
                    Rulings="- never deploy on fridays without review sign off")
    learn.run(c)
    user_config('[distiller]\ncommand = "none"\n[learn]\nmax_new = 1\n'
                'lesson_sections = ["Rulings", "Lessons learned"]\n')
    c = cfg()
    for _ in range(4):
        learn.run(c)
    assert sorted(r["origin"] for r in learn.report(c)) == ["lessons", "lessons", "ruling"]


def test_r9_o2_a_repeated_refused_line_is_held_once(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n')
    c = cfg()
    put(c, "session:1", Lessons_learned="- always pin the zebracorp mirror\n- always pin the zebracorp mirror")
    user_config('[distiller]\ncommand = "none"\n[isolation]\nchecks = ["builtin-secrets", "builtin-patterns"]\n'
                'deny = ["zebracorp"]\n')
    learn.run(cfg())
    assert len(writes.held(cfg())) == 1


def test_r9_o3_a_held_lesson_covers_nothing(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n')
    c = cfg()
    put(c, "session:1", Lessons_learned="- always pin the zebracorp mirror before running the deploy")
    put(c, "session:2", Lessons_learned="- always pin the package mirror before running the deploy")
    user_config('[distiller]\ncommand = "none"\n[isolation]\nchecks = ["builtin-secrets", "builtin-patterns"]\n'
                'deny = ["zebracorp"]\n')
    c = cfg()
    learn.run(c)
    assert [r["statement"] for r in learn.report(c)] == ["always pin the package mirror before running the deploy"]


def test_r10_o1_the_same_line_as_lesson_and_ruling_is_two_items(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[learn]\nmax_new = 1\n')
    c = cfg()
    _person_journal(c, "session:1", Lessons_learned="- never deploy on fridays without review sign off",
                    Rulings="- never deploy on fridays without review sign off")
    for _ in range(3):
        learn.run(c)
    assert sorted((r["origin"], r["pinned"]) for r in learn.report(c)) == [("lessons", False), ("ruling", True)]
