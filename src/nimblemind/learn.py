"""The learning loop (DESIGN.md section 15): knowledge that evolves with
experience.

Every stored journal is evidence for or against what is already believed.
A first observation is a hypothesis; independent sessions support it; a
session where it applied and the problem happened anyway counts against it;
a challenged belief is revised under narrower conditions and the revision
replaces it. Everything that decides is a rule in this file: what counts as
evidence, what is independent, how confidence moves, which status follows.
A model only writes a revision's wording, and with `distiller.command =
"none"` not even that.

Changing state (evidence lists, statuses, transitions, gap lifecycles, which
journals were read) lives in `<home>/learn/<ns>/state.json` and holds ids,
enumerated words, numbers and timestamps only. Free text is always a record,
written through `writes.write`, so it passes isolation like every write.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from nimblemind import journal, model, paths, writes
from nimblemind.backends.local import tokens
from nimblemind.fsutil import append_line, atomic_write, file_lock, read_text
from nimblemind.records import Record, now_iso, stamp

STATE_VERSION = 2
STATUSES = ("hypothesis", "supported", "established", "challenged", "stale", "retired")
APPLYING = ("supported", "established", "challenged")
GAP_STATUSES = ("open", "answered", "parked", "verified", "recurred")
PINNED_ORIGINS = ("person", "ruling")
WEIGHT = {"contradiction": 5, "not-holding": 4, "surprise": 3, "recurring": 2}
MAX_NEW_GAPS = 5

TEMPLATES = {
    "recurring": "Why does this keep happening, and what would prevent it: ",
    "surprise": "What explains this result differing from what was expected: ",
}
NOT_HOLDING = "Belief {bid} applied but was not followed ({jid}): what in the workflow lets it be skipped?"
CONTRADICTION = "Evidence goes against belief {bid} ({jid}): under what conditions does it actually hold?"

# A fixed list, so similarity never depends on a model or a locale.
STOP = frozenset("""
a about above after again against all also am an and any are as at be because been
before being below between both but by can could did do does doing done down during
each few for from further had has have having he her here hers him his how i if in
into is it its itself just me more most my no nor not now of off on once only or other
our ours out over own same she should so some such than that the their theirs them
then there these they this those through to too under until up very was we were what
when where which while who whom why will with would you your yours yet still again
one two new use used using get got make made via per across within without
""".split())

_CHECK_LINE = re.compile(r"^-\s*(B-[0-9a-f]{8})\s*:\s*(followed|violated|n/a)\b", re.IGNORECASE)
_TOKEN_STR = re.compile(r"^\S{1,300}$")


class StateError(RuntimeError):
    pass


# ---- text ----------------------------------------------------------------

def content_tokens(text: str) -> frozenset[str]:
    """The local backend's tokens, minus stop words and short words. A CJK
    character is a token on its own and is kept whatever its length."""
    return frozenset(t for t in tokens(text)
                     if t not in STOP and (len(t) >= 3 or not t.isascii()))


def similar(a: frozenset, b: frozenset, threshold: float) -> bool:
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= threshold


def items(text: str, sections: list[str]) -> list[tuple[str, str]]:
    """(section, item) for every `- ` line of the named sections, except
    `- none`."""
    out = []
    for name in sections:
        for line in journal.section(text, name).split("\n"):
            line = line.strip()
            if line.startswith("- ") and line.lower() != journal.EMPTY:
                item = line[2:].strip()
                if item:
                    out.append((name, item))
    return out


def _field(content: str, label: str) -> str:
    for line in content.split("\n"):
        if line.startswith(label + ":"):
            return line[len(label) + 1:].strip()
    return ""


def belief_text(bid: str, statement: str, applies: str, not_when: str) -> str:
    def one(s):
        return " ".join(s.split())  # labels are line-based
    return (f"# Belief {bid}\nStatement: {one(statement)}\n"
            f"Applies when: {one(applies) or 'unspecified'}\nNot when: {one(not_when) or 'none'}\n")


def parse_belief(content: str) -> dict | None:
    m = re.fullmatch(r"# Belief (B-[0-9a-f]{8})", content.split("\n", 1)[0].strip())
    statement = _field(content, "Statement")
    if not m or not statement:
        return None
    return {"bid": m.group(1), "statement": statement,
            "applies_when": _field(content, "Applies when"),
            "not_when": _field(content, "Not when")}


def gap_text(gid: str, question: str, signal: str) -> str:
    return f"# Gap {gid}\nQuestion: {' '.join(question.split())}\nSignal: {signal}\n"


def parse_gap(content: str) -> dict | None:
    m = re.fullmatch(r"# Gap (G-[0-9a-f]{8})", content.split("\n", 1)[0].strip())
    question = _field(content, "Question")
    if not m or not question:
        return None
    return {"gid": m.group(1), "question": question, "signal": _field(content, "Signal")}


def gap_subject(question: str) -> str:
    """A gap's question without its template wording, so two gaps about one
    thing compare as one."""
    for prefix in TEMPLATES.values():
        if question.startswith(prefix):
            return question[len(prefix):]
    return question


def new_id(prefix: str, taken: set[str]) -> str:
    while True:
        i = f"{prefix}-{secrets.token_hex(4)}"
        if i not in taken:
            return i


def belief_checks(text: str, listed: set[str]) -> dict[str, str]:
    """`{belief source id: verdict}` from a journal's `Belief checks`
    section, keeping only ids that were listed to the distiller."""
    out = {}
    for line in journal.section(text, journal.CHECKS).split("\n"):
        m = _CHECK_LINE.match(line.strip())
        if m and m.group(1) in listed:
            out["belief:" + m.group(1)] = m.group(2).lower()
    return out


def _when(iso) -> datetime:
    """A stored time as a datetime, so a whole-second time and a microsecond
    stamp compare by time, not as strings."""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def after(a, b) -> bool:
    """Whether time `a` is strictly later than time `b`."""
    return _when(a) > _when(b)


def signature(b: dict) -> frozenset:
    return content_tokens(b["statement"] + " " + b.get("applies_when", ""))


# ---- state ---------------------------------------------------------------

def _state_file(cfg, namespace: str = "") -> Path:
    ns = namespace or cfg.namespace
    return paths.data_home() / "learn" / paths.safe_name(ns) / "state.json"


def _lock_file(cfg, namespace: str = "") -> Path:
    ns = namespace or cfg.namespace
    return cfg.layout(ns).locks / f"learn-{paths.safe_name(ns)}.lock"


def empty_state() -> dict:
    return {"version": STATE_VERSION, "consumed": [], "beliefs": {}, "gaps": {}}


def _check_value(v, where: str) -> None:
    if v is None or isinstance(v, (bool, int)):
        return
    if isinstance(v, float):
        if not math.isfinite(v):
            raise StateError(f"{where}: not a finite number")
        return
    if isinstance(v, str):
        if v and not _TOKEN_STR.match(v):
            raise StateError(f"{where}: free text is not allowed in learning state")
        return
    if isinstance(v, list):
        for i, x in enumerate(v):
            _check_value(x, f"{where}[{i}]")
        return
    if isinstance(v, dict):
        for k, x in v.items():
            _check_value(k, f"{where} key")
            _check_value(x, f"{where}.{k}")
        return
    raise StateError(f"{where}: unexpected {type(v).__name__}")


def validate_state(data) -> dict:
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise StateError("not a learning state of this version")
    base = empty_state()
    if set(data) - set(base):
        raise StateError(f"unknown keys {sorted(set(data) - set(base))}")
    for k, v in base.items():
        if not isinstance(data.get(k, v), type(v)):
            raise StateError(f"{k}: wrong type")
    _check_value(data, "state")
    for bid, b in data.get("beliefs", {}).items():
        if not isinstance(b, dict) or b.get("status") not in STATUSES:
            raise StateError(f"beliefs.{bid}: missing or unknown status")
        ev = b.get("evidence")
        if not isinstance(ev, list) or not all(
                isinstance(e, dict) and e.get("dir") in ("for", "against") for e in ev):
            raise StateError(f"beliefs.{bid}: malformed evidence")
        if not isinstance(b.get("history"), list):
            raise StateError(f"beliefs.{bid}: malformed history")
    for gid, g in data.get("gaps", {}).items():
        if not isinstance(g, dict) or g.get("status") not in GAP_STATUSES:
            raise StateError(f"gaps.{gid}: missing or unknown status")
    return {**base, **data}


def load_state(cfg, namespace: str = "") -> dict:
    f = _state_file(cfg, namespace)
    try:
        raw = read_text(f)
    except FileNotFoundError:
        return empty_state()
    try:
        return validate_state(json.loads(raw))
    except (ValueError, StateError) as e:
        raise StateError(f"{f}: {e}; `nimblemind learn --reset-state` rebuilds it") from None


def save_state(cfg, state: dict, namespace: str = "") -> None:
    validate_state(state)
    f = _state_file(cfg, namespace)
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(f, json.dumps(state, ensure_ascii=True, sort_keys=True, indent=1) + "\n")


@contextmanager
def locked_state(cfg, namespace: str = ""):
    """Load, yield, and save the state under the namespace's learn lock."""
    with file_lock(_lock_file(cfg, namespace)):
        state = load_state(cfg, namespace)
        yield state
        save_state(cfg, state, namespace)


