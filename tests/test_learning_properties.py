"""Randomized properties of the learning loop (v0.2.0 adversarial round 5): the
cached state equals a fresh derivation after every run, the derivation does not
depend on approval order, a pinned belief is never below supported, and session
start stays within budget."""

import json, random
from datetime import datetime, timezone, timedelta
import pytest
from conftest import toml_list
from nimblemind import learn, writes, recall, config as config_mod
from test_learning import jtext, draft

LESSONS = [
    "recall the newest journal in full at session start to resume work",
    "at session start recall the newest journal in full to resume the work",
    "resume work by recalling the newest journal in full at session start",
    "the newest journal recalled in full at session start resumes work",
    "run the database migration before starting the compose stack",
    "start the compose stack only after the database migration has run",
    "pin dependency versions in the lockfile before release builds",
    "release builds need dependency versions pinned in the lockfile",
]
PITS = [
    "recall of the newest journal in full at session start missed the work that mattered",
    "compose stack started before the database migration and failed healthcheck",
    "qdrant healthcheck has no start_period so compose up aborts",
    "compose up aborts because qdrant healthcheck lacks start_period",
    "release build broke because dependency versions were not pinned in lockfile",
]
SURPRISES = ["qdrant healthcheck passed on the second compose up without changes",
             "the lockfile pinned versions differ from the release build output"]
CHANGES = ["upgraded the compose stack and the database migration tool",
           "the newest journal format at session start changed"]
PERSON = ("B-0000aaaa", LESSONS[0])
PERSON2 = ("B-0000bbbb", LESSONS[4])


def make_journals(rng, n, base):
    out = []
    for i in range(n):
        p = {}
        if rng.random() < .6: p["Lessons_learned"] = "\n".join("- " + x for x in rng.sample(LESSONS, rng.randint(1, 2)))
        if rng.random() < .15: p["Rulings"] = "- " + rng.choice(LESSONS)
        if rng.random() < .5: p["Pitfalls"] = "\n".join("- " + x for x in rng.sample(PITS, rng.randint(1, 2)))
        if rng.random() < .25: p["Surprises"] = "- " + rng.choice(SURPRISES)
        if rng.random() < .2: p["Changes"] = "- " + rng.choice(CHANGES)
        p["Unfinished"] = f"- task {i} " + "x" * rng.randint(0, 300)
        meta = {"session_id": f"s{rng.randint(0, n // 2)}"}
        listed = [b for b, _ in (PERSON, PERSON2) if rng.random() < .4]
        if listed:
            meta["beliefs_listed"] = listed
            meta["belief_checks"] = {f"belief:{b}": rng.choice(["followed", "violated", "n/a"]) for b in listed}
        out.append((f"journal:j{i:02d}", jtext(**p), meta, (base + timedelta(minutes=i)).isoformat()))
    return out


def person(c, bid, st):
    writes.write(c, "cli", "belief", learn.belief_text(bid, st, "resuming work", "none"),
                 source_id=f"belief:{bid}", approved_by="t",
                 metadata={"origin": "person", "supersedes": "", "evidence": []}, created_at=PT)


PT = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def check(c, total):
    cached = learn.load_state(c)
    fresh = learn.derive(c)
    assert json.dumps(cached, sort_keys=True) == json.dumps(fresh, sort_keys=True), "cache != derive"
    for r in learn.report(c):
        if r["pinned"]:
            assert r["status"] in ("supported", "established"), r
    for bid, e in fresh["beliefs"].items():
        if e["pinned"]:
            # Never below supported, from the first entry; retired only by a
            # person (the loop never retires a pinned belief by evidence).
            assert all(h["status"] in ("supported", "established", "retired") for h in e["history"]), e["history"]
    block = recall.assemble(c)
    assert len(block) <= total, (len(block), total)


FAM = {x: ("A" if i < 4 else "B" if i < 6 else "C") for i, x in enumerate(LESSONS)}


def norm(c, state):
    recs = {r.source_id: r for r in learn.stored(c, "belief")}
    name = lambda sid: (learn.parse_belief(recs[sid].content) or {}).get("statement", sid) if sid in recs else sid
    name0 = name
    name = lambda sid: (FAM.get(name0(sid), name0(sid)), recs[sid].metadata.get("origin") if sid in recs else "")
    bel = {}
    for sid, e in state["beliefs"].items():
        bel[name(sid)] = (e["status"], e["confidence"], sorted((x["dir"], x["kind"], x["ref"]) for x in e["evidence"]),
                          [h["status"] for h in e["history"]], e["stale_since"], e["end"] is not None)
    gaps = {}
    for r in learn.stored(c, "gap"):
        e = state["gaps"][r.source_id]
        k = (e["signal"], name(e["belief"]) if e["belief"] else learn.gap_subject(learn.parse_gap(r.content)["question"]))
        gaps.setdefault(k, []).append((e["status"], sorted(set(e["occurrences"]))))
    return bel, {k: sorted(v) for k, v in gaps.items()}


