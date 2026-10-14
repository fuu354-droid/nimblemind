"""Ranking, research and stopping for gaps (DESIGN.md section 15.4).

What to ask next and when to stop are rules here, never a model's choice:
a gap's priority is its impact, raised by uncertainty, divided by its cost;
research stops on a confident answer or after `max_attempts`, and a parked
gap waits for new evidence before it is tried again.
"""

from __future__ import annotations

import math
import re
import subprocess

from nimblemind import learn, model, writes
from nimblemind.fsutil import LockBusy, file_lock, is_absolute, private_dir, which

READY = ("open", "recurred")
MAX_EVIDENCE_CHARS = 4000

SYSTEM = """You research one open question an AI coding agent's memory raised about its own work.
Use the evidence (excerpts of its session journals) and, where it helps, the web.
Form hypotheses, say how each was checked, and give the one a later session can verify.
Reply in exactly this shape:

## Answer
<the answer: hypotheses, what was checked, conclusion, and a check a later session can run>

## Confidence
<an integer 0-100: how sure you are the conclusion is right>

## Belief
Statement: <one sentence that should be believed from now on, or none>
Applies when: <the situation>
Not when: <the exceptions, or none>
Revises: <the id of ONE listed belief this corrects, or none>"""


def priority(cfg, rec, g: dict) -> float:
    meta = rec.metadata
    signal = g["signal"]
    if signal == "manual":
        weight = meta.get("impact") if isinstance(meta.get("impact"), int) else 2
        cost = meta.get("cost") if isinstance(meta.get("cost"), int) else cfg["curiosity"]["cost"]["manual"]
    else:
        weight = learn.WEIGHT[signal]
        cost = cfg["curiosity"]["cost"][signal]
    conf = g.get("confidence")
    if conf is None:
        conf = meta.get("confidence") if isinstance(meta.get("confidence"), (int, float)) \
            and not isinstance(meta.get("confidence"), bool) else 50
    conf = min(max(float(conf), 0.0), 100.0)
    occ = max(len(g.get("occurrences") or []), 1)
    impact = weight * (1 + math.log2(occ))
    return impact * (0.5 + (100 - conf) / 100) / max(int(cost), 1)


def ranked(cfg, state: dict | None = None) -> list[dict]:
    """Every stored gap with its derived status and priority, highest first,
    ties oldest first. Gaps waiting for approval are not ranked."""
    state = state if state is not None else learn.derive(cfg)
    out = []
    for rec in learn.stored(cfg, "gap"):
        parsed = learn.parse_gap(rec.content)
        if not parsed:
            continue
        g = learn.gap_entry(state, rec)
        out.append({"source_id": rec.source_id, "question": parsed["question"],
                    "signal": g["signal"], "status": g["status"],
                    "occurrences": len(g["occurrences"]), "attempts": g["attempts"],
                    "priority": round(priority(cfg, rec, g), 3), "created_at": rec.created_at,
                    "_rec": rec, "_g": g})
    out.sort(key=lambda x: (-x["priority"], x["created_at"]))
    return out


def _parse(text: str) -> dict:
    m = re.search(r"\d{1,3}", learn.section_of(text, "Confidence"))
    return {"answer": learn.section_of(text, "Answer"),
            "confidence": min(int(m.group(0)), 100) if m else 0,
            "belief": learn.section_of(text, "Belief")}


def _evidence(cfg, ids: list[str]) -> str:
    local = writes.canonical(cfg)
    lr = cfg["learn"]
    parts, used = [], 0
    for sid in ids[-6:]:
        rec = local.get(sid, cfg.namespace)
        if rec is None:
            continue
        view = "\n".join(f"- {i}" for _, i in learn.items(
            rec.content, lr["pitfall_sections"] + lr["surprise_sections"] + lr["lesson_sections"])) \
            or rec.content[:800]
        chunk = f"[{sid}]\n{view}"
        if used + len(chunk) > MAX_EVIDENCE_CHARS:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts)


