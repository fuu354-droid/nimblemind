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

What is believed is derived, never accumulated: `derive` computes every
belief's evidence, confidence, status and history, and every gap's
lifecycle, from the stored records (journals, beliefs, gaps, answers) and
the person's event log, as a function of them alone. So the order in which
records became visible (a journal approved a week late, a run cut short by
`max_new`) cannot change the result, and `--reset-state` is just the same
derivation. The state file is a cache of it. What a run *does* (write a new
belief, a gap, a revision) is a side effect recorded once in `done.jsonl`,
so it happens once whatever is replayed.

Both files hold ids, enumerated words, numbers and timestamps only. Free
text is always a record, written through `writes.write`, so it passes
isolation like every write.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from nimblemind import approval, journal, model, paths, writes
from nimblemind.backends.local import tokens
from nimblemind.fsutil import append_line, atomic_write, file_lock, read_text
from nimblemind.records import Record, now_iso, stamp

STATE_VERSION = 4
STATUSES = ("hypothesis", "supported", "established", "challenged", "stale", "retired")
APPLYING = ("supported", "established", "challenged")
EVIDENCE_KINDS = ("restated", "outcome", "counter", "research", "inherited")
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

_CHECK_LINE = re.compile(r"^-\s*(B-[0-9a-f]{8})\s*:\s*(followed|violated|n/a)\s*(?::|$)", re.IGNORECASE)
_TOKEN_STR = re.compile(r"^\S{1,300}$")
_BID = re.compile(r"^B-[0-9a-f]{8}$")


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