def _log(cfg, msg: str) -> None:
    append_line(cfg.layout().logs / "learn.log", f"{now_iso()} {msg}")


# ---- beliefs -------------------------------------------------------------

def stored(cfg, kind: str, namespace: str = "") -> list[Record]:
    """Stored records of one kind, oldest first. Never pending or held."""
    ns = namespace or cfg.namespace
    recs = [r for r in writes.canonical(cfg).list(ns) if r.kind == kind]
    return sorted(recs, key=lambda r: (_when(r.created_at), r.source_id))


def _counts(entry: dict) -> tuple[int, int]:
    s = sum(1 for e in entry["evidence"] if e["dir"] == "for")
    return s, len(entry["evidence"]) - s


def confidence(entry: dict) -> float:
    s, c = _counts(entry)
    return round((s + 1) / (s + 2 * c + 2), 4)


def _status(entry: dict, superseded: bool) -> str:
    """Section 15.4, first match wins."""
    ev = entry["evidence"]
    s, c = _counts(entry)
    conf = (s + 1) / (s + 2 * c + 2)
    pinned = entry.get("pinned", False)
    if superseded or entry.get("retired_at"):
        return "retired"
    if not pinned and c >= 2 and conf < 0.25:
        return "retired"
    stale = entry.get("stale_since")
    if stale and not any(e["dir"] == "for" and after(e["at"], stale) for e in ev):
        return "stale"
    if not pinned and ev and ev[-1]["dir"] == "against" and conf < 0.6:
        return "challenged"
    if s >= 4 and conf >= 0.8:
        return "established"
    if (s >= 2 and conf >= 0.6) or pinned:
        return "supported"
    return "hypothesis"


