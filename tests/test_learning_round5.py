"""Regression tests for v0.2.0 adversarial round 5 (contradiction gaps)."""

import time
from datetime import datetime, timezone, timedelta
from nimblemind import learn, writes, config as config_mod
from test_learning import jtext, _person_belief, COUNTER

def ts(base, sec):
    return (base + timedelta(seconds=sec)).isoformat()

def gaps_of(c, signal):
    return [r for r in learn.stored(c, "gap") if r.metadata.get("signal") == signal]

def test_r5_late_counter_older_than_answer_is_absorbed_by_verified_contradiction_gap(cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n[curiosity]\nverify_window = 2\n')
    config_mod.set_grant("gap", True)
    c = cfg()
    bid = _person_belief(c)
    def w(sid, sec, meta, **p):
        time.sleep(0.01)
        return writes.write(c, "cli", "journal", jtext(**p), source_id=sid, metadata=meta,
                            approved_by="t", created_at=mid if sid == "session:2" else "")
    w("session:1", 10, {"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    learn.run(c)
    [g1] = gaps_of(c, "contradiction")
    mid = learn.now_iso() if False else datetime.now(timezone.utc).isoformat()
    time.sleep(0.01)
    writes.write(c, "cli", "answer", "## Answer\nx\n\n## Confidence\n90\n", source_id="answer:a1",
                 metadata={"gap": g1.source_id, "confidence": 90}, approved_by="t")
    w("session:3", 40, {}, Unfinished="- other")
    w("session:4", 50, {}, Unfinished="- other again")
    learn.run(c)
    assert learn.load_state(c)["gaps"][g1.source_id]["status"] == "verified"
    # A journal from before the answer, approved late: an old occurrence.
    w("session:2", 20, {"beliefs_listed": [bid]}, Pitfalls=COUNTER)
    learn.run(c)
    st = learn.load_state(c)["gaps"]
    assert "session:2" in st[g1.source_id]["occurrences"] and st[g1.source_id]["status"] == "verified"
    assert len(gaps_of(c, "contradiction")) == 1, [(g.source_id, g.created_at, g.metadata, st[g.source_id]["occurrences"], st[g.source_id]["status"]) for g in gaps_of(c, "contradiction")]


import pytest
@pytest.mark.parametrize("first", ["A", "B"])
def test_r5_contradiction_occurrences_depend_on_approval_order(first, cfg, user_config):
    user_config('[distiller]\ncommand = "none"\n')
    config_mod.set_grant("gap", True)
    c = cfg()
    bid = _person_belief(c)
    meta = {"beliefs_listed": [bid], "session_id": "same"}
    ids = {}
    for n in ("A", "B"):
        time.sleep(0.01)
        ids[n] = writes.write(c, "distill", "journal", jtext(Pitfalls=COUNTER), source_id=f"journal:{n}",
                              metadata=meta).entry_id
    for n in (first, "B" if first == "A" else "A"):
        writes.approve(c, ids[n], by="t")
        learn.run(c)
    [g] = gaps_of(c, "contradiction")
    occ = learn.load_state(c)["gaps"][g.source_id]["occurrences"]
    assert sorted(occ) == ["journal:A", "journal:B"], occ

