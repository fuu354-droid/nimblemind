"""The learning loop (DESIGN.md section 15): notice, ask, update what is
believed, and check later whether it changed what happened.

Everything that decides is a rule in this file: which journal items are
signals, when two items are the same thing, when a gap is merged, when a
lesson becomes a principle, when a principle is holding. A model only writes
a principle's wording, and with `distiller.command = "none"` not even that.

State that changes over time (statuses, counts, which journals were read)
lives in `<home>/learn/<ns>/state.json` and holds ids, enumerated words,
numbers and timestamps only. Free text is always a record, written through
`writes.write`, so it passes isolation and approval like every other write.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

from nimblemind import approval, journal, model, paths, writes
from nimblemind.backends.local import tokens
from nimblemind.fsutil import append_line, atomic_write, file_lock, read_text
from nimblemind.records import Record, now_iso

STATE_VERSION = 1
GAP_STATUSES = ("open", "answered", "parked", "verified", "recurred")
VERDICTS = ("not-holding", "untested", "unused", "holding")
CHECK_WORDS = ("followed", "violated", "n/a")
WEIGHT = {"recurring": 2, "surprise": 3, "not-holding": 4}
MAX_NEW_GAPS = 5        # per run: a migrated history must not flood the queue
MAX_PROPOSALS = 3       # principles drafted per run, for the same reason

TEMPLATES = {
    "recurring": "Why does this keep happening, and what would prevent it: ",
    "surprise": "What explains this result differing from what was expected: ",
}
NOT_HOLDING = "Principle {pid} exists but was not applied ({jid}): what in the workflow lets it be skipped?"

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

_ID = re.compile(r"^(P|G)-[0-9a-f]{8}$")
_CHECK_LINE = re.compile(r"^-\s*(P-[0-9a-f]{8})\s*:\s*(followed|violated|n/a)\b", re.IGNORECASE)
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


def principle_text(pid: str, statement: str, applies: str, not_when: str) -> str:
    one = lambda s: " ".join(s.split())  # noqa: E731 - labels are line-based
    return (f"# Principle {pid}\nStatement: {one(statement)}\n"
            f"Applies when: {one(applies) or 'unspecified'}\nNot when: {one(not_when) or 'none'}\n")


def parse_principle(content: str) -> dict | None:
    first = content.split("\n", 1)[0]
    m = re.fullmatch(r"# Principle (P-[0-9a-f]{8})", first.strip())
    statement = _field(content, "Statement")
    if not m or not statement:
        return None
    return {"pid": m.group(1), "statement": statement,
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
    """The part of a gap's question that names the thing, without its
    template wording, so two gaps about one thing compare as one."""
    for prefix in TEMPLATES.values():
        if question.startswith(prefix):
            return question[len(prefix):]
    return question


def new_id(prefix: str, taken: set[str]) -> str:
    while True:
        i = f"{prefix}-{secrets.token_hex(4)}"
        if i not in taken:
            return i


def item_hash(item: str) -> str:
    return hashlib.sha256(" ".join(item.split()).encode("utf-8")).hexdigest()[:32]


def principle_checks(text: str, listed: set[str]) -> dict[str, str]:
    """`{principle source id: verdict}` from a journal's `Principle checks`
    section, keeping only ids that were listed to the distiller."""
    out = {}
    for line in journal.section(text, "Principle checks").split("\n"):
        m = _CHECK_LINE.match(line.strip())
        if m and m.group(1) in listed:
            out["principle:" + m.group(1)] = m.group(2).lower()
    return out


# ---- state ---------------------------------------------------------------

def _state_file(cfg, namespace: str = "") -> Path:
    ns = namespace or cfg.namespace
    return paths.data_home() / "learn" / paths.safe_name(ns) / "state.json"


def _lock_file(cfg, namespace: str = "") -> Path:
    ns = namespace or cfg.namespace
    return cfg.layout(ns).locks / f"learn-{paths.safe_name(ns)}.lock"


def empty_state() -> dict:
    return {"version": STATE_VERSION, "consumed": [], "proposed": [], "gaps": {},
            "principles": {}}


def _check_value(v, where: str) -> None:
    if v is None or isinstance(v, bool):
        return
    if isinstance(v, int):
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


# ---- reading the store ---------------------------------------------------

def stored(cfg, kind: str, namespace: str = "") -> list[Record]:
    """Stored records of one kind, oldest first. Never pending or held."""
    ns = namespace or cfg.namespace
    recs = [r for r in writes.canonical(cfg).list(ns) if r.kind == kind]
    return sorted(recs, key=lambda r: r.created_at)


def activation_times(cfg) -> dict[str, str]:
    """When each record was approved (its newest approval entry), by id."""
    log = approval.latest(cfg.layout().approvals)
    return {sid: e.get("approved_at", "") for (ns, sid), e in log.items()
            if ns == cfg.namespace}


def active_principles(cfg, state: dict | None = None) -> list[tuple[Record, dict]]:
    """Stored principles that nothing supersedes and nobody retired, newest
    first, with their parsed fields."""
    state = state if state is not None else load_state(cfg)
    recs = stored(cfg, "principle")
    superseded = {r.metadata.get("supersedes") for r in recs if r.metadata.get("supersedes")}
    out = []
    for r in recs:
        p = parse_principle(r.content)
        if not p or r.source_id in superseded:
            continue
        if (state["principles"].get(r.source_id) or {}).get("retired_at"):
            continue
        out.append((r, p))
    return list(reversed(out))


def principle_signature(p: dict) -> frozenset:
    return content_tokens(p["statement"] + " " + p.get("applies_when", ""))


# ---- gaps ----------------------------------------------------------------

def _gap_entry(state: dict, rec: Record) -> dict:
    g = state["gaps"].get(rec.source_id)
    if g is None:
        ev = [e for e in (rec.metadata.get("evidence") or []) if isinstance(e, str)
              and _TOKEN_STR.match(e)]
        g = {"status": "open", "signal": rec.metadata.get("signal") or "manual",
             "occurrences": ev, "attempts": 0, "confidence": None, "answers": [],
             "answered_at": None, "principle": rec.metadata.get("principle") or "",
             "recurred": 0}
        if g["signal"] not in WEIGHT and g["signal"] != "manual":
            g["signal"] = "manual"
        state["gaps"][rec.source_id] = g
    return g


def known_gaps(cfg) -> list[tuple[Record, str]]:
    """Stored gaps and gaps still waiting for approval, with where each is.
    A pending gap still blocks a duplicate: the person has not decided yet."""
    out = [(r, "stored") for r in stored(cfg, "gap")]
    for e in writes.pending(cfg):
        rec = e.get("record") or {}
        if rec.get("kind") == "gap":
            try:
                out.append((Record.from_dict(rec), "pending"))
            except (KeyError, ValueError):
                continue
    return out


def _merge_target(cfg, state, gaps, signal: str, subject: frozenset, pid: str,
                  threshold: float):
    for rec, where in gaps:
        g = state["gaps"].get(rec.source_id)
        if g and g["status"] == "verified":
            continue
        if signal == "not-holding":
            if rec.metadata.get("principle") == pid:
                return rec
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


# ---- the run -------------------------------------------------------------

def _signals(cfg, state, journals, active, acts) -> list[dict]:
    lr = cfg["learn"]
    th = lr["similarity"]
    consumed = set(state["consumed"])
    pit_tokens = {j.source_id: [content_tokens(i) for _, i in items(j.content, lr["pitfall_sections"])]
                  for j in journals}
    out = []
    for j in journals:
        if j.source_id in consumed or not _TOKEN_STR.match(j.source_id):
            continue
        for _, item in items(j.content, lr["pitfall_sections"]):
            t = content_tokens(item)
            others = [o.source_id for o in journals if o.source_id != j.source_id
                      and _TOKEN_STR.match(o.source_id)
                      and any(similar(t, x, th) for x in pit_tokens[o.source_id])]
            if len(others) >= lr["recur_min"] - 1:
                # The evidence is every journal the pitfall appears in, so a
                # new gap starts with its true occurrence count.
                out.append({"signal": "recurring", "item": item, "tokens": t,
                            "journal": j.source_id, "occurrences": len(others) + 1, "pid": "",
                            "also": others})
            for rec, p in active:
                at = acts.get(rec.source_id, "")
                if at and j.created_at > at and similar(t, principle_signature(p), th):
                    out.append({"signal": "not-holding", "item": item, "tokens": t,
                                "journal": j.source_id, "occurrences": 1, "pid": rec.source_id})
        for _, item in items(j.content, lr["surprise_sections"]):
            out.append({"signal": "surprise", "item": item, "tokens": content_tokens(item),
                        "journal": j.source_id, "occurrences": 1, "pid": ""})
        checks = j.metadata.get("principle_checks") or {}
        for rec, p in active:
            at = acts.get(rec.source_id, "")
            if checks.get(rec.source_id) == "violated" and at and j.created_at > at:
                out.append({"signal": "not-holding", "item": p["statement"], "tokens": frozenset(),
                            "journal": j.source_id, "occurrences": 1, "pid": rec.source_id})
    return out


def _signals_to_gaps(cfg, state, journals, active, acts) -> dict:
    th = cfg["learn"]["similarity"]
    signals = _signals(cfg, state, journals, active, acts)
    gaps = known_gaps(cfg)
    merged = created = dropped = 0
    fresh: list[dict] = []
    for s in signals:
        target = _merge_target(cfg, state, gaps, s["signal"], s["tokens"], s["pid"], th)
        if target is not None:
            _note_occurrence(state, target, s["journal"])
            merged += 1
            continue
        # Two signals of one run about one thing become one gap.
        twin = next((f for f in fresh if f["signal"] == s["signal"] and (
            (s["pid"] and f["pid"] == s["pid"]) or
            (not s["pid"] and similar(f["tokens"], s["tokens"], th)))), None)
        if twin:
            if s["journal"] not in twin["journals"]:
                twin["journals"].append(s["journal"])
            continue
        fresh.append({**s, "journals": [*s.get("also", []), s["journal"]]})
    fresh.sort(key=lambda f: -WEIGHT[f["signal"]] * (1 + math.log2(max(f["occurrences"], len(f["journals"])))))
    taken = {r.source_id.removeprefix("gap:") for r, _ in gaps}
    for f in fresh[:MAX_NEW_GAPS]:
        gid = new_id("G", taken)
        taken.add(gid)
        if f["signal"] == "not-holding":
            question = NOT_HOLDING.format(pid=f["pid"].removeprefix("principle:"), jid=f["journal"])
        else:
            question = TEMPLATES[f["signal"]] + f["item"]
        meta = {"signal": f["signal"], "evidence": f["journals"]}
        if f["pid"]:
            meta["principle"] = f["pid"]
        r = writes.write(cfg, "gap", "gap", gap_text(gid, question, f["signal"]),
                         source_id=f"gap:{gid}", metadata=meta)
        if r.state in ("stored", "pending"):
            rec = Record(source_id=f"gap:{gid}", namespace=cfg.namespace, kind="gap",
                         content=gap_text(gid, question, f["signal"]), metadata=meta)
            _gap_entry(state, rec)
            created += 1
        _log(cfg, f"gap {r.state} gap:{gid} {f['signal']}")
    dropped = max(0, len(fresh) - MAX_NEW_GAPS)
    for j in journals:
        if j.source_id not in state["consumed"] and _TOKEN_STR.match(j.source_id):
            state["consumed"].append(j.source_id)
    return {"signals": len(signals), "merged": merged, "gaps_created": created,
            "gaps_dropped": dropped}


def _groups(cfg, journals) -> list[list[tuple[str, str, str, frozenset]]]:
    """Lesson items grouped by similarity (single link): each group is a
    list of (journal id, section, item, tokens)."""
    lr = cfg["learn"]
    flat = []
    for j in journals:
        for sec, item in items(j.content, lr["lesson_sections"]):
            t = content_tokens(item)
            if t:
                flat.append((j.source_id, sec, item, t))
    parent = list(range(len(flat)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_token: dict[str, list[int]] = {}
    for i, (_, _, _, t) in enumerate(flat):
        for tok in t:
            by_token.setdefault(tok, []).append(i)
    seen = set()
    for idx in by_token.values():
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                i, k = idx[a], idx[b]
                if (i, k) in seen:
                    continue
                seen.add((i, k))
                if find(i) != find(k) and similar(flat[i][3], flat[k][3], lr["similarity"]):
                    parent[find(i)] = find(k)
    groups: dict[int, list] = {}
    for i, entry in enumerate(flat):
        groups.setdefault(find(i), []).append(entry)
    return list(groups.values())


DRAFT_SYSTEM = """You turn repeated lessons from an AI agent's work journals into one working principle.
Reply with exactly these four lines and nothing else:
Statement: <one sentence: what to do or not do>
Applies when: <the situation that triggers it>
Not when: <the exceptions, or none>
Revises: <the id of ONE listed existing principle this replaces, or none>
Keep the wording general enough to apply next time, specific enough to act on."""


def _draft(cfg, group, overlapping) -> dict | None:
    newest = max(group, key=lambda g: g[0])[2]
    if cfg["distiller"].get("command") == "none":
        return {"statement": newest, "applies_when": "unspecified", "not_when": "none",
                "revises": ""}
    lines = ["Lessons:"] + [f"- {item}" for _, _, item, _ in group[:12]]
    if overlapping:
        lines += ["", "Existing principles that overlap:"]
        lines += [f"- {p['pid']}: {p['statement']} (applies when: {p['applies_when']})"
                  for _, p in overlapping]
    try:
        text = model.run(cfg["distiller"], DRAFT_SYSTEM, "\n".join(lines)).text
    except model.ModelError as e:
        _log(cfg, f"draft failed: {e}")
        return None
    out = {"statement": _field(text, "Statement"), "applies_when": _field(text, "Applies when"),
           "not_when": _field(text, "Not when"), "revises": _field(text, "Revises")}
    if not out["statement"] or not out["applies_when"] or not out["not_when"]:
        _log(cfg, "draft refused: a label is missing")
        return None
    rev = out["revises"].strip().strip(".")
    if rev.lower() in ("", "none"):
        out["revises"] = ""
    elif rev in {p["pid"] for _, p in overlapping}:
        out["revises"] = rev
    else:
        _log(cfg, f"draft refused: revises {rev!r}, which is not an active principle it overlaps")
        return None
    return out


def _lessons_to_principles(cfg, state, journals, active) -> dict:
    lr = cfg["learn"]
    th = lr["similarity"]
    proposed = set(state["proposed"])
    pending_principles = []
    for e in writes.pending(cfg):
        rec = e.get("record") or {}
        if rec.get("kind") == "principle":
            p = parse_principle(rec.get("content") or "")
            if p:
                pending_principles.append(p)
    covering = [p for _, p in active] + pending_principles
    candidates = []
    for group in _groups(cfg, journals):
        hashes = {item_hash(item) for _, _, item, _ in group}
        if hashes <= proposed:
            continue
        spans = {jid for jid, _, _, _ in group}
        ruled = any(sec in lr["ruling_sections"] for _, sec, _, _ in group)
        if len(spans) < lr["recur_min"] and not ruled:
            continue
        gtok = frozenset().union(*(t for _, _, _, t in group))
        cover = next((p for p in covering if any(
            similar(t, principle_signature(p), th) for _, _, _, t in group)), None)
        if cover is not None:
            sid = "principle:" + cover["pid"]
            entry = state["principles"].setdefault(sid, {"retired_at": None, "reinforced": [],
                                                         "verdict": "untested"})
            for jid in sorted(spans):
                if jid not in entry["reinforced"]:
                    entry["reinforced"].append(jid)
            proposed |= hashes
            continue
        candidates.append((len(spans), max(spans), group, hashes, gtok, ruled))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    made = 0
    taken = {r.source_id.removeprefix("principle:") for r in stored(cfg, "principle")}
    for n, _, group, hashes, gtok, ruled in candidates[:MAX_PROPOSALS]:
        overlapping = [(r, p) for r, p in active
                       if gtok & principle_signature(p)]
        draft = _draft(cfg, group, overlapping)
        if draft is None:
            continue           # tried again next run
        pid = new_id("P", taken)
        taken.add(pid)
        meta = {"evidence": sorted({jid for jid, _, _, _ in group}),
                "origin": "rulings" if ruled else "lessons",
                "supersedes": ("principle:" + draft["revises"]) if draft["revises"] else ""}
        r = writes.write(cfg, "learn", "principle",
                         principle_text(pid, draft["statement"], draft["applies_when"],
                                        draft["not_when"]),
                         source_id=f"principle:{pid}", metadata=meta)
        _log(cfg, f"principle {r.state} principle:{pid} from {n} journals")
        if r.state in ("stored", "pending", "held"):
            proposed |= hashes
            made += 1
    state["proposed"] = sorted(proposed)
    return {"principles_proposed": made, "principle_candidates": len(candidates)}


def report(cfg, state: dict | None = None) -> list[dict]:
    """Section 15.8, per active principle, newest first."""
    state = state if state is not None else load_state(cfg)
    lr, cu = cfg["learn"], cfg["curiosity"]
    th = lr["similarity"]
    journals = stored(cfg, "journal")
    acts = activation_times(cfg)
    out = []
    for rec, p in active_principles(cfg, state):
        at = acts.get(rec.source_id, rec.created_at)
        sig = principle_signature(p)
        after = [j for j in journals if j.created_at > at]
        followed = sum(1 for j in after if (j.metadata.get("principle_checks") or {})
                       .get(rec.source_id) == "followed")
        violated = sum(1 for j in after if (j.metadata.get("principle_checks") or {})
                       .get(rec.source_id) == "violated")
        recurred = sum(1 for j in after if any(
            similar(content_tokens(i), sig, th) for _, i in items(j.content, lr["pitfall_sections"])))
        evidence = rec.metadata.get("evidence") or []
        recent = after[-lr["stale_after"]:]
        checked_recently = any(
            (j.metadata.get("principle_checks") or {}).get(rec.source_id) in ("followed", "violated")
            for j in recent)
        if violated or recurred:
            verdict = "not-holding"
        elif len(after) < cu["verify_window"]:
            verdict = "untested"
        elif not checked_recently:
            verdict = "unused"
        else:
            verdict = "holding"
        out.append({"source_id": rec.source_id, "pid": p["pid"], "statement": p["statement"],
                    "activated_at": at, "before": len(evidence), "after": recurred,
                    "followed": followed, "violated": violated,
                    "journals_since": len(after), "verdict": verdict})
    return out


def _verify(cfg, state, journals) -> dict:
    window = cfg["curiosity"]["verify_window"]
    verified = 0
    for sid, g in state["gaps"].items():
        if g["status"] != "answered" or not g.get("answered_at"):
            continue
        later = [j for j in journals if j.created_at > g["answered_at"]]
        if len(later) >= window:
            g["status"] = "verified"
            verified += 1
    return {"gaps_verified": verified}


def run(cfg, *, reset_state: bool = False) -> dict:
    """One `learn` pass (section 15.5), under the namespace's lock."""
    with file_lock(_lock_file(cfg)):
        state = empty_state() if reset_state else load_state(cfg)
        journals = stored(cfg, "journal")
        acts = activation_times(cfg)
        active = active_principles(cfg, state)
        summary = {}
        summary.update(_signals_to_gaps(cfg, state, journals, active, acts))
        summary.update(_lessons_to_principles(cfg, state, journals, active))
        summary.update(_verify(cfg, state, journals))
        verdicts = {r["source_id"]: r["verdict"] for r in report(cfg, state)}
        for sid, v in verdicts.items():
            state["principles"].setdefault(sid, {"retired_at": None, "reinforced": [],
                                                 "verdict": v})["verdict"] = v
        save_state(cfg, state)
    _log(cfg, "run " + " ".join(f"{k}={v}" for k, v in summary.items()))
    return summary


def retire(cfg, source_id: str) -> bool:
    if not any(r.source_id == source_id for r, _ in active_principles(cfg)):
        return False
    with locked_state(cfg) as state:
        entry = state["principles"].setdefault(source_id, {"retired_at": None, "reinforced": [],
                                                           "verdict": "untested"})
        entry["retired_at"] = now_iso()
    _log(cfg, f"retire {source_id}")
    return True