def _fresh_entry(rec: Record) -> dict:
    origin = rec.metadata.get("origin") if isinstance(rec.metadata.get("origin"), str) else ""
    if not _TOKEN_STR.match(origin or "x"):
        origin = ""
    return {"status": "hypothesis", "confidence": 0.5, "evidence": [], "history": [],
            "pinned": origin in PINNED_ORIGINS, "origin": origin,
            "stale_since": None, "retired_at": None, "revised": False}


def _entry(state: dict, rec: Record) -> dict:
    """The state entry for a stored belief, created on first sight."""
    e = state["beliefs"].get(rec.source_id)
    if e is None:
        e = state["beliefs"][rec.source_id] = _fresh_entry(rec)
    return e


def _superseded(cfg) -> set[str]:
    return {r.metadata.get("supersedes") for r in stored(cfg, "belief") if r.metadata.get("supersedes")}


def _restatus(state: dict, sid: str, superseded: set[str], at: str, cause: str) -> str:
    e = state["beliefs"][sid]
    new = _status(e, sid in superseded)
    e["confidence"] = confidence(e)
    if new != e["status"]:
        e["history"].append({"status": new, "at": at, "cause": cause})
        e["status"] = new
    return new


def _add_evidence(state: dict, sid: str, direction: str, kind: str, ref: str, at: str) -> bool:
    """At most one `for` and one `against` per belief and source (section
    15.3): one session, one vote."""
    e = state["beliefs"][sid]
    if any(x["dir"] == direction and x["ref"] == ref for x in e["evidence"]):
        return False
    e["evidence"].append({"dir": direction, "kind": kind, "ref": ref, "at": at})
    return True