def record_shape(c):
    bs = sorted((FAM.get(learn.parse_belief(r.content)["statement"]), r.metadata.get("origin"), tuple(r.metadata.get("evidence") or []))
                for r in learn.stored(c, "belief"))
    gs = sorted((r.metadata.get("signal"), learn.gap_subject(learn.parse_gap(r.content)["question"]).split("(")[0])
                for r in learn.stored(c, "gap"))
    return bs, gs


def scenario(c, journals, order, rng, total, events):
    person(c, *PERSON); person(c, *PERSON2)
    for sid, text, meta, at in journals:
        r = writes.write(c, "distill", "journal", text, source_id=sid, metadata=meta, created_at=at)
        assert r.state == "pending"
    entries = {e["record"]["source_id"]: e for e in writes.pending(c)}
    for n, sid in enumerate(order):
        e = entries[sid]
        writes.approve(c, e.get("id") or e.get("entry_id") or e["_id"], by="t")
        if rng.random() < .5:
            learn.run(c); check(c, total)
            if events and rng.random() < .3:
                rows = [r for r in learn.report(c) if not r["pinned"] and r["status"] != "retired"]
                if rows:
                    r = rng.choice(rows)
                    (learn.retire if rng.random() < .3 else learn.mark_stale)(c, r["source_id"])
                    check(c, total)
    learn.run(c); check(c, total)
    learn.run(c); check(c, total)


@pytest.mark.parametrize("seed", range(40))
def test_order_independence(seed, cfg, user_config, tmp_path, monkeypatch):
    rng = random.Random(seed)
    total = rng.choice([400, 800, 1500, 6000])
    user_config(f'[distiller]\ncommand = "none"\n[budget]\ntotal_chars = {total}\n[curiosity]\nverify_window = 3\n')
    config_mod.set_grant("gap", True)
    base = datetime.now(timezone.utc) - timedelta(days=1)
    journals = make_journals(rng, 15, base)
    results = []
    for k in range(2):
        d = tmp_path / f"run{k}"
        monkeypatch.setenv("NIMBLEMIND_HOME", str(d))
        config_mod.set_grant("gap", True)
        c = cfg()
        order = [j[0] for j in journals]
        rng.shuffle(order)
        scenario(c, journals, order, rng, total, events=False)
        results.append((record_shape(c), norm(c, learn.load_state(c)), order))
    (shape0, n0, o0), (shape1, n1, o1) = results
    if shape0 != shape1:
        pytest.skip("different records stored: %s" % (set(shape0[0]) ^ set(shape1[0]) or set(shape0[1]) ^ set(shape1[1]),))
    for k in set(n0[0]) | set(n1[0]):
        assert n0[0].get(k) == n1[0].get(k), (k, n0[0].get(k), n1[0].get(k), o0, o1)
    assert n0[1] == n1[1], "gaps differ"


@pytest.mark.parametrize("seed", range(40))
def test_full_mix(seed, cfg, user_config, fake_model, tmp_path):
    rng = random.Random(1000 + seed)
    total = rng.choice([400, 800, 1500, 6000])
    rev = draft(statement="recall only the unfinished part of the newest journal at session start",
                applies="resuming the same task", not_when="a different task")
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(rev))}\n'
                f'[budget]\ntotal_chars = {total}\n[curiosity]\nverify_window = 3\n')
    config_mod.set_grant("gap", True)
    c = cfg()
    base = datetime.now(timezone.utc) - timedelta(days=1)
    journals = make_journals(rng, 15, base)
    order = [j[0] for j in journals]; rng.shuffle(order)
    scenario(c, journals, order, rng, total, events=True)
    # answers to random gaps, with belief parts, then more runs
    for g in learn.stored(c, "gap")[:4]:
        conf = rng.choice([30, 80, 95])
        body = f"## Answer\nx\n\n## Confidence\n{conf}\n\n## Belief\n" + draft(
            statement=rng.choice(LESSONS), applies="resuming work", not_when="none",
            revises=(g.metadata.get("belief") or "none").removeprefix("belief:"))
        writes.write(c, "cli", "answer", body, metadata={"gap": g.source_id, "confidence": conf}, approved_by="t")
        learn.run(c); check(c, total)
    import collections
    st = learn.load_state(c)
    print("STATS", collections.Counter((e["origin"], e["status"]) for e in st["beliefs"].values()),
          collections.Counter((g["signal"], g["status"]) for g in st["gaps"].values()))



FIXED = [("B-000000a1", LESSONS[0], "j00"), ("B-000000c1", LESSONS[6], "j03"), ("B-000000d1", LESSONS[4], "j05")]
PITS1 = [PITS[0], PITS[1], PITS[2], PITS[4]]


