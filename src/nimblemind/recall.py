"""Injection: the session-start block and the per-prompt block, each inside
its budget (DESIGN.md sections 9 and 10).

Session start, counted together against `budget.total_chars`: the source
label, the beliefs that apply (whole items, capped at `beliefs_chars`), core
memory (the newest stored journal's `core_sections`, capped at
`core_chars`), recall (older records' recall sections, capped at
`recall_chars`), and a truncation note whenever anything was cut. Over the
total, recall is dropped first, core is cut next, beliefs are cut last.
Every cut is named in the note; nothing is trimmed silently.

Fail-open: any error means nothing is injected, and no session is ever
blocked. Fail-open never reaches the write side: recall only ever reads
stored records, never `pending/` or `held/`.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from nimblemind import journal, learn, writes
from nimblemind.fsutil import atomic_write, read_text
from nimblemind.paths import data_home

RECALL_QUERY = "unfinished work next steps blockers decisions pitfalls"
SESSION_TTL = 7 * 86400
MAX_QUERY_CHARS = 2000
# Records never shown as recall: beliefs have their own part, and a gap is
# a question, not memory.
NOT_RECALLED = ("belief:", "gap:")


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def _cut_items(lines: list[str], limit: int) -> tuple[list[str], bool]:
    """Whole items from the front while they fit."""
    out, used = [], 0
    for line in lines:
        extra = len(line) + (1 if out else 0)
        if used + extra > limit:
            return out, True
        out.append(line)
        used += extra
    return out, False


def label(cfg) -> str:
    return f"[nimblemind {cfg.namespace}]"


def latest_journal(cfg):
    """The newest stored journal in this namespace, or None."""
    for rec in writes.canonical(cfg).list(cfg.namespace):
        if rec.kind == "journal":
            return rec
    return None


def core_view(cfg, rec) -> str:
    """What session start shows of the newest journal: its `core_sections`,
    or all of it when that list is empty."""
    sections = cfg["journal"]["core_sections"]
    if not sections:
        return rec.content.strip()
    parts = []
    for name in sections:
        body = journal.section(rec.content, name)
        if body and body != journal.EMPTY:
            parts.append(f"{name}:\n{body}")
    return "\n".join(parts)


def _mark(e: dict) -> str:
    st = e["status"]
    if st == "challenged":
        return "challenged: counter-evidence, being revised"
    if st == "stale":
        return "stale: something it depends on changed"
    if learn.is_verifying(e):
        return "revised: verifying"
    if e.get("pinned"):
        return "yours"
    return f"{st} {e['confidence']:.2f}"


def belief_line(p: dict, e: dict) -> str:
    when = p.get("applies_when") or "unspecified"
    nw = p.get("not_when") or "none"
    return f"- {p['bid']} [{_mark(e)}]: {p['statement']} (applies when: {when}; not when: {nw})"


def _rank(e: dict) -> tuple:
    urgent = e["status"] == "challenged" or learn.is_verifying(e)
    return (not urgent, not e.get("pinned"), -e["confidence"])


def belief_lines(cfg) -> list[tuple[str, str]]:
    """(source id, line) for the beliefs that apply (section 15.8):
    challenged and revised first, then the person's, then by confidence."""
    state = learn.load_state(cfg)
    ranked = sorted(learn.applying(cfg, state), key=lambda x: _rank(x[2]))
    return [(r.source_id, belief_line(p, e)) for r, p, e in ranked]


def _view(cfg, ev) -> str:
    view = journal.recall_view(cfg, ev.content)
    if not view:
        if journal.is_journal(cfg, ev.content):
            return ""  # a journal whose recall sections are all empty adds nothing
        view = ev.content  # a note or an answer has no sections: shown as is
    return f"[{ev.source_id[:100]}] {view.strip()}"


def older(cfg, exclude: str, k: int) -> list[tuple[str, str]]:
    """Up to k (source id, snippet), never the journal already shown as
    core, never a belief or a gap."""
    if k <= 0:
        return []
    # A backend failure propagates: fail-open means nothing is injected at
    # all, not core memory without its recall.
    hits = writes.recall_backend(cfg).recall(RECALL_QUERY, cfg.namespace, k + 9)
    out = []
    for ev in hits:
        if ev.source_id == exclude or ev.source_id.startswith(NOT_RECALLED):
            continue
        v = _view(cfg, ev)
        if v:
            out.append((ev.source_id, v))
        if len(out) >= k:
            break
    return out