def beliefs(cfg, state: dict | None = None) -> list[tuple[Record, dict, dict]]:
    """Every stored belief with its parsed text and state entry (a fresh one
    when the state has not seen it yet), newest first."""
    state = state if state is not None else load_state(cfg)
    recs = stored(cfg, "belief")
    superseded = {r.metadata.get("supersedes") for r in recs if r.metadata.get("supersedes")}
    out = []
    for r in recs:
        p = parse_belief(r.content)
        if not p:
            continue
        e = state["beliefs"].get(r.source_id)
        if e is None:
            e = _fresh_entry(r)
            e["status"] = _status(e, r.source_id in superseded)
        elif r.source_id in superseded and e["status"] != "retired":
            e = {**e, "status": "retired"}
        out.append((r, p, e))
    return list(reversed(out))


def applying(cfg, state: dict | None = None) -> list[tuple[Record, dict, dict]]:
    """Beliefs a session works from (section 15.8): supported, established,
    challenged, a revision still being verified, and every pinned belief that
    is not retired."""
    return [(r, p, e) for r, p, e in beliefs(cfg, state)
            if e["status"] in APPLYING or (e["status"] != "retired" and e.get("pinned"))
            or (e["status"] == "hypothesis" and e.get("origin") == "revision")]


# ---- gaps ----------------------------------------------------------------

def _gap_entry(state: dict, rec: Record) -> dict:
    g = state["gaps"].get(rec.source_id)
    if g is None:
        ev = [e for e in (rec.metadata.get("evidence") or []) if isinstance(e, str)
              and _TOKEN_STR.match(e)]
        sig = rec.metadata.get("signal") if rec.metadata.get("signal") in WEIGHT else "manual"
        bel = rec.metadata.get("belief") if isinstance(rec.metadata.get("belief"), str) else ""
        g = {"status": "open", "signal": sig, "occurrences": ev, "attempts": 0,
             "confidence": None, "answers": [], "answered_at": None,
             "belief": bel if _TOKEN_STR.match(bel or "x") else "", "recurred": 0,
             "evidence_given": False}
        state["gaps"][rec.source_id] = g
    return g


def known_gaps(cfg) -> list[tuple[Record, str]]:
    """Stored gaps and gaps still waiting for approval. A pending gap still
    blocks a duplicate: the person has not decided yet."""
    out = [(r, "stored") for r in stored(cfg, "gap")]
    for e in writes.pending(cfg):
        rec = e.get("record") or {}
        if rec.get("kind") == "gap":
            try:
                out.append((Record.from_dict(rec), "pending"))
            except (KeyError, ValueError):
                continue
    return out


def _merge_target(state, gaps, signal: str, subject: frozenset, bid: str, threshold: float):
    for rec, _ in gaps:
        g = state["gaps"].get(rec.source_id)
        if g and g["status"] == "verified":
            continue
        if bid:
            if rec.metadata.get("belief") == bid and rec.metadata.get("signal") == signal:
                return rec
            continue
        if rec.metadata.get("belief"):
            continue
        parsed = parse_gap(rec.content)
        if parsed and similar(content_tokens(gap_subject(parsed["question"])), subject, threshold):
            return rec
    return None


def _note_occurrence(state: dict, rec: Record, journal_id: str) -> None:
    g = _gap_entry(state, rec)
    if journal_id in g["occurrences"]:
        return
    g["occurrences"].append(journal_id)
    if g["status"] == "answered":
        g["status"] = "recurred"       # the answer did not stop it
        g["recurred"] += 1
        g["attempts"] = 0
    elif g["status"] == "parked":
        g["status"] = "open"           # new evidence: worth another attempt
        g["attempts"] = 0