def _one(cfg, item: dict, active) -> dict:
    """One research attempt. The answer, its confidence and its proposed
    belief are one record on path `research`; what it does to the gap and to
    beliefs is derived from that record by `learn` (section 15.6), so an
    answer a person approves a week later counts the same as one stored now."""
    rec, g = item["_rec"], item["_g"]
    gid = rec.source_id.removeprefix("gap:")
    user = [f"Question ({gid}): {item['question']}", "", "Evidence:",
            _evidence(cfg, list(g["occurrences"])) or "(none)"]
    if active:
        user += ["", "Current beliefs:"] + [f"- {p['bid']} [{e['status']}]: {p['statement']}"
                                            for _, p, e in active]
    spec = cfg["researcher"]
    tools = spec.get("tools") if spec.get("command") == "claude-cli" else None
    result = {"gap": rec.source_id, "answer": "", "confidence": 0}
    try:
        parsed = _parse(model.run(spec, SYSTEM, "\n".join(user), tools=tools or None).text)
    except model.ModelError as e:
        result["answer"] = f"failed: {e}"
        parsed = None
    if not parsed or not parsed["answer"]:
        # A model that failed to answer says nothing about the question: no
        # record, no attempt counted; the gap stays as it was.
        learn._log(cfg, f"research {rec.source_id}: {result['answer'] or 'no answer'}")
        result["answer"] = result["answer"] or "no answer"
        return result
    content = (f"Untrusted evidence: researched by a model for {gid}; verify before relying on it.\n"
               f"# Answer to {gid}\nQuestion: {item['question']}\n\n## Answer\n{parsed['answer']}\n\n"
               f"## Confidence\n{parsed['confidence']}\n"
               + (f"\n## Belief\n{parsed['belief']}\n" if parsed["belief"] else ""))
    r = writes.write(cfg, "research", "answer", content,
                     source_id=f"answer:{gid}-{learn.secrets.token_hex(3)}",
                     metadata={"gap": rec.source_id, "confidence": parsed["confidence"]})
    result.update(answer=f"{r.state} {r.source_id}", confidence=parsed["confidence"])
    return result


def _after_run(cfg) -> str:
    argv = list(cfg["curiosity"].get("after_run") or [])
    if not argv:
        return ""
    exe = argv[0] if is_absolute(argv[0]) else which(argv[0])
    if not exe:
        return f"after_run: {argv[0]!r} not found"
    cwd = str(cfg.project_file.parent) if cfg.project_file and cfg.project_trusted else None
    try:
        with private_dir() as tmp:
            r = subprocess.run([exe, *argv[1:]], cwd=cwd or tmp, capture_output=True, text=True,
                               timeout=cfg["researcher"]["timeout"], stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"after_run: {type(e).__name__}: {e}"
    return f"after_run: exit {r.returncode}"


def run(cfg, max_gaps: int = 3) -> dict:
    """Research the highest priority ready gaps, one run at a time, then fold
    the answers in with a `learn` pass."""
    if cfg["researcher"].get("command") == "none":
        raise model.ModelError("researcher.command is none")
    lock = cfg.layout().locks / f"research-{learn.paths.safe_name(cfg.namespace)}.lock"
    try:
        with file_lock(lock, wait=False):
            state = learn.derive(cfg)
            items = [i for i in ranked(cfg, state) if i["status"] in READY
                     and i["priority"] >= cfg["curiosity"]["min_priority"]]
            active = learn.applying(cfg, state)
            results = [_one(cfg, i, active) for i in items[:max(max_gaps, 0)]]
            learn.run(cfg)
            after = learn.load_state(cfg)
            for res in results:
                g = after["gaps"].get(res["gap"])
                res["status"] = g["status"] if g else "open"
    except LockBusy:
        return {"state": "busy", "results": []}
    out = {"state": "ok", "results": results}
    note = _after_run(cfg)
    if note:
        out["after_run"] = note
    learn._log(cfg, f"research {len(results)} gaps" + (f"; {note}" if note else ""))
    return out
