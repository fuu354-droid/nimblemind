"""The learning loop, DESIGN.md section 15, and acceptance 8 to 13."""

import io
import json
import sys
import time

import pytest

from conftest import toml_list
from nimblemind import api, cli, config as config_mod, distill, learn, recall, research, writes

SECTIONS = ["Decisions", "Rulings", "Pitfalls", "Surprises", "Unfinished", "Lessons learned",
            "Improvements found"]


def jtext(**parts):
    """A journal with the named sections filled (keys use _ for spaces)."""
    filled = {k.replace("_", " "): v for k, v in parts.items()}
    return "\n".join(f"## {s}\n" + (filled.get(s) or "- none") for s in SECTIONS) + "\n"


def draft(**fields):
    """A drafter reply: one `Label: value` line per field."""
    names = {"statement": "Statement", "applies": "Applies when", "not_when": "Not when",
             "revises": "Revises"}
    return "".join(f"{names[k]}: {v}\n" for k, v in fields.items())


def put_journal(cfg, sid, metadata=None, **parts):
    time.sleep(0.002)  # distinct creation stamps, in order
    r = writes.write(cfg, "cli", "journal", jtext(**parts), source_id=sid,
                     metadata=metadata or {}, approved_by="t")
    assert r.state == "stored"
    return r


def gaps(cfg):
    return [r for r in learn.stored(cfg, "gap")]


def approve_all(cfg, kind):
    for e in writes.pending(cfg):
        if e["record"]["kind"] == kind:
            assert writes.approve(cfg, e["id"], by="t").state == "stored"


@pytest.fixture
def no_model(user_config):
    user_config('[distiller]\ncommand = "none"\n')


PITFALL = "- qdrant healthcheck has no start_period so the first compose up aborts"
PITFALL2 = "- the qdrant healthcheck lacks start_period and compose up aborts again"
PITFALL3 = "- compose up aborted: qdrant healthcheck start_period missing"


# ---- acceptance 8: signals ------------------------------------------------

def test_a8_recurring_pitfall_makes_one_gap_and_merges_later(cfg, no_model):
    c = cfg()
    put_journal(c, "session:1", Pitfalls=PITFALL)
    learn.run(c)
    assert not writes.pending(c)                 # one occurrence is not a signal
    put_journal(c, "session:2", Pitfalls=PITFALL2)
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "gap"]
    assert len(pend) == 1 and pend[0]["record"]["metadata"]["signal"] == "recurring"
    approve_all(c, "gap")
    before = research.ranked(c)[0]["priority"]
    put_journal(c, "session:3", Pitfalls=PITFALL3)
    learn.run(c)
    assert [e for e in writes.pending(c) if e["record"]["kind"] == "gap"] == []
    assert len(gaps(c)) == 1
    after = research.ranked(c)[0]
    assert after["occurrences"] == 3 and after["priority"] > before


def test_a8_pending_and_held_journals_produce_nothing(cfg, no_model, user_config):
    c = cfg()
    for i in (1, 2):
        writes.write(c, "distill", "journal", jtext(Pitfalls=PITFALL), source_id=f"session:{i}")
    assert len(writes.pending(c)) == 2
    assert learn.run(c)["signals"] == 0
    assert [e for e in writes.pending(c) if e["record"]["kind"] == "gap"] == []


def test_a8_learn_twice_adds_nothing(cfg, no_model):
    c = cfg()
    put_journal(c, "session:1", Surprises="- RTK was added and the usage limit still hit early")
    learn.run(c)
    n = len(writes.pending(c))
    assert n == 1
    learn.run(c)
    assert len(writes.pending(c)) == n


def test_surprise_question_comes_from_a_template(cfg, no_model):
    c = cfg()
    put_journal(c, "session:1", Surprises="- RTK was added and the usage limit still hit early")
    learn.run(c)
    rec = writes.pending(c)[0]["record"]
    q = learn.parse_gap(rec["content"])["question"]
    assert q == learn.TEMPLATES["surprise"] + "RTK was added and the usage limit still hit early"


# ---- acceptance 9: ranking and stopping -----------------------------------

def _manual_gap(c, question, **meta):
    taken = set()
    gid = learn.new_id("G", taken)
    m = {"signal": "manual", "evidence": [], **meta}
    return writes.write(c, "cli", "gap", learn.gap_text(gid, question, "manual"),
                        source_id=f"gap:{gid}", metadata=m, approved_by="t")