def _write_gaps(cfg, state, signals: list[dict]) -> dict:
    th = cfg["learn"]["similarity"]
    gaps = known_gaps(cfg)
    merged = 0
    fresh: list[dict] = []
    for s in signals:
        target = _merge_target(state, gaps, s["signal"], s["tokens"], s["belief"], th)
        if target is not None:
            _note_occurrence(state, target, s["journal"])
            merged += 1
            continue
        twin = next((f for f in fresh if f["signal"] == s["signal"] and (
            (s["belief"] and f["belief"] == s["belief"]) or
            (not s["belief"] and not f["belief"] and similar(f["tokens"], s["tokens"], th)))), None)
        if twin:
            for j in [*s.get("also", []), s["journal"]]:
                if j not in twin["journals"]:
                    twin["journals"].append(j)
            continue
        fresh.append({**s, "journals": list(dict.fromkeys([*s.get("also", []), s["journal"]]))})
    fresh.sort(key=lambda f: -WEIGHT[f["signal"]] * (1 + math.log2(len(f["journals"]))))
    taken = {r.source_id.removeprefix("gap:") for r, _ in gaps}
    created = 0
    for f in fresh[:MAX_NEW_GAPS]:
        gid = new_id("G", taken)
        taken.add(gid)
        bid = f["belief"].removeprefix("belief:")
        if f["signal"] == "not-holding":
            question = NOT_HOLDING.format(bid=bid, jid=f["journal"])
        elif f["signal"] == "contradiction":
            question = CONTRADICTION.format(bid=bid, jid=f["journal"])
        else:
            question = TEMPLATES[f["signal"]] + f["item"]
        meta = {"signal": f["signal"], "evidence": f["journals"]}
        if f["belief"]:
            meta["belief"] = f["belief"]
        content = gap_text(gid, question, f["signal"])
        r = writes.write(cfg, "gap", "gap", content, source_id=f"gap:{gid}", metadata=meta)
        if r.state in ("stored", "pending"):
            _gap_entry(state, Record(source_id=f"gap:{gid}", namespace=cfg.namespace, kind="gap",
                                     content=content, metadata=meta))
            created += 1
        _log(cfg, f"gap {r.state} gap:{gid} {f['signal']}")
    return {"gaps_merged": merged, "gaps_created": created,
            "gaps_dropped": max(0, len(fresh) - MAX_NEW_GAPS)}


# ---- the run -------------------------------------------------------------

def new_belief(cfg, state, statement: str, applies: str, not_when: str, *, origin: str,
               evidence: list[str], supersedes: str = "", path: str = "learn") -> str | None:
    """Write one belief record; returns its id once stored, else None."""
    taken = {sid.removeprefix("belief:") for sid in state["beliefs"]}
    taken |= {r.source_id.removeprefix("belief:") for r in stored(cfg, "belief")}
    bid = new_id("B", taken)
    sid = f"belief:{bid}"
    meta = {"origin": origin, "supersedes": supersedes, "evidence": evidence}
    content = belief_text(bid, statement, applies, not_when)
    r = writes.write(cfg, path, "belief", content, source_id=sid, metadata=meta)
    _log(cfg, f"belief {r.state} {sid} origin={origin}" + (f" supersedes={supersedes}" if supersedes else ""))
    if r.state != "stored":
        return None
    e = _entry(state, Record(source_id=sid, namespace=cfg.namespace, kind="belief",
                             content=content, metadata=meta))
    e["history"].append({"status": "hypothesis", "at": stamp(),
                         "cause": supersedes or (evidence[0] if evidence else "new")})
    return sid