def section_of(text: str, name: str) -> str:
    """A `## name` section of an answer, up to the next `## `."""
    m = re.search(rf"^## {re.escape(name)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


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


def _token(s: str) -> str:
    """`s` if it can sit in the state as an id, else a hash of it."""
    return s if _TOKEN_STR.match(s) and len(s) <= 200 else \
        hashlib.sha256(s.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def session_key(j: Record) -> str:
    """The unit of independence: the session a journal came from. A session
    distilled twice (a resumed session ends twice) is still one vote."""
    sid = j.metadata.get("session_id") if isinstance(j.metadata, dict) else None
    if isinstance(sid, str) and sid:
        return "s:" + _token(sid)
    return "j:" + j.source_id


def shown_ids(j: Record) -> tuple[set[str], set[str]]:
    """(belief ids B-…, record source ids) the session was shown: listed to
    its distiller, or injected at session start or by the prompt hook."""
    meta = j.metadata if isinstance(j.metadata, dict) else {}
    bids, recs = set(), set()
    for key in ("beliefs_listed", "beliefs_shown"):
        for x in meta.get(key) or []:
            if isinstance(x, str) and _BID.match(x.removeprefix("belief:")):
                bids.add(x.removeprefix("belief:"))
    for x in meta.get("records_shown") or []:
        if isinstance(x, str):
            recs.add(x)
            if x.startswith("belief:") and _BID.match(x.removeprefix("belief:")):
                bids.add(x.removeprefix("belief:"))
    return bids, recs


# ---- files ---------------------------------------------------------------

def _dir(cfg) -> Path:
    return paths.data_home() / "learn" / paths.safe_name(cfg.namespace)


def _state_file(cfg) -> Path:
    return _dir(cfg) / "state.json"


def _events_file(cfg) -> Path:
    return _dir(cfg) / "events.jsonl"


def _done_file(cfg) -> Path:
    return _dir(cfg) / "done.jsonl"


def _lock_file(cfg) -> Path:
    return cfg.layout().locks / f"learn-{paths.safe_name(cfg.namespace)}.lock"


def _log(cfg, msg: str) -> None:
    append_line(cfg.layout().logs / "learn.log", f"{now_iso()} {msg}")


def _jsonl(f: Path, ok) -> list[dict]:
    try:
        text = read_text(f)
    except FileNotFoundError:
        return []
    out = []
    for line in text.split("\n"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and ok(e):
            out.append(e)
    return out


def _events(cfg) -> list[dict]:
    return _jsonl(_events_file(cfg), lambda e: e.get("action") in ("retire", "stale")
                  and isinstance(e.get("id"), str) and _TOKEN_STR.match(e["id"])
                  and isinstance(e.get("at"), str) and _TOKEN_STR.match(e["at"]))


def _done(cfg) -> set[str]:
    return {e["k"] for e in _jsonl(_done_file(cfg), lambda e: isinstance(e.get("k"), str)
                                   and _TOKEN_STR.match(e["k"]))}


def _mark_done(cfg, key: str) -> None:
    append_line(_done_file(cfg), json.dumps({"k": key, "at": stamp()}, ensure_ascii=True))


def empty_state() -> dict:
    return {"version": STATE_VERSION, "beliefs": {}, "gaps": {}}


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


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_state(data) -> dict:
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        raise StateError("not a learning state of this version")
    base = empty_state()
    if set(data) != set(base):
        raise StateError(f"keys must be {sorted(base)}")
    for k, v in base.items():
        if not isinstance(data[k], type(v)):
            raise StateError(f"{k}: wrong type")
    _check_value(data, "state")
    for bid, b in data["beliefs"].items():
        if not isinstance(b, dict) or b.get("status") not in STATUSES:
            raise StateError(f"beliefs.{bid}: missing or unknown status")
        if not _number(b.get("confidence")) or not 0 <= b["confidence"] <= 1:
            raise StateError(f"beliefs.{bid}: confidence must be a number from 0 to 1")
        ev = b.get("evidence")
        if not isinstance(ev, list) or not all(
                isinstance(e, dict) and e.get("dir") in ("for", "against")
                and e.get("kind") in EVIDENCE_KINDS for e in ev):
            raise StateError(f"beliefs.{bid}: malformed evidence")
        if not isinstance(b.get("history"), list) or not all(
                isinstance(h, dict) and h.get("status") in STATUSES for h in b["history"]):
            raise StateError(f"beliefs.{bid}: malformed history")
    for gid, g in data["gaps"].items():
        if not isinstance(g, dict) or g.get("status") not in GAP_STATUSES:
            raise StateError(f"gaps.{gid}: missing or unknown status")
        if g.get("signal") not in (*WEIGHT, "manual"):
            raise StateError(f"gaps.{gid}: unknown signal")
    return data


def load_state(cfg) -> dict:
    """The cached derivation; empty before the first run."""
    f = _state_file(cfg)
    try:
        raw = read_text(f)
    except FileNotFoundError:
        return empty_state()
    try:
        return validate_state(json.loads(raw))
    except (ValueError, StateError) as e:
        raise StateError(f"{f}: {e}; `nimblemind learn --reset-state` rebuilds it") from None


def save_state(cfg, state: dict) -> None:
    validate_state(state)
    f = _state_file(cfg)
    f.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(f, json.dumps(state, ensure_ascii=True, sort_keys=True, indent=1) + "\n")


# ---- reading the store ---------------------------------------------------

def stored(cfg, kind: str) -> list[Record]:
    """Stored records of one kind, oldest first. Never pending or held."""
    recs = [r for r in writes.canonical(cfg).list(cfg.namespace) if r.kind == kind]
    return sorted(recs, key=lambda r: (_when(r.created_at), r.source_id))


def _pending(cfg, kind: str) -> list[Record]:
    out = []
    for e in writes.pending(cfg):
        rec = e.get("record") or {}
        if rec.get("kind") == kind:
            try:
                out.append(Record.from_dict(rec))
            except (KeyError, ValueError):
                continue
    return out


def _held(cfg, kind: str) -> list[Record]:
    out = []
    for e in writes.held(cfg):
        rec = e.get("record") or {}
        if rec.get("kind") == kind:
            try:
                out.append(Record.from_dict(rec))
            except (KeyError, ValueError):
                continue
    return out


def _origin(rec: Record) -> str:
    o = rec.metadata.get("origin") if isinstance(rec.metadata.get("origin"), str) else ""
    return o if o in ("person", "ruling", "lessons", "research", "revision") else ""


def _person_approved(log: dict, ns: str, journal_id: str) -> bool:
    """Whether a person stored this journal: not a standing grant, not an
    automatic path, not a bulk import of another tool's files."""
    e = log.get((ns, journal_id)) or {}
    by = e.get("approved_by", "")
    return bool(by) and not by.startswith(("grant:", "auto:")) and e.get("rule") != "migrated"


# ---- derivation ----------------------------------------------------------

def _counts(ev: list[dict]) -> tuple[int, int]:
    s = sum(1 for e in ev if e["dir"] == "for")
    return s, len(ev) - s


def _conf(ev: list[dict]) -> float:
    s, c = _counts(ev)
    return (s + 1) / (s + 2 * c + 2)


def _status(ev: list[dict], pinned: bool, stale_since: str | None, retired: bool) -> str:
    """Section 15.4, first match wins, over the evidence so far (oldest
    first). A pinned belief is never below supported, stale or challenged."""
    s, c = _counts(ev)
    conf = (s + 1) / (s + 2 * c + 2)
    if retired:
        return "retired"
    if pinned:
        return "established" if s >= 4 and conf >= 0.8 else "supported"
    if c >= 2 and conf < 0.25:
        return "retired"
    if stale_since and not any(e["dir"] == "for" and after(e["at"], stale_since) for e in ev):
        return "stale"
    if ev and ev[-1]["dir"] == "against" and conf < 0.6:
        return "challenged"
    if s >= 4 and conf >= 0.8:
        return "established"
    if s >= 2 and conf >= 0.6:
        return "supported"
    return "hypothesis"


def _first_per_session(ev: list[dict]) -> list[dict]:
    """One `for` and one `against` per session: the earliest of each."""
    seen, out = set(), []
    for e in sorted(ev, key=lambda e: (_when(e["at"]), e["ref"])):
        key = (e["dir"], e["session"])
        if key not in seen:
            seen.add(key)
            out.append(e)
    return out


class _World:
    """Everything a derivation reads, loaded once."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.lr, self.cu = cfg["learn"], cfg["curiosity"]
        self.journals = stored(cfg, "journal")
        self.by_jid = {j.source_id: j for j in self.journals}
        self.beliefs = [(r, p) for r in stored(cfg, "belief") if (p := parse_belief(r.content))]
        self.parsed = {r.source_id: p for r, p in self.beliefs}
        self.gaps = [(r, p) for r in stored(cfg, "gap") if (p := parse_gap(r.content))]
        self.answers = [(a, True) for a in stored(cfg, "answer")]
        self.answers.sort(key=lambda x: _when(x[0].created_at))
        # Answers a person has not decided on: the gap waits for them, and is
        # neither answered by them nor researched again meanwhile.
        self.awaiting = {a.metadata.get("gap") for a in _pending(cfg, "answer") + _held(cfg, "answer")}
        self.events = _events(cfg)
        self.log = approval.latest(cfg.layout().approvals)
        self.succ: dict[str, list[Record]] = {}
        for r, _ in self.beliefs:
            sup = r.metadata.get("supersedes")
            if isinstance(sup, str) and sup:
                self.succ.setdefault(sup, []).append(r)
        lr = self.lr
        self.parts = {}
        for j in self.journals:
            meta = j.metadata if isinstance(j.metadata, dict) else {}
            bids, recs = shown_ids(j)
            raw = meta.get("belief_checks") if isinstance(meta.get("belief_checks"), dict) else {}
            self.parts[j.source_id] = {
                "session": session_key(j), "bids": bids, "recs": recs,
                "checks": {k: v for k, v in raw.items()
                           if isinstance(k, str) and k.removeprefix("belief:") in bids},
                "lessons": [(s, i, content_tokens(i)) for s, i in items(j.content, lr["lesson_sections"])],
                "bad": [content_tokens(i) for _, i in items(j.content, lr["pitfall_sections"]
                                                            + lr["surprise_sections"])],
                "pits": [(i, content_tokens(i)) for _, i in items(j.content, lr["pitfall_sections"])],
                "surprises": [(i, content_tokens(i)) for _, i in items(j.content, lr["surprise_sections"])],
                "changes": [content_tokens(i) for _, i in items(j.content, lr["change_sections"])],
                "person": _person_approved(self.log, cfg.namespace, j.source_id),
            }

    def born(self, rec: Record) -> str:
        """A lesson's or ruling's belief is born with the session it came
        from; every other belief when it was written."""
        if _origin(rec) in ("lessons", "ruling"):
            ev = rec.metadata.get("evidence") or []
            if ev and isinstance(ev[0], str) and ev[0] in self.by_jid:
                return self.by_jid[ev[0]].created_at
        return rec.created_at


def _belief_entry(w: _World, rec: Record, p: dict, done: dict[str, dict]) -> dict:
    cfg, lr = w.cfg, w.lr
    th = lr["similarity"]
    sid, bid = rec.source_id, p["bid"]
    origin = _origin(rec)
    pinned = origin in PINNED_ORIGINS
    born = w.born(rec)
    meta_ev = [x for x in (rec.metadata.get("evidence") or []) if isinstance(x, str)]
    derived = set(meta_ev) if origin in ("research", "revision") else set()
    # The sessions it was drawn from, whichever journal of theirs is read,
    # and the answers it came from: a session shown one of those answers is
    # not independent of it. Journals shown at session start show only their
    # core sections, never the lessons, so showing a journal is not counted.
    derived_answers = {x for x in derived if x.startswith("answer:")}
    drawn = set(derived)
    if origin == "research":
        # Every session that raised its question, as the gap stands now, not
        # only those visible when the answer was written.
        gap_ids = {a.metadata.get("gap") for a, _ in w.answers if a.source_id in derived_answers}
        for g, _ in w.gaps:
            if g.source_id in gap_ids:
                occ, later = _gap_occurrences(w, g, done)
                drawn |= set(occ) | {jid for _, jid in later}
    sup_id = rec.metadata.get("supersedes")
    if origin == "revision" and isinstance(sup_id, str) and sup_id in done:
        # Every counter-example of the belief it revises, as derived now.
        drawn |= {e["ref"] for e in done[sup_id]["evidence"] if e["dir"] == "against"}
    derived_sessions = {w.parts[x]["session"] for x in drawn if x in w.parts}
    sig = signature(p)
    ends = [w.born(s) for s in w.succ.get(sid, [])]
    retire_at = min((e["at"] for e in w.events if e["action"] == "retire" and e["id"] == sid),
                    key=_when, default=None)
    succ_at = min(ends, key=_when) if ends else None
    succ_id = min(w.succ.get(sid, []), key=lambda r: _when(w.born(r))).source_id if ends else ""
    end = min([t for t in (succ_at, retire_at) if t], key=_when, default=None)
    ev: list[dict] = []
    sup = rec.metadata.get("supersedes")
    if isinstance(sup, str) and sup in done:
        # What held still held, now under narrower conditions.
        ev += [{**e, "kind": "inherited"} for e in done[sup]["evidence"]
               if e["dir"] == "for" and not after(e["at"], born)]
    stale_marks = [e["at"] for e in w.events if e["action"] == "stale" and e["id"] == sid] if not pinned else []
    for j in w.journals:
        if end and not after(end, j.created_at):
            break
        pt = w.parts[j.source_id]
        shown = bid in pt["bids"] or bool(derived_answers & pt["recs"])
        at, sess = j.created_at, pt["session"]
        for sec, _, t in pt["lessons"]:
            if not similar(t, sig, th):
                continue
            if sec in lr["ruling_sections"] and pt["person"] and not pinned:
                continue      # a ruling may say the opposite; it never adopts a learned belief
            if not shown and sess not in derived_sessions:
                ev.append({"dir": "for", "kind": "restated", "ref": j.source_id, "session": sess, "at": at})
        hit = any(similar(t, sig, th) for t in pt["bad"])
        if pt["checks"].get(sid) == "followed" and not hit:
            ev.append({"dir": "for", "kind": "outcome", "ref": j.source_id, "session": sess, "at": at})
        if bid in pt["bids"] and hit:
            ev.append({"dir": "against", "kind": "counter", "ref": j.source_id, "session": sess, "at": at})
        if not pinned and any(similar(t, sig, th) for t in pt["changes"]):
            stale_marks.append(at)
    gap_ids = {g.source_id for g, _ in w.gaps if g.metadata.get("belief") == sid
               and g.metadata.get("signal") == "contradiction"}
    for g in gap_ids:
        for a, is_stored in w.answers:
            if not is_stored or a.metadata.get("gap") != g:
                continue
            conf = a.metadata.get("confidence")
            if not _number(conf) or conf < w.cu["confidence_floor"]:
                continue
            if end and not after(end, a.created_at):
                break
            against = any(a.source_id in (s.metadata.get("evidence") or []) for s in w.succ.get(sid, []))
            ev.append({"dir": "against" if against else "for", "kind": "research", "ref": a.source_id,
                       "session": "a:" + g, "at": a.created_at})
            break
    ev = _first_per_session(ev)
    # Replay the belief's life in time order to get its history.
    points = sorted({born, *(e["at"] for e in ev), *stale_marks,
                     *([retire_at] if retire_at else []), *([succ_at] if succ_at else [])}, key=_when)
    cause0 = sup or (meta_ev[0] if meta_ev else "") or "new"
    status = "supported" if pinned else "hypothesis"     # a person's word starts there
    history = [{"status": status, "at": born, "cause": _token(cause0)}]
    for t in points:
        if after(born, t):
            continue
        so_far = [e for e in ev if not after(e["at"], t)]
        stale = max((s for s in stale_marks if not after(s, t)), key=_when, default=None)
        retired = bool((retire_at and not after(retire_at, t)) or (succ_at and not after(succ_at, t)))
        new = _status(so_far, pinned, stale, retired)
        if new != status:
            if succ_at and t == succ_at and new == "retired":
                cause = succ_id
            elif retire_at and t == retire_at and new == "retired":
                cause = "person"
            elif t == born:
                cause = cause0
            else:
                cause = next((e["ref"] for e in reversed(so_far) if e["at"] == t), None) \
                    or next((j.source_id for j in w.journals if j.created_at == t), "change")
            history.append({"status": new, "at": t, "cause": _token(cause)})
            status = new
    return {"status": status, "confidence": round(_conf(ev), 4), "evidence": ev, "history": history,
            "pinned": pinned, "origin": origin, "born": born, "end": end,
            "stale_since": max(stale_marks, key=_when) if stale_marks else None}


def _gap_occurrences(w: _World, rec: Record, beliefs: dict[str, dict]) -> tuple[list[str], list[tuple[str, str]]]:
    """(the journals in the gap's own evidence, (time, journal id) of every
    other journal whose signal matches it, whenever the journal is from)."""
    th = w.lr["similarity"]
    meta = rec.metadata
    signal = meta.get("signal") if meta.get("signal") in WEIGHT else "manual"
    bel = meta.get("belief") if isinstance(meta.get("belief"), str) else ""
    occ = [x for x in (meta.get("evidence") or []) if isinstance(x, str) and _TOKEN_STR.match(x)]
    p = parse_gap(rec.content) or {"question": ""}
    subject = content_tokens(gap_subject(p["question"]))
    later: list[tuple[str, str]] = []
    for j in w.journals:
        if j.source_id in occ:
            continue
        pt = w.parts[j.source_id]
        hit = False
        if signal in ("recurring", "surprise"):
            pool = pt["pits"] if signal == "recurring" else pt["surprises"]
            hit = any(similar(t, subject, th) for _, t in pool)
        elif signal == "not-holding":
            hit = pt["checks"].get(bel) == "violated"
        elif signal == "contradiction" and bel in w.parsed:
            # Every counter-example journal, as for the other kinds: not the
            # belief's evidence, which keeps one per session.
            sig = signature(w.parsed[bel])
            hit = w.parsed[bel]["bid"] in pt["bids"] and any(similar(t, sig, th) for t in pt["bad"])
        if hit:
            later.append((j.created_at, j.source_id))
    return occ, later


def _gap_entry(w: _World, rec: Record, p: dict, beliefs: dict[str, dict]) -> dict:
    lr, cu = w.lr, w.cu
    meta = rec.metadata
    signal = meta.get("signal") if meta.get("signal") in WEIGHT else "manual"
    bel = meta.get("belief") if isinstance(meta.get("belief"), str) else ""
    bel = bel if _TOKEN_STR.match(bel or "x") else ""
    occ, later = _gap_occurrences(w, rec, beliefs)
    timeline = [(a.created_at, "a", a, s) for a, s in w.answers if a.metadata.get("gap") == rec.source_id]
    timeline += [(t, "j", jid, None) for t, jid in later]
    timeline.sort(key=lambda x: (_when(x[0]), x[1]))
    g = {"status": "open", "signal": signal, "occurrences": list(occ), "attempts": 0,
         "confidence": None, "answers": [], "answered_at": None, "belief": bel, "recurred": 0,
         "awaiting": rec.source_id in w.awaiting}
    last_attempt = ""
    for t, kind, obj, _ in timeline:
        if kind == "a":
            last_attempt = t
            conf = obj.metadata.get("confidence")
            conf = float(conf) if _number(conf) and math.isfinite(conf) else 0.0
            g["answers"].append(obj.source_id)
            g["confidence"] = conf
            if conf >= cu["confidence_floor"]:
                g["status"], g["answered_at"] = "answered", t
            else:
                g["attempts"] += 1
                if g["attempts"] >= cu["max_attempts"]:
                    g["status"] = "parked"
        else:
            g["occurrences"].append(obj)
            if g["status"] == "answered" and after(t, g["answered_at"]):
                g["status"] = "recurred"
                g["recurred"] += 1
                g["attempts"] = 0
            elif g["status"] == "parked" and after(t, last_attempt):
                g["status"] = "open"
                g["attempts"] = 0
    if g["status"] == "answered" and sum(
            1 for j in w.journals if after(j.created_at, g["answered_at"])) >= cu["verify_window"]:
        g["status"] = "verified"
    return g


def derive(cfg, w: _World | None = None) -> dict:
    """Section 15's whole state as a function of the records and events."""
    w = w or _World(cfg)
    out = empty_state()
    for rec, p in sorted(w.beliefs, key=lambda x: (_when(w.born(x[0])), x[0].source_id)):
        out["beliefs"][rec.source_id] = _belief_entry(w, rec, p, out["beliefs"])
    for rec, p in w.gaps:
        out["gaps"][rec.source_id] = _gap_entry(w, rec, p, out["beliefs"])
    return out


# ---- reading derived beliefs ---------------------------------------------

def _fresh_entry(rec: Record) -> dict:
    origin = _origin(rec)
    pinned = origin in PINNED_ORIGINS
    return {"status": "supported" if pinned else "hypothesis", "confidence": 0.5, "evidence": [],
            "history": [], "pinned": pinned, "origin": origin, "born": rec.created_at,
            "end": None, "stale_since": None}


def beliefs(cfg, state: dict | None = None) -> list[tuple[Record, dict, dict]]:
    """Every stored belief with its parsed text and derived entry (the
    cache's, or a fresh one for a record stored since the last run), newest
    first."""
    state = state if state is not None else load_state(cfg)
    recs = stored(cfg, "belief")
    superseded = {r.metadata.get("supersedes") for r in recs if r.metadata.get("supersedes")}
    out = []
    for r in recs:
        p = parse_belief(r.content)
        if not p:
            continue
        e = state["beliefs"].get(r.source_id) or _fresh_entry(r)
        if r.source_id in superseded and e["status"] != "retired":
            e = {**e, "status": "retired"}
        out.append((r, p, e))
    return list(reversed(out))


def is_verifying(e: dict) -> bool:
    """A revision that has had no evidence of its own since it was born."""
    return e.get("origin") == "revision" and not any(
        e2["kind"] != "inherited" and after(e2["at"], e.get("born", "")) for e2 in e["evidence"])


def applying(cfg, state: dict | None = None) -> list[tuple[Record, dict, dict]]:
    """Beliefs a session works from (section 15.8): supported, established,
    challenged, every revision not yet out of hypothesis, and every pinned
    belief that is not retired. Stale beliefs are not among them."""
    return [(r, p, e) for r, p, e in beliefs(cfg, state)
            if e["status"] in APPLYING or (e["status"] != "retired" and e.get("pinned"))
            or (e["status"] == "hypothesis" and e.get("origin") == "revision")]


def known_gaps(cfg) -> list[tuple[Record, str]]:
    """Stored gaps and gaps still waiting for approval. A pending gap still
    blocks a duplicate: the person has not decided yet."""
    return [(r, "stored") for r in stored(cfg, "gap")] + [(r, "pending") for r in _pending(cfg, "gap")]


def gap_entry(state: dict, rec: Record) -> dict:
    """The derived lifecycle of a stored gap, or a fresh one before the
    first run sees it."""
    g = state["gaps"].get(rec.source_id)
    if g is not None:
        return g
    sig = rec.metadata.get("signal") if rec.metadata.get("signal") in WEIGHT else "manual"
    return {"status": "open", "signal": sig, "attempts": 0, "confidence": None, "answers": [],
            "answered_at": None, "belief": rec.metadata.get("belief") or "", "recurred": 0,
            "occurrences": [x for x in (rec.metadata.get("evidence") or []) if isinstance(x, str)],
            "awaiting": False}


# ---- side effects --------------------------------------------------------

def new_belief(cfg, statement: str, applies: str, not_when: str, *, origin: str,
               evidence: list[str], supersedes: str = "", path: str = "learn") -> tuple[str, str]:
    """Write one belief record. Returns (write state, source id)."""
    taken = {r.source_id.removeprefix("belief:") for r in stored(cfg, "belief")}
    bid = new_id("B", taken)
    sid = f"belief:{bid}"
    meta = {"origin": origin, "supersedes": supersedes, "evidence": evidence}
    r = writes.write(cfg, path, "belief", belief_text(bid, statement, applies, not_when),
                     source_id=sid, metadata=meta)
    _log(cfg, f"belief {r.state} {sid} origin={origin}" + (f" supersedes={supersedes}" if supersedes else ""))
    return r.state, sid


def _all_beliefs_for_coverage(cfg) -> list[tuple[dict, bool, str]]:
    """(parsed, pinned, source id) for stored and pending beliefs, so a lesson
    waiting in the approval queue is not proposed twice."""
    out = []
    for r in stored(cfg, "belief") + _pending(cfg, "belief"):
        p = parse_belief(r.content)
        if p:
            out.append((p, _origin(r) in PINNED_ORIGINS, r.source_id))
    return out


def _has_successor(cfg, sid: str) -> bool:
    return any(r.metadata.get("supersedes") == sid for r in stored(cfg, "belief") + _pending(cfg, "belief"))


def _new_beliefs_from_journals(cfg, w: _World, state: dict, done: set[str]) -> tuple[int, list[dict]]:
    """Uncovered lessons become hypotheses; journal signals are collected.
    Journals oldest first, at most `learn.max_new` beliefs per run; each
    lesson item is handled once, and a journal is done once all of it is."""
    lr = w.lr
    th = lr["similarity"]
    # Retired beliefs still cover: restating a refuted or retired belief is not
    # a first observation, and a fresh belief would inherit the old support
    # without the counter-examples that retired it.
    retired = {sid for sid, e in state["beliefs"].items() if e["status"] == "retired"}
    # (signature, a person's and still standing). A person's retired belief
    # never absorbs their new ruling: that is a new word of theirs.
    cover = [(signature(p), pinned and sid not in retired) for p, pinned, sid in _all_beliefs_for_coverage(cfg)]
    created, signals = 0, []
    for j in w.journals:
        if "signals:" + _token(j.source_id) not in done:
            signals += _journal_signals(w, j, done)
        jt = _token(j.source_id)
        key = "journal:" + jt
        if key in done:
            continue
        pt = w.parts[j.source_id]
        finished = True
        # Item by item, in section order, each handled exactly once whatever
        # became of its write (stored, pending, held, rejected later), with the
        # cover updated after each: so a journal split across runs by
        # `max_new` proposes exactly what one run would have.
        for sec, item, t in pt["lessons"]:
            # Keyed by content, not position: a change to `lesson_sections`
            # or a repeated line cannot make an unhandled item look handled.
            digest = hashlib.sha256(f"{sec}\0{item}".encode("utf-8")).hexdigest()[:16]
            ikey = f"lesson:{jt}:{digest}"
            if ikey in done:
                continue
            ruling = sec in lr["ruling_sections"] and pt["person"]
            if not any(similar(t, sig, th) and (pinned or not ruling) for sig, pinned in cover):
                if created >= lr["max_new"]:
                    finished = False
                    break          # the rest waits for the next run, in order
                origin = "ruling" if ruling else "lessons"
                state_, _ = new_belief(cfg, item, "", "", origin=origin, evidence=[j.source_id])
                if state_ in ("stored", "pending"):
                    created += 1
                    cover.append((t, ruling))
            _mark_done(cfg, ikey)
            done.add(ikey)
        if finished:
            _mark_done(cfg, key)
    return created, signals


def _journal_signals(w: _World, j: Record, done: set[str]) -> list[dict]:
    """The journal's signals not yet turned into (or merged into) a gap. Each
    carries the done key it is marked with once handled, and the journal's
    `signals:` key once all of them are."""
    lr = w.lr
    th = lr["similarity"]
    pt = w.parts[j.source_id]
    out = []
    for item, t in pt["pits"]:
        others = [o.source_id for o in w.journals if o.source_id != j.source_id
                  and _TOKEN_STR.match(o.source_id)
                  and any(similar(t, x, th) for _, x in w.parts[o.source_id]["pits"])]
        if len(others) >= lr["recur_min"] - 1:
            out.append({"signal": "recurring", "item": item, "tokens": t, "journal": j.source_id,
                        "belief": "", "also": others})
    for item, t in pt["surprises"]:
        out.append({"signal": "surprise", "item": item, "tokens": t, "journal": j.source_id, "belief": ""})
    for sid, verdict in pt["checks"].items():
        if verdict == "violated":
            out.append({"signal": "not-holding", "belief": sid, "journal": j.source_id,
                        "item": "", "tokens": frozenset()})
    jt = _token(j.source_id)
    for s in out:
        s["at"] = j.created_at
        what = s["belief"] or s["item"]
        s["key"] = f"sig:{s['signal']}:{jt}:{hashlib.sha256(what.encode('utf-8')).hexdigest()[:16]}"
    pending = [s for s in out if s["key"] not in done]
    for s in pending:
        s["journal_key"] = "signals:" + jt
    if not pending:
        _mark_done(w.cfg, "signals:" + jt)
    return pending


def _beliefs_from_answers(cfg, w: _World, state: dict, done: set[str]) -> int:
    """A stored, confident answer's `Belief` part becomes a belief once: a
    revision of the belief it names (never a person's, never one that already
    has a successor), else a research hypothesis that has to earn support
    from later sessions like any other."""
    made = 0
    for a, is_stored in w.answers:
        key = "answer:" + _token(a.source_id)
        if not is_stored or key in done:
            continue
        conf = a.metadata.get("confidence")
        part = section_of(a.content, "Belief")
        statement, applies = _field(part, "Statement"), _field(part, "Applies when")
        if not (_number(conf) and conf >= w.cu["confidence_floor"]) or not statement \
                or statement.lower() == "none" or not applies:
            _mark_done(cfg, key)
            continue
        if any(a.source_id in (r.metadata.get("evidence") or [])
               for r in stored(cfg, "belief") + _pending(cfg, "belief") + _held(cfg, "belief")):
            _mark_done(cfg, key)   # written before, the done mark was lost
            continue
        rev = _field(part, "Revises").strip().strip(".")
        target = f"belief:{rev}" if _BID.match(rev) else ""
        e = state["beliefs"].get(target)
        if not e or e["status"] == "retired" or e.get("pinned") or _has_successor(cfg, target):
            if target and e and e.get("pinned"):
                _log(cfg, f"answer {a.source_id} would revise pinned {target}; kept as a hypothesis")
            target = ""
        g = next((r for r, _ in w.gaps if r.source_id == a.metadata.get("gap")), None)
        evidence = [x for x in ((g.metadata.get("evidence") or []) if g else []) if isinstance(x, str)]
        evidence = list(dict.fromkeys([*evidence, *(state["gaps"].get(g.source_id, {}).get("occurrences", [])
                                                    if g else []), a.source_id]))
        st, _ = new_belief(cfg, statement, applies, _field(part, "Not when") or "none",
                           origin="revision" if target else "research", evidence=evidence,
                           supersedes=target)
        if st in ("stored", "pending"):
            made += 1
        _mark_done(cfg, key)
    return made


REVISE_SYSTEM = """A working belief of an AI agent was contradicted by experience.
Rewrite it so it only claims what the evidence still supports.
Reply with exactly these three lines and nothing else:
Statement: <one sentence>
Applies when: <narrowed to the situations where it held>
Not when: <the situations of the counter-examples>"""


def _revisions(cfg, w: _World, state: dict, done: set[str]) -> tuple[int, list[dict]]:
    """Challenged beliefs get one drafted revision per new counter-example
    while none is stored or waiting; without a drafter, and for a person's
    belief, a contradiction question instead."""
    lr = w.lr
    revised, signals = 0, []
    parsed = {r.source_id: (r, p) for r, p in w.beliefs}
    for sid, e in state["beliefs"].items():
        against = [x for x in e["evidence"] if x["dir"] == "against"]
        if not against:
            continue
        key = f"{'contra' if e['pinned'] else 'revise'}:{_token(sid)}:{len(against)}"
        if e["pinned"]:
            if key not in done:
                signals.append({"signal": "contradiction", "belief": sid, "journal": against[-1]["ref"],
                                "item": "", "tokens": frozenset(), "key": key, "at": against[-1]["at"]})
            continue
        if e["status"] != "challenged" or _has_successor(cfg, sid) or key in done:
            continue
        r, p = parsed[sid]
        drafted = False
        if cfg["distiller"].get("command") != "none":
            counter = []
            for x in against:
                j = w.by_jid.get(x["ref"])
                if j is not None:
                    counter += [i for _, i in items(j.content, lr["pitfall_sections"] + lr["surprise_sections"])
                                if similar(content_tokens(i), signature(p), lr["similarity"])]
            user = (f"Belief {p['bid']}:\nStatement: {p['statement']}\nApplies when: {p['applies_when']}\n"
                    f"Not when: {p['not_when']}\n\nWhat happened while it applied:\n"
                    + "\n".join(f"- {c}" for c in counter[:10]))
            try:
                text = model.run(cfg["distiller"], REVISE_SYSTEM, user).text
                st, ap, nw = _field(text, "Statement"), _field(text, "Applies when"), _field(text, "Not when")
                if st and ap and nw:
                    w_state, _ = new_belief(cfg, st, ap, nw, origin="revision",
                                            evidence=[x["ref"] for x in against], supersedes=sid)
                    drafted = w_state in ("stored", "pending")
                    revised += drafted
                else:
                    _log(cfg, f"revision of {sid} refused: a label is missing")
            except model.ModelError as err:
                _log(cfg, f"revision of {sid} failed: {err}")
        if drafted:
            _mark_done(cfg, key)
        else:
            signals.append({"signal": "contradiction", "belief": sid, "journal": against[-1]["ref"],
                            "item": "", "tokens": frozenset(), "key": key, "at": against[-1]["at"]})
    return revised, signals


def _merge_target(state, gaps, s: dict, threshold: float):
    for rec, _ in gaps:
        g = state["gaps"].get(rec.source_id)
        if g and g["status"] == "verified" and not (
                s.get("at") and g.get("answered_at") and not after(s["at"], g["answered_at"])):
            continue      # it stopped; a journal from before the answer is an old occurrence
        if rec.metadata.get("signal") != s["signal"]:
            continue
        if s["belief"]:
            if rec.metadata.get("belief") == s["belief"] and rec.metadata.get("signal") == s["signal"]:
                return rec
            continue
        parsed = parse_gap(rec.content)
        if parsed and similar(content_tokens(gap_subject(parsed["question"])), s["tokens"], threshold):
            return rec
    return None


def _write_gaps(cfg, state, signals: list[dict]) -> dict:
    th = cfg["learn"]["similarity"]
    gaps = known_gaps(cfg)
    merged = 0
    fresh: list[dict] = []
    for s in signals:
        if _merge_target(state, gaps, s, th) is not None:
            merged += 1        # the derivation counts it as an occurrence
            _mark_done(cfg, s["key"])
            continue
        twin = next((f for f in fresh if f["signal"] == s["signal"] and (
            (s["belief"] and f["belief"] == s["belief"]) or
            (not s["belief"] and not f["belief"] and similar(f["tokens"], s["tokens"], th)))), None)
        if twin:
            for j in [*s.get("also", []), s["journal"]]:
                if j not in twin["journals"]:
                    twin["journals"].append(j)
            twin["keys"].append(s["key"])
            continue
        fresh.append({**s, "journals": list(dict.fromkeys([*s.get("also", []), s["journal"]])),
                      "keys": [s["key"]]})
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
        r = writes.write(cfg, "gap", "gap", gap_text(gid, question, f["signal"]),
                         source_id=f"gap:{gid}", metadata=meta)
        created += r.state in ("stored", "pending")
        _log(cfg, f"gap {r.state} gap:{gid} {f['signal']}")
        # Written, held or rejected: handled either way, never asked twice.
        for k in f["keys"]:
            _mark_done(cfg, k)
    # A journal whose every signal is now handled is done with.
    handled = _done(cfg)
    for jk in {s["journal_key"] for s in signals if s.get("journal_key")}:
        if all(s["key"] in handled for s in signals if s.get("journal_key") == jk):
            _mark_done(cfg, jk)
    return {"gaps_merged": merged, "gaps_created": created,
            "gaps_dropped": max(0, len(fresh) - MAX_NEW_GAPS)}


def run(cfg, *, reset_state: bool = False) -> dict:
    """One `learn` pass under the namespace's lock: side effects for what is
    new, then the derivation. With `reset_state`, the derivation only: no
    record is written, and the result is what the runs reached."""
    with file_lock(_lock_file(cfg)):
        if reset_state:
            state = derive(cfg)
            save_state(cfg, state)
            return {"beliefs": len(state["beliefs"]), "gaps": len(state["gaps"])}
        done = _done(cfg)
        w = _World(cfg)
        state = derive(cfg, w)
        created, signals = _new_beliefs_from_journals(cfg, w, state, done)
        from_answers = _beliefs_from_answers(cfg, w, state, done)
        # New hypotheses cannot be challenged yet: the first derivation is
        # the right one for revisions.
        revised, more = _revisions(cfg, w, state, done)
        summary = {"beliefs_created": created + from_answers, "beliefs_revised": revised}
        summary.update(_write_gaps(cfg, state, signals + more))
        state = derive(cfg)
        save_state(cfg, state)
    _log(cfg, "run " + " ".join(f"{k}={v}" for k, v in summary.items()))
    return summary


def report(cfg, state: dict | None = None) -> list[dict]:
    out = []
    for r, p, e in beliefs(cfg, state):
        s, c = _counts(e["evidence"])
        out.append({"source_id": r.source_id, "bid": p["bid"], "statement": p["statement"],
                    "applies_when": p["applies_when"], "not_when": p["not_when"],
                    "status": e["status"], "confidence": round(_conf(e["evidence"]), 4),
                    "pinned": e.get("pinned", False), "origin": e.get("origin", ""),
                    "verifying": is_verifying(e), "for": s, "against": c,
                    "supersedes": r.metadata.get("supersedes", ""),
                    "last": e["history"][-1] if e["history"] else {}})
    return out


def history(cfg, source_id: str) -> list[dict]:
    """Every version related to `source_id` (its predecessors and all their
    successors), oldest first, each with its transitions and evidence."""
    by_id = {r.source_id: r for r in stored(cfg, "belief")}
    if source_id not in by_id:
        return []
    head, seen = source_id, set()
    while by_id[head].metadata.get("supersedes") in by_id and head not in seen:
        seen.add(head)
        head = by_id[head].metadata["supersedes"]
    succ: dict[str, list[Record]] = {}
    for r in by_id.values():
        if r.metadata.get("supersedes"):
            succ.setdefault(r.metadata["supersedes"], []).append(r)
    state = load_state(cfg)
    chain, queue, seen = [], [head], set()
    while queue:
        cur = queue.pop(0)
        if cur in seen or cur not in by_id:
            continue
        seen.add(cur)
        p = parse_belief(by_id[cur].content) or {}
        e = state["beliefs"].get(cur) or {}
        chain.append({"source_id": cur, "statement": p.get("statement", ""),
                      "applies_when": p.get("applies_when", ""), "not_when": p.get("not_when", ""),
                      "supersedes": by_id[cur].metadata.get("supersedes", ""),
                      "status": e.get("status", "hypothesis"), "history": e.get("history", []),
                      "evidence": e.get("evidence", [])})
        queue += [r.source_id for r in sorted(succ.get(cur, []), key=lambda r: _when(r.created_at))]
    return chain


def _person_event(cfg, action: str, source_id: str) -> bool:
    with file_lock(_lock_file(cfg)):
        rec = writes.canonical(cfg).get(source_id, cfg.namespace)
        if rec is None or rec.kind != "belief":
            return False
        e = derive(cfg)["beliefs"].get(source_id)
        if e is None or e["status"] == "retired" or (action == "stale" and e["pinned"]):
            return False
        append_line(_events_file(cfg), json.dumps({"action": action, "id": source_id, "at": stamp()},
                                                  ensure_ascii=True))
        save_state(cfg, derive(cfg))
    _log(cfg, f"{action} {source_id}")
    return True


def retire(cfg, source_id: str) -> bool:
    """A person retires a belief (a terminal command), kept as an event."""
    return _person_event(cfg, "retire", source_id)


def mark_stale(cfg, source_id: str) -> bool:
    """Something it depends on changed: trusted again only after new support.
    A person's own belief is theirs to retire, never marked stale."""
    return _person_event(cfg, "stale", source_id)