def test_a9_not_holding_outranks_an_uncertain_unimportant_manual_gap(cfg, no_model):
    c = cfg()
    _manual_gap(c, "what is the exact colour name of the logo", confidence=5)
    gid = "G-0000aaaa"
    writes.write(c, "cli", "gap", learn.gap_text(gid, "principle P-1 not applied", "not-holding"),
                 source_id=f"gap:{gid}", approved_by="t",
                 metadata={"signal": "not-holding", "evidence": ["session:x"], "principle": "principle:P-1"})
    order = [g["signal"] for g in research.ranked(c)]
    assert order == ["not-holding", "manual"]


def _researcher(user_config, fake_model, reply, extra=""):
    user_config('[distiller]\ncommand = "none"\n[researcher]\ncommand = "argv"\n'
                f"argv = {toml_list(fake_model(reply))}\n" + extra)


def test_a9_parks_after_max_attempts_and_reopens_on_recurrence(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nnot sure\n\n## Confidence\n20\n\n## Principle\nStatement: none\n")
    config_mod.set_grant("research", True)
    c = cfg()
    put_journal(c, "session:1", Pitfalls=PITFALL)
    put_journal(c, "session:2", Pitfalls=PITFALL2)
    config_mod.set_grant("gap", True)
    c = cfg()
    learn.run(c)
    assert research.run(c, 1)["results"][0]["status"] == "open"
    assert research.run(c, 1)["results"][0]["status"] == "parked"
    assert research.run(c, 1)["results"] == []             # parked: not tried again
    put_journal(c, "session:3", Pitfalls=PITFALL3)
    learn.run(c)
    assert research.ranked(c)[0]["status"] == "open"        # new evidence reopens it


def test_a9_answered_gap_verifies_after_window_and_recurs_on_signal(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nadd start_period\n\n## Confidence\n90\n",
                extra="[curiosity]\nverify_window = 2\n")
    for p in ("research", "gap"):
        config_mod.set_grant(p, True)
    c = cfg()
    put_journal(c, "session:1", Pitfalls=PITFALL)
    put_journal(c, "session:2", Pitfalls=PITFALL2)
    learn.run(c)
    assert research.run(c, 1)["results"][0]["status"] == "answered"
    put_journal(c, "session:3", Pitfalls=PITFALL3)        # recurs after the answer
    learn.run(c)
    assert research.ranked(c)[0]["status"] == "recurred"
    assert research.run(c, 1)["results"][0]["status"] == "answered"
    put_journal(c, "session:4", Unfinished="- other work")
    put_journal(c, "session:5", Unfinished="- other work again")
    learn.run(c)
    assert research.ranked(c)[0]["status"] == "verified"


def test_research_answer_is_marked_untrusted_and_pending_by_default(cfg, user_config, fake_model):
    _researcher(user_config, fake_model, "## Answer\nbecause\n\n## Confidence\n80\n")
    c = cfg()
    _manual_gap(c, "why is the sky blue at noon")
    out = research.run(c, 1)
    assert out["results"][0]["answer"].startswith("pending")
    ans = [e for e in writes.pending(c) if e["record"]["kind"] == "answer"][0]
    assert ans["record"]["content"].startswith("Untrusted evidence")


def test_research_refuses_with_no_researcher(cfg, user_config):
    user_config('[researcher]\ncommand = "none"\n')
    from nimblemind import model
    with pytest.raises(model.ModelError):
        research.run(cfg(), 1)


# ---- acceptance 10: principles --------------------------------------------

LESSON = "- check the board for a doing task before starting any engineering commit"
LESSON2 = "- before any engineering commit check the board has a doing task"


def test_a10_recurring_lesson_becomes_a_pending_principle_with_evidence(cfg, no_model):
    c = cfg()
    put_journal(c, "session:1", Lessons_learned=LESSON)
    put_journal(c, "session:2", Lessons_learned=LESSON2)
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "principle"]
    assert len(pend) == 1
    rec = pend[0]["record"]
    assert rec["metadata"]["evidence"] == ["session:1", "session:2"]
    assert learn.parse_principle(rec["content"])["statement"]
    # Not injected before approval: no hook shows a pending principle.
    assert "Principle" not in recall.assemble(c) and learn.active_principles(c) == []
    block, _ = recall.prompt_block(c, "starting an engineering commit, check the board first")
    assert "P-" not in block
    approve_all(c, "principle")
    assert len(learn.active_principles(c)) == 1
    assert "Principles:" in recall.assemble(c)