def _evidence_from(cfg, state, j: Record, live: list[tuple[str, dict]]):
    """Section 15.3 for one journal against the beliefs not retired.
    Returns (signals, lesson items no belief covers)."""
    lr = cfg["learn"]
    th = lr["similarity"]
    meta = j.metadata if isinstance(j.metadata, dict) else {}
    listed = {x for x in (meta.get("beliefs_listed") or []) if isinstance(x, str)}
    checks = meta.get("belief_checks") if isinstance(meta.get("belief_checks"), dict) else {}
    lessons = items(j.content, lr["lesson_sections"])
    bad = [content_tokens(i) for _, i in items(j.content, lr["pitfall_sections"] + lr["surprise_sections"])]
    changes = [content_tokens(i) for _, i in items(j.content, lr["change_sections"])]
    at = j.created_at
    signals = []
    covered = set()
    for sid, p in live:
        e = state["beliefs"][sid]
        sig = signature(p)
        bid = sid.removeprefix("belief:")
        for idx, (sec, item) in enumerate(lessons):
            if similar(content_tokens(item), sig, th):
                covered.add(idx)
                if bid not in listed:
                    _add_evidence(state, sid, "for", "restated", j.source_id, at)
                    if sec in lr["ruling_sections"]:
                        e["pinned"] = True     # a person's ruling endorses it
        hit = any(similar(t, sig, th) for t in bad)
        if checks.get(sid) == "followed" and not hit:
            _add_evidence(state, sid, "for", "outcome", j.source_id, at)
        if bid in listed and hit:
            if _add_evidence(state, sid, "against", "counter", j.source_id, at) and e.get("pinned"):
                signals.append({"signal": "contradiction", "belief": sid, "journal": j.source_id,
                                "item": "", "tokens": frozenset()})
        if checks.get(sid) == "violated":
            signals.append({"signal": "not-holding", "belief": sid, "journal": j.source_id,
                            "item": "", "tokens": frozenset()})
        if any(similar(t, sig, th) for t in changes):
            e["stale_since"] = at
    fresh = [(sec, item) for idx, (sec, item) in enumerate(lessons) if idx not in covered]
    # Two lessons of one journal about one thing are one observation.
    unique: list[tuple[str, str]] = []
    for sec, item in fresh:
        if not any(similar(content_tokens(item), content_tokens(u), th) for _, u in unique):
            unique.append((sec, item))
    return signals, unique


def _pitfall_signals(cfg, j: Record, journals: list[Record], pit_tokens: dict) -> list[dict]:
    lr = cfg["learn"]
    th = lr["similarity"]
    out = []
    for _, item in items(j.content, lr["pitfall_sections"]):
        t = content_tokens(item)
        others = [o.source_id for o in journals if o.source_id != j.source_id
                  and _TOKEN_STR.match(o.source_id)
                  and any(similar(t, x, th) for x in pit_tokens[o.source_id])]
        if len(others) >= lr["recur_min"] - 1:
            out.append({"signal": "recurring", "item": item, "tokens": t, "journal": j.source_id,
                        "belief": "", "also": others})
    for _, item in items(j.content, lr["surprise_sections"]):
        out.append({"signal": "surprise", "item": item, "tokens": content_tokens(item),
                    "journal": j.source_id, "belief": ""})
    return out


REVISE_SYSTEM = """A working belief of an AI agent was contradicted by experience.
Rewrite it so it only claims what the evidence still supports.
Reply with exactly these three lines and nothing else:
Statement: <one sentence>
Applies when: <narrowed to the situations where it held>
Not when: <the situations of the counter-examples>"""


def _revise(cfg, state, rec: Record, p: dict, superseded: set[str]) -> str | None:
    if cfg["distiller"].get("command") == "none":
        return None
    e = state["beliefs"][rec.source_id]
    local = writes.canonical(cfg)
    lr = cfg["learn"]
    counter = []
    for ev in e["evidence"]:
        if ev["dir"] != "against":
            continue
        j = local.get(ev["ref"], cfg.namespace)
        if j is not None:
            counter += [i for _, i in items(j.content, lr["pitfall_sections"] + lr["surprise_sections"])
                        if similar(content_tokens(i), signature(p), lr["similarity"])]
    user = (f"Belief {p['bid']}:\nStatement: {p['statement']}\nApplies when: {p['applies_when']}\n"
            f"Not when: {p['not_when']}\n\nWhat happened while it applied:\n"
            + "\n".join(f"- {c}" for c in counter[:10]))
    try:
        text = model.run(cfg["distiller"], REVISE_SYSTEM, user).text
    except model.ModelError as err:
        _log(cfg, f"revision of {rec.source_id} failed: {err}")
        return None
    st, ap, nw = _field(text, "Statement"), _field(text, "Applies when"), _field(text, "Not when")
    if not st or not ap or not nw:
        _log(cfg, f"revision of {rec.source_id} refused: a label is missing")
        return None
    new = new_belief(cfg, state, st, ap, nw, origin="revision",
                     evidence=[ev["ref"] for ev in e["evidence"] if ev["dir"] == "against"],
                     supersedes=rec.source_id)
    if new:
        # What held still held, now under narrower conditions.
        state["beliefs"][new]["evidence"] = [dict(x) for x in e["evidence"] if x["dir"] == "for"]
        e["revised"] = True
        superseded.add(rec.source_id)
    return new