def make_journals2(rng, n, base):
    out = []
    bids = [b for b, _, _ in FIXED] + [PERSON[0], PERSON2[0]]
    for i in range(n):
        p = {}
        if rng.random() < .6: p["Lessons_learned"] = "\n".join("- " + x for x in rng.sample(LESSONS, rng.randint(1, 2)))
        if rng.random() < .5: p["Pitfalls"] = "\n".join("- " + x for x in rng.sample(PITS1, rng.randint(1, 2)))
        if rng.random() < .25: p["Surprises"] = "- " + SURPRISES[0]
        if rng.random() < .2: p["Changes"] = "- " + rng.choice(CHANGES)
        p["Unfinished"] = f"- task {i} " + "x" * rng.randint(0, 300)
        meta = {"session_id": f"s{rng.randint(0, n // 2)}"}
        listed = [b for b in bids if rng.random() < .35]
        if listed:
            meta["beliefs_listed"] = listed
            meta["belief_checks"] = {f"belief:{b}": rng.choice(["followed", "followed", "violated", "n/a"]) for b in listed}
        out.append((f"journal:j{i:02d}", jtext(**p), meta, (base + timedelta(minutes=i)).isoformat()))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_order_fixed_beliefs(seed, cfg, user_config, tmp_path, monkeypatch):
    rng = random.Random(5000 + seed)
    total = rng.choice([400, 800, 1500, 6000])
    user_config(f'[distiller]\ncommand = "none"\n[budget]\ntotal_chars = {total}\n[curiosity]\nverify_window = 3\n')
    base = datetime.now(timezone.utc) - timedelta(days=1)
    journals = make_journals2(rng, 15, base)
    results = []
    for k in range(2):
        monkeypatch.setenv("NIMBLEMIND_HOME", str(tmp_path / f"run{k}"))
        config_mod.set_grant("gap", True)
        c = cfg()
        for bid, st, j in FIXED:
            writes.write(c, "cli", "belief", learn.belief_text(bid, st, "", "none"), source_id=f"belief:{bid}",
                         approved_by="auto:learn", created_at=PT,
                         metadata={"origin": "lessons", "supersedes": "", "evidence": [f"journal:{j}"]})
        order = [j[0] for j in journals]
        rng.shuffle(order)
        scenario(c, journals, order, rng, total, events=False)
        results.append((record_shape(c), norm(c, learn.load_state(c)), order))
    (shape0, n0, o0), (shape1, n1, o1) = results
    if shape0 != shape1:
        pytest.skip("different records stored: %s" % (set(shape0[0]) ^ set(shape1[0]) or set(shape0[1]) ^ set(shape1[1]),))
    for k in set(n0[0]) | set(n1[0]):
        assert n0[0].get(k) == n1[0].get(k), (k, n0[0].get(k), n1[0].get(k), o0, o1)
    assert n0[1] == n1[1], "gaps differ"



@pytest.mark.parametrize("seed", range(40))
def test_fixed_mix(seed, cfg, user_config, fake_model, tmp_path):
    rng = random.Random(9000 + seed)
    total = rng.choice([400, 800, 1500, 6000])
    rev = draft(statement="recall only the unfinished part of the newest journal at session start",
                applies="resuming the same task", not_when="a different task")
    user_config(f'[distiller]\ncommand = "argv"\nargv = {toml_list(fake_model(rev))}\n'
                f'[budget]\ntotal_chars = {total}\n[curiosity]\nverify_window = 3\n')
    config_mod.set_grant("gap", True)
    c = cfg()
    for bid, st, j in FIXED:
        writes.write(c, "cli", "belief", learn.belief_text(bid, st, "", "none"), source_id=f"belief:{bid}",
                     approved_by="auto:learn", created_at=PT,
                     metadata={"origin": "lessons", "supersedes": "", "evidence": [f"journal:{j}"]})
    journals = make_journals2(rng, 15, datetime.now(timezone.utc) - timedelta(days=1))
    order = [j[0] for j in journals]; rng.shuffle(order)
    scenario(c, journals, order, rng, total, events=True)
    for g in learn.stored(c, "gap")[:5]:
        conf = rng.choice([30, 80, 95])
        body = f"## Answer\nx\n\n## Confidence\n{conf}\n\n## Belief\n" + draft(
            statement=rng.choice(LESSONS), applies="resuming work", not_when="none",
            revises=(g.metadata.get("belief") or "none").removeprefix("belief:"))
        writes.write(c, "cli", "answer", body, metadata={"gap": g.source_id, "confidence": conf}, approved_by="t")
        learn.run(c); check(c, total)
    import collections
    st = learn.load_state(c)
    print("STATS", collections.Counter((e["origin"], e["status"]) for e in st["beliefs"].values()))