def test_a10_one_lesson_is_not_enough_but_one_ruling_is(cfg, no_model):
    c = cfg()
    put_journal(c, "session:1", Lessons_learned=LESSON)
    learn.run(c)
    assert writes.pending(c) == []
    put_journal(c, "session:2", Rulings="- a necessary fix stays inside the task under review")
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "principle"]
    assert len(pend) == 1 and pend[0]["record"]["metadata"]["origin"] == "rulings"


def test_a10_a_covered_lesson_is_not_proposed_again(cfg, no_model):
    c = cfg()
    put_journal(c, "session:1", Lessons_learned=LESSON)
    put_journal(c, "session:2", Lessons_learned=LESSON2)
    learn.run(c)
    approve_all(c, "principle")
    put_journal(c, "session:3", Lessons_learned="- check the board has a doing task before an engineering commit")
    learn.run(c)
    assert [e for e in writes.pending(c) if e["record"]["kind"] == "principle"] == []


def test_a10_approved_revision_retires_what_it_supersedes(cfg, user_config, fake_model):
    c = cfg()
    old = writes.write(c, "cli", "principle",
                       learn.principle_text("P-00000001", "check the board before an engineering commit",
                                            "starting an engineering commit", "none"),
                       source_id="principle:P-00000001", approved_by="t",
                       metadata={"evidence": [], "origin": "manual", "supersedes": ""})
    assert old.state == "stored"
    reply = draft(statement="claim a doing task on the board before any engineering commit",
                  applies="an engineering commit is about to land", not_when="board-only commits",
                  revises="P-00000001")
    user_config('[distiller]\ncommand = "argv"\n'
                f"argv = {toml_list(fake_model(reply))}\n")
    c = cfg()
    # Overlaps the old principle (it shares "board") without being covered
    # by it, so the drafter sees it and may name it as revised.
    put_journal(c, "session:1", Lessons_learned="- release tags need a changelog entry in the board notes")
    put_journal(c, "session:2", Lessons_learned="- release tags need a changelog entry first")
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "principle"]
    assert len(pend) == 1
    assert pend[0]["record"]["metadata"]["supersedes"] == "principle:P-00000001"
    assert [r.source_id for r, _ in learn.active_principles(c)] == ["principle:P-00000001"]
    approve_all(c, "principle")
    ids = [r.source_id for r, _ in learn.active_principles(c)]
    assert "principle:P-00000001" not in ids and len(ids) == 1


def test_supersedes_retires_through_any_route(cfg):
    c = cfg()
    for pid, sup in (("P-00000001", ""), ("P-00000002", "principle:P-00000001")):
        writes.write(c, "cli", "principle", learn.principle_text(pid, f"rule {pid}", "always", "none"),
                     source_id=f"principle:{pid}", approved_by="t",
                     metadata={"evidence": [], "origin": "manual", "supersedes": sup})
    assert [r.source_id for r, _ in learn.active_principles(c)] == ["principle:P-00000002"]


def test_draft_naming_an_unknown_revision_is_refused(cfg, user_config, fake_model):
    user_config('[distiller]\ncommand = "argv"\n'
                f"argv = {toml_list(fake_model(draft(statement='s', applies='a', not_when='none', revises='P-deadbeef')))}\n")
    c = cfg()
    put_journal(c, "session:1", Lessons_learned=LESSON)
    put_journal(c, "session:2", Lessons_learned=LESSON2)
    assert learn.run(c)["principles_proposed"] == 0
    assert writes.pending(c) == []


# ---- acceptance 11: application -------------------------------------------

def _active(c, pid="P-1a2b3c4d", statement="claim a doing task on the board before an engineering commit"):
    writes.write(c, "cli", "principle", learn.principle_text(pid, statement, "an engineering commit", "none"),
                 source_id=f"principle:{pid}", approved_by="t",
                 metadata={"evidence": ["session:0", "session:00"], "origin": "lessons", "supersedes": ""})


def test_a11_violation_after_activation_is_not_holding_and_reported(cfg, no_model):
    c = cfg()
    _active(c)
    time.sleep(1.1)  # approval times are per second
    put_journal(c, "session:1", metadata={"principle_checks": {"principle:P-1a2b3c4d": "violated"}},
                Unfinished="- x")
    learn.run(c)
    pend = [e for e in writes.pending(c) if e["record"]["kind"] == "gap"]
    assert len(pend) == 1 and pend[0]["record"]["metadata"]["signal"] == "not-holding"
    rep = learn.report(c)[0]
    assert rep["verdict"] == "not-holding" and rep["violated"] == 1 and rep["before"] == 2
    assert "not holding" in recall.assemble(c)