def run(cfg, *, reset_state: bool = False) -> dict:
    """One `learn` pass, under the namespace's lock."""
    with file_lock(_lock_file(cfg)):
        state = empty_state() if reset_state else load_state(cfg)
        summary = _run(cfg, state)
        save_state(cfg, state)
    _log(cfg, "run " + " ".join(f"{k}={v}" for k, v in summary.items()))
    return summary


def _run(cfg, state) -> dict:
    lr = cfg["learn"]
    journals = stored(cfg, "journal")
    recs = stored(cfg, "belief")
    superseded = {r.metadata.get("supersedes") for r in recs if r.metadata.get("supersedes")}
    parsed: dict[str, tuple[Record, dict]] = {}
    for r in recs:
        p = parse_belief(r.content)
        if p:
            parsed[r.source_id] = (r, p)
            _entry(state, r)
    pit_tokens = {j.source_id: [content_tokens(i) for _, i in items(j.content, lr["pitfall_sections"])]
                  for j in journals}
    consumed = set(state["consumed"])
    signals: list[dict] = []
    created = read = 0
    for j in journals:
        if j.source_id in consumed or not _TOKEN_STR.match(j.source_id):
            continue
        if created >= lr["max_new"]:
            break          # the rest of the history waits for the next run, in order
        # Every belief not retired, whenever it was written: a session that
        # restated it independently supports it whether it came before or
        # after the belief was formed, and the other kinds of evidence need
        # the belief listed to that session, so they cannot predate it.
        live = [(sid, p) for sid, (r, p) in parsed.items()
                if sid not in superseded and state["beliefs"][sid]["status"] != "retired"]
        found, fresh = _evidence_from(cfg, state, j, live)
        signals += found + _pitfall_signals(cfg, j, journals, pit_tokens)
        for sec, item in fresh:
            origin = "ruling" if sec in lr["ruling_sections"] else "lessons"
            sid = new_belief(cfg, state, item, "", "", origin=origin, evidence=[j.source_id])
            if sid:
                created += 1
                _add_evidence(state, sid, "for", "restated", j.source_id, j.created_at)
                rec = writes.canonical(cfg).get(sid, cfg.namespace)
                if rec is not None:
                    parsed[sid] = (rec, parse_belief(rec.content))
        for sid in parsed:
            _restatus(state, sid, superseded, j.created_at, j.source_id)
        state["consumed"].append(j.source_id)
        read += 1
    revised = 0
    for sid, (r, p) in list(parsed.items()):
        e = state["beliefs"][sid]
        _restatus(state, sid, superseded, stamp(), "recheck")
        if e["status"] != "challenged" or e.get("revised"):
            continue
        new = _revise(cfg, state, r, p, superseded)
        if new:
            revised += 1
            rec = writes.canonical(cfg).get(new, cfg.namespace)
            if rec is not None:
                parsed[new] = (rec, parse_belief(rec.content))
                _restatus(state, new, superseded, stamp(), sid)
            _restatus(state, sid, superseded, stamp(), new)
        else:
            last = next((x for x in reversed(e["evidence"]) if x["dir"] == "against"), None)
            if last:
                signals.append({"signal": "contradiction", "belief": sid, "journal": last["ref"],
                                "item": "", "tokens": frozenset()})
    summary = {"journals_read": read, "beliefs_created": created, "beliefs_revised": revised}
    summary.update(_write_gaps(cfg, state, signals))
    summary.update(_verify(cfg, state, journals))
    return summary