def _render(head: str, parts: list[tuple[str, str]], notes: list[str]) -> str:
    out = [head] + [f"{title}:\n{body}" for title, body in parts if body]
    if notes:
        out.append("[nimblemind: " + "; ".join(notes) + "]")
    return "\n\n".join(out)


def assemble_with_ids(cfg) -> tuple[str, list[str]]:
    """The session-start block and the ids it shows, or ("", [])."""
    b = cfg["budget"]
    core_rec = latest_journal(cfg)
    prin = belief_lines(cfg)
    snippets = older(cfg, core_rec.source_id if core_rec else "", b["recall_k"])
    core_full = core_view(cfg, core_rec) if core_rec else ""
    if not core_full and not snippets and not prin:
        return "", []

    head = label(cfg)
    notes: list[str] = []
    p_lines, p_cut = _cut_items([line for _, line in prin], b["beliefs_chars"])
    if p_cut:
        notes.append(f"beliefs cut to {b['beliefs_chars']} chars")
    core, core_cut = _cut(core_full, b["core_chars"])
    if core_cut:
        notes.append(f"core memory cut to {b['core_chars']} chars")
    recall_text, recall_cut = _cut("\n---\n".join(s for _, s in snippets), b["recall_chars"])
    if recall_cut:
        notes.append(f"recall cut to {b['recall_chars']} chars")

    def render():
        return _render(head, [("Beliefs", "\n".join(p_lines)),
                              ("Last session", core), ("Earlier sessions", recall_text)], notes)

    total = b["total_chars"]
    if len(render()) > total and recall_text:
        notes = [n for n in notes if not n.startswith("recall")] + ["recall dropped to fit the total budget"]
        recall_text = ""
    if len(render()) > total and core:
        notes = [n for n in notes if not n.startswith("core")] + \
            [f"core memory cut to fit the total budget of {total} chars"]
        keep = len(core) - (len(render()) - total)
        core = _cut(core, keep)[0] if keep > 0 else ""
    if len(render()) > total and p_lines:
        notes = [n for n in notes if not n.startswith("beliefs")] + \
            [f"beliefs cut to fit the total budget of {total} chars"]
        while p_lines and len(render()) > total:
            p_lines = p_lines[:-1]
    out = render()
    if len(out) > total or (not p_lines and not core and not recall_text):
        # Label and notes alone do not fit, or nothing is left to say:
        # inject nothing rather than break the budget or send only a label.
        return "", []
    ids = [sid for sid, line in prin if line in p_lines]
    if core and core_rec:
        ids.append(core_rec.source_id)
    if recall_text:
        ids += [sid for sid, _ in snippets]
    return out, ids


def assemble(cfg) -> str:
    """The block to inject, or "" when there is nothing to say."""
    return assemble_with_ids(cfg)[0]


# ---- per prompt ----------------------------------------------------------

PLACEHOLDER_IDS = frozenset({"", "unknown", "null", "none", "undefined", "nil"})


def real_session_id(session_id) -> str:
    """The harness's own session id, or "" for a missing or placeholder one."""
    s = session_id.strip() if isinstance(session_id, str) else ""
    return s if s and s.lower() not in PLACEHOLDER_IDS else ""


def session_key_for(session_id, transcript_path: str = "") -> str:
    """The key a session's shown set is kept under: its id, or when the
    harness sends none (or a placeholder), its transcript path, which the
    hooks and the distill child both see; "" when neither exists."""
    s = real_session_id(session_id)
    if s:
        return s
    if isinstance(transcript_path, str) and transcript_path.strip():
        path = os.path.normpath(transcript_path) if os.path.isabs(transcript_path) else transcript_path
        return "t:" + hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return ""


def _session_file(session_id: str):
    h = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()
    return data_home() / "run" / "sessions" / f"{h}.json"


def injected(session_id: str) -> set[str]:
    """The ids this session has already been shown."""
    if not session_id:
        return set()
    try:
        data = json.loads(read_text(_session_file(session_id)))
    except (OSError, ValueError):
        return set()
    ids = data.get("ids") if isinstance(data, dict) else None
    return {i for i in ids if isinstance(i, str)} if isinstance(ids, list) else set()