def test_a11_followed_checks_make_it_holding_after_the_window(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[curiosity]\nverify_window = 2\n')
    c = cfg()
    _active(c)
    time.sleep(1.1)
    for i in (1, 2):
        put_journal(c, f"session:{i}", metadata={"principle_checks": {"principle:P-1a2b3c4d": "followed"}},
                    Unfinished="- x")
    assert learn.report(c)[0]["verdict"] == "holding"


def test_principle_checks_are_parsed_only_for_listed_ids():
    text = jtext() + "## Principle checks\n- P-1a2b3c4d: violated: skipped the claim\n" \
        "- P-99999999: followed\n- P-1a2b3c4e: maybe\n"
    assert learn.principle_checks(text, {"P-1a2b3c4d", "P-1a2b3c4e"}) == \
        {"principle:P-1a2b3c4d": "violated"}


def test_distill_lists_principles_and_stores_checks(cfg, user_config, fake_model, tmp_path):
    reply = jtext() + "## Principle checks\n- P-1a2b3c4d: violated: committed before claiming\n"
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(reply))}\n')
    c = cfg()
    _active(c)
    lines = [json.dumps({"type": "user", "origin": {"kind": "human"}, "message": {"content": f"do {i}"}})
             for i in range(4)]
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(lines), encoding="utf-8")
    assert distill.distill(c, str(t), "s1") == "pending"
    rec = [e for e in writes.pending(c) if e["record"]["kind"] == "journal"][0]["record"]
    assert rec["metadata"]["principle_checks"] == {"principle:P-1a2b3c4d": "violated"}


def test_distill_rejects_a_journal_missing_checks_when_principles_exist(cfg, user_config, fake_model, tmp_path):
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(jtext()))}\n')
    c = cfg()
    _active(c)
    t = tmp_path / "t.jsonl"
    t.write_text("\n".join(json.dumps({"type": "user", "origin": {"kind": "human"},
                                       "message": {"content": "x"}}) for _ in range(4)), encoding="utf-8")
    assert distill.distill(c, str(t), "s1") == "reject"


# ---- acceptance 12: prompt hook -------------------------------------------

def _hook(monkeypatch, event, payload):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert cli.main(["hook", event]) == 0
    return out.getvalue()


def test_a12_prompt_hook_injects_matches_once_per_session(cfg, tmp_path, monkeypatch):
    c = cfg()
    _active(c)
    writes.write(c, "cli", "note", "the qdrant healthcheck needs a start_period on first compose up",
                 source_id="note:q", approved_by="t")
    payload = {"session_id": "s-1", "cwd": str(tmp_path),
               "prompt": "the qdrant compose healthcheck is failing again"}
    first = _hook(monkeypatch, "prompt", payload)
    ctx = json.loads(first)["hookSpecificOutput"]["additionalContext"]
    assert "note:q" in ctx and "prompt]" in ctx
    assert _hook(monkeypatch, "prompt", payload) == ""            # already shown
    p2 = {**payload, "prompt": "about to make an engineering commit, board task claimed?"}
    ctx2 = json.loads(_hook(monkeypatch, "prompt", p2))["hookSpecificOutput"]["additionalContext"]
    assert "P-1a2b3c4d" in ctx2


def test_a12_prompt_hook_respects_its_cap(cfg, user_config, tmp_path):
    user_config("[budget]\nprompt_chars = 300\n")
    c = cfg()
    for i in range(5):
        writes.write(c, "cli", "note", f"qdrant healthcheck compose note {i} " + "z" * 200,
                     source_id=f"note:{i}", approved_by="t")
    block, ids = recall.prompt_block(c, "qdrant healthcheck compose trouble", "s")
    assert block and len(block) <= 300 and "cut to fit" in block