def _verify(cfg, state, journals) -> dict:
    window = cfg["curiosity"]["verify_window"]
    verified = 0
    for g in state["gaps"].values():
        if g["status"] != "answered" or not g.get("answered_at"):
            continue
        if sum(1 for j in journals if after(j.created_at, g["answered_at"])) >= window:
            g["status"] = "verified"
            verified += 1
    return {"gaps_verified": verified}


def research_evidence(cfg, state, gap_rec: Record, answer_id: str, against: bool) -> None:
    """A confident research answer about a belief's gap is evidence, once per
    gap (section 15.3)."""
    g = _gap_entry(state, gap_rec)
    sid = g.get("belief")
    if not sid or g.get("evidence_given") or sid not in state["beliefs"]:
        return
    _add_evidence(state, sid, "against" if against else "for", "research", answer_id, stamp())
    g["evidence_given"] = True
    _restatus(state, sid, _superseded(cfg), stamp(), answer_id)


def report(cfg, state: dict | None = None) -> list[dict]:
    out = []
    for r, p, e in beliefs(cfg, state):
        s, c = _counts(e)
        out.append({"source_id": r.source_id, "bid": p["bid"], "statement": p["statement"],
                    "applies_when": p["applies_when"], "not_when": p["not_when"],
                    "status": e["status"], "confidence": confidence(e),
                    "pinned": e.get("pinned", False), "origin": e.get("origin", ""),
                    "for": s, "against": c, "supersedes": r.metadata.get("supersedes", ""),
                    "last": e["history"][-1] if e["history"] else {}})
    return out


def history(cfg, source_id: str) -> list[dict]:
    """The version chain through `source_id`, oldest first, each version with
    its transitions and evidence."""
    by_id = {r.source_id: r for r in stored(cfg, "belief")}
    if source_id not in by_id:
        return []
    nxt = {r.metadata.get("supersedes"): sid for sid, r in by_id.items() if r.metadata.get("supersedes")}
    head, seen = source_id, set()
    while by_id[head].metadata.get("supersedes") in by_id and head not in seen:
        seen.add(head)
        head = by_id[head].metadata["supersedes"]
    state = load_state(cfg)
    chain, cur, seen = [], head, set()
    while cur in by_id and cur not in seen:
        seen.add(cur)
        p = parse_belief(by_id[cur].content) or {}
        e = state["beliefs"].get(cur) or {}
        chain.append({"source_id": cur, "statement": p.get("statement", ""),
                      "applies_when": p.get("applies_when", ""), "not_when": p.get("not_when", ""),
                      "status": e.get("status", "hypothesis"), "history": e.get("history", []),
                      "evidence": e.get("evidence", [])})
        cur = nxt.get(cur)
    return chain


def retire(cfg, source_id: str) -> bool:
    """A person retires a belief (a terminal command)."""
    with locked_state(cfg) as state:
        rec = writes.canonical(cfg).get(source_id, cfg.namespace)
        if rec is None or rec.kind != "belief":
            return False
        e = _entry(state, rec)
        if e["status"] == "retired":
            return False
        e["retired_at"] = stamp()
        e["history"].append({"status": "retired", "at": e["retired_at"], "cause": "person"})
        e["status"] = "retired"
    _log(cfg, f"retire {source_id}")
    return True


def mark_stale(cfg, source_id: str) -> bool:
    """Something it depends on changed: trust it again only after new support."""
    with locked_state(cfg) as state:
        rec = writes.canonical(cfg).get(source_id, cfg.namespace)
        if rec is None or rec.kind != "belief":
            return False
        e = _entry(state, rec)
        if e["status"] == "retired":
            return False
        e["stale_since"] = stamp()
        _restatus(state, source_id, _superseded(cfg), e["stale_since"], "person")
    _log(cfg, f"stale {source_id}")
    return True