def prune(keep: str = "") -> None:
    """Drop session files older than a week; every hook run calls it. The
    calling session's own file is kept and touched: a session idle for a
    week still has to know, at its end, what it was shown."""
    d = data_home() / "run" / "sessions"
    mine = _session_file(keep) if keep else None
    if mine is not None and mine.exists():
        try:
            os.utime(mine)
        except OSError:
            pass
    now = time.time()
    for other in d.glob("*.json") if d.is_dir() else []:
        try:
            if other != mine and now - other.stat().st_mtime > SESSION_TTL:
                other.unlink()
        except OSError:
            pass


def recorded(session_id: str) -> bool:
    """Whether a hook kept a record for this session at all."""
    return bool(session_id) and _session_file(session_id).exists()


def remember(session_id: str, ids: list[str]) -> None:
    """Add `ids` to what this session has seen. Called by every hook run
    that has a session key, with nothing to add too, so the record's
    existence says the hooks could see this session."""
    if not session_id:
        return
    f = _session_file(session_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    seen = injected(session_id) | set(ids)
    atomic_write(f, json.dumps({"ids": sorted(seen)}) + "\n")
    try:
        os.chmod(f, 0o600)
    except OSError:
        pass


def prompt_block(cfg, prompt: str, session_id: str = "") -> tuple[str, list[str]]:
    """The per-prompt block (section 10) and the ids it shows."""
    b, lr = cfg["budget"], cfg["learn"]
    q = learn.content_tokens(prompt)
    if len(q) < b["prompt_min_tokens"]:
        return "", []
    seen = injected(session_id)
    prin = []
    # Every belief that is not retired, hypotheses and stale ones included
    # and marked: the prompt is the evidence that it may apply.
    for rec, p, e in learn.beliefs(cfg):
        if rec.source_id in seen or e["status"] == "retired":
            continue
        if len(q & learn.signature(p)) >= lr["match_min"]:
            prin.append((rec.source_id, belief_line(p, e)))
    hits = []
    if b["prompt_k"] > 0:
        local = cfg["backend"].get("name", "local") == "local"
        # The local backend matches tokens, so it is asked with the prompt's
        # content tokens and must share `match_min` of them; any other
        # backend ranks the prompt text itself.
        query = " ".join(sorted(q)) if local else prompt[:MAX_QUERY_CHARS]
        for ev in writes.recall_backend(cfg).recall(query, cfg.namespace, b["prompt_k"] + len(seen) + 8):
            if ev.source_id in seen or ev.source_id.startswith(NOT_RECALLED):
                continue
            if local and (ev.scores or {}).get("overlap", 0) < lr["match_min"]:
                continue
            v = _view(cfg, ev)
            if v:
                hits.append((ev.source_id, v))
            if len(hits) >= b["prompt_k"]:
                break
    if not prin and not hits:
        return "", []
    head = f"[nimblemind {cfg.namespace} prompt]"
    limit = b["prompt_chars"]
    notes: list[str] = []
    p_lines = [line for _, line in prin]
    shown = list(hits)

    def render():
        return _render(head, [("Beliefs that may apply", "\n".join(p_lines)),
                              ("Related memory", "\n---\n".join(s for _, s in shown))], notes)

    # Related memory goes first, whole items from the end, then beliefs.
    if len(render()) > limit and shown:
        notes.append(f"related memory cut to fit {limit} chars")
        first = shown[0]
        while shown and len(render()) > limit:
            shown = shown[:-1]
        if not shown:
            # Not even one whole item fits: the best match, cut to the room
            # left, rather than nothing.
            shown = [(first[0], "")]
            room = limit - len(render()) - len("Related memory:\n") - 2
            shown = [(first[0], _cut(first[1], room)[0])] if room > 40 else []
    if len(render()) > limit and p_lines:
        notes.append(f"beliefs cut to fit {limit} chars")
        while p_lines and len(render()) > limit:
            p_lines = p_lines[:-1]
    out = render()
    if len(out) > limit or (not p_lines and not shown):
        return "", []
    ids = [sid for sid, line in prin if line in p_lines] + [sid for sid, _ in shown]
    return out, ids