def test_a12_short_prompt_and_failures_are_silent(cfg, tmp_path, monkeypatch):
    c = cfg()
    writes.write(c, "cli", "note", "qdrant healthcheck", source_id="note:q", approved_by="t")
    assert _hook(monkeypatch, "prompt", {"session_id": "s", "cwd": str(tmp_path), "prompt": "ok"}) == ""
    state = learn._state_file(c)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{broken", encoding="utf-8")
    assert _hook(monkeypatch, "prompt", {"session_id": "s", "cwd": str(tmp_path),
                                         "prompt": "qdrant healthcheck compose failing"}) == ""


def test_session_start_ids_are_not_repeated_by_the_prompt_hook(cfg, tmp_path, monkeypatch):
    c = cfg()
    _active(c)
    start = _hook(monkeypatch, "session-start", {"session_id": "s-9", "cwd": str(tmp_path)})
    assert "P-1a2b3c4d" in start
    out = _hook(monkeypatch, "prompt", {"session_id": "s-9", "cwd": str(tmp_path),
                                        "prompt": "engineering commit board task claim"})
    assert "P-1a2b3c4d" not in out


# ---- acceptance 13: session start -----------------------------------------

def test_a13_session_start_injects_core_sections_not_the_whole_journal(cfg):
    c = cfg()
    _active(c)
    put_journal(c, "session:1", Decisions="- an unrelated decision", Unfinished="- resume the hook")
    block = recall.assemble(c)
    assert "resume the hook" in block and "unrelated decision" not in block
    assert "P-1a2b3c4d" in block


def test_a13_budget_holds_with_many_principles(cfg, user_config):
    user_config("[budget]\ntotal_chars = 800\nprinciples_chars = 600\ncore_chars = 400\nrecall_chars = 300\n")
    c = cfg()
    for i in range(30):
        _active(c, pid=f"P-{i:08x}", statement=f"rule number {i} " + "w" * 60)
    put_journal(c, "session:1", Unfinished="- " + "u" * 900)
    block = recall.assemble(c)
    assert 0 < len(block) <= 800 and "cut" in block


def test_empty_core_sections_restores_the_whole_journal(cfg, user_config):
    user_config("[journal]\ncore_sections = []\n")
    c = cfg()
    put_journal(c, "session:1", Decisions="- an unrelated decision", Unfinished="- resume")
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
    with pytest.raises(learn.StateError):
        learn.run(c)
    learn.run(c, reset_state=True)
    assert learn.load_state(c)["version"] == 1


def test_learn_and_curiosity_need_trust_in_a_project(cfg, tmp_path):
    (tmp_path / ".nimblemind.toml").write_text("[learn]\nauto = false\n[curiosity]\nmax_attempts = 9\n",
                                               encoding="utf-8")
    c = cfg()
    assert c["learn"]["auto"] is True and c["curiosity"]["max_attempts"] == 2
    assert any("not trusted" in n for n in c.notes)


@pytest.mark.parametrize("text", ["[learn]\nsimilarity = 0\n", "[learn]\nsimilarity = nan\n",
                                  "[learn]\nrecur_min = 0\n", "[learn]\nwhat = 1\n",
                                  "[curiosity]\ncost = {surprise = 9}\n",
                                  "[curiosity]\ncost = {other = 1}\n",
                                  "[curiosity]\ngaps_file = 'x'\n",
                                  "[researcher]\ntools = ['Web Search']\n",
                                  "[budget]\nprinciples_chars = 99999\n"])
def test_learning_config_is_validated(cfg, user_config, text):
    user_config(text)
    with pytest.raises(config_mod.ConfigError):
        cfg()


def test_cli_gap_principle_and_learn_round_trip(cfg, no_model, capsys):
    assert cli.main(["gap", "add", "why do sessions hit the usage limit early", "--impact", "4"]) == 0
    assert "pending gap:G-" in capsys.readouterr().out
    assert cli.main(["principle", "add", "--statement", "fix it inside the task",
                     "--applies-when", "a review round finds a defect"]) == 0
    capsys.readouterr()
    assert cli.main(["learn", "--json"]) == 0
    assert "signals" in json.loads(capsys.readouterr().out)
    with pytest.raises(SystemExit):
        cli.main(["gap", "add", "x", "--impact", "9"])


def test_principle_retire_needs_a_terminal(cfg, monkeypatch):
    c = cfg()
    _active(c)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(cli.NotATerminal):
        cli.main(["principle", "retire", "P-1a2b3c4d"])
    assert len(learn.active_principles(c)) == 1


def test_api_writes_go_through_provider_approval(cfg):
    r = api.store("a note from another tool about qdrant")
    assert r["state"] == "pending"
    config_mod.set_grant("provider", True)
    r = api.store("a stored note about qdrant healthcheck", source_id="note:api")
    assert r["state"] == "stored"
    assert api.recall("qdrant healthcheck")[0]["source_id"] == "note:api"
    assert api.gaps_add("why is it slow", impact=3)["state"] == "stored"
    assert api.gaps_list()[0]["signal"] == "manual"
    with pytest.raises(ValueError):
        api.store("x", kind="principle")
    assert api.delete("note:api") is True


def test_a7_hermes_provider_stub(tmp_path):
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
