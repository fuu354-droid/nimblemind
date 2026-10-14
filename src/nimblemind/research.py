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
from nimblemind.records import now_iso

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

## Principle
Statement: <one sentence the agent should follow from now on, or none>
Applies when: <the situation>
Not when: <the exceptions, or none>
Revises: <the id of ONE listed principle this replaces, or none>"""


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
    """Every stored gap with its status and priority, highest first, ties
    oldest first. Gaps waiting for approval are not ranked."""
    state = state if state is not None else learn.load_state(cfg)
    out = []
    for rec in learn.stored(cfg, "gap"):
        parsed = learn.parse_gap(rec.content)
        if not parsed:
            continue
        g = learn._gap_entry(state, rec)
        out.append({"source_id": rec.source_id, "question": parsed["question"],
                    "signal": g["signal"], "status": g["status"],
                    "occurrences": len(g["occurrences"]), "attempts": g["attempts"],
                    "priority": round(priority(cfg, rec, g), 3), "created_at": rec.created_at,
                    "_rec": rec})
    out.sort(key=lambda x: (-x["priority"], x["created_at"]))
    return out


def _parse(text: str) -> dict:
    def section(name):
        m = re.search(rf"^## {name}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
        return m.group(1).strip() if m else ""

    conf_text = section("Confidence")
    m = re.search(r"\d{1,3}", conf_text)
    conf = min(int(m.group(0)), 100) if m else 0
    p = section("Principle")
    return {"answer": section("Answer"), "confidence": conf,
            "statement": learn._field(p, "Statement"),
            "applies_when": learn._field(p, "Applies when"),
            "not_when": learn._field(p, "Not when"),
            "revises": learn._field(p, "Revises").strip().strip(".")}


def _evidence(cfg, ids: list[str]) -> str:
    local = writes.canonical(cfg)
    parts, used = [], 0
    for sid in ids[-6:]:
        rec = local.get(sid, cfg.namespace)
        if rec is None:
            continue
        view = "\n".join(f"- {i}" for _, i in learn.items(
            rec.content, cfg["learn"]["pitfall_sections"] + cfg["learn"]["surprise_sections"]
            + cfg["learn"]["lesson_sections"])) or rec.content[:800]
        chunk = f"[{sid}]\n{view}"
        if used + len(chunk) > MAX_EVIDENCE_CHARS:
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(parts)


def _one(cfg, item: dict, active) -> dict:
    rec = item["_rec"]
    gid = rec.source_id.removeprefix("gap:")
    with learn.locked_state(cfg) as state:
        evidence_ids = list(learn._gap_entry(state, rec)["occurrences"])
    user = [f"Question ({gid}): {item['question']}", "", "Evidence:", _evidence(cfg, evidence_ids) or "(none)"]
    if active:
        user += ["", "Active principles:"] + [f"- {p['pid']}: {p['statement']}" for _, p in active]
    spec = cfg["researcher"]
    tools = spec.get("tools") if spec.get("command") == "claude-cli" else None
    result = {"gap": rec.source_id, "answer": "", "confidence": 0, "principle": ""}
    try:
        text = model.run(spec, SYSTEM, "\n".join(user), tools=tools or None).text
        parsed = _parse(text)
    except model.ModelError as e:
        parsed = None
        result["answer"] = f"failed: {e}"
    stored_answer = False
    if parsed and parsed["answer"]:
        content = (f"Untrusted evidence: researched by a model for {gid}; verify before relying on it.\n"
                   f"# Answer to {gid}\nQuestion: {item['question']}\n\n{parsed['answer']}\n")
        r = writes.write(cfg, "research", "answer", content,
                         source_id=f"answer:{gid}-{learn.secrets.token_hex(3)}",
                         metadata={"gap": rec.source_id, "confidence": parsed["confidence"]})
        result.update(answer=r.state + " " + r.source_id, confidence=parsed["confidence"])
        stored_answer = r.state in ("stored", "pending")
        if stored_answer and parsed["statement"] and parsed["statement"].lower() != "none" \
                and parsed["applies_when"]:
            rev = parsed["revises"]
            if rev.lower() in ("", "none") or rev not in {p["pid"] for _, p in active}:
                rev = ""
            taken = {x.removeprefix("principle:") for x in
                     (r2.source_id for r2 in learn.stored(cfg, "principle"))}
            pid = learn.new_id("P", taken)
            pr = writes.write(cfg, "learn", "principle",
                              learn.principle_text(pid, parsed["statement"], parsed["applies_when"],
                                                   parsed["not_when"] or "none"),
                              source_id=f"principle:{pid}",
                              metadata={"evidence": evidence_ids + [r.source_id],
                                        "origin": "research",
                                        "supersedes": f"principle:{rev}" if rev else ""})
            result["principle"] = pr.state + " " + pr.source_id
    with learn.locked_state(cfg) as state:
        g = learn._gap_entry(state, rec)
        floor = cfg["curiosity"]["confidence_floor"]
        if stored_answer:
            g["answers"].append(result["answer"].split(" ", 1)[1])
            g["confidence"] = parsed["confidence"]
        if stored_answer and parsed["confidence"] >= floor:
            g["status"] = "answered"
            g["answered_at"] = now_iso()
        else:
            g["attempts"] += 1
            if g["attempts"] >= cfg["curiosity"]["max_attempts"]:
                g["status"] = "parked"
        result["status"] = g["status"]
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
    """Research the highest priority ready gaps, one run at a time."""
    if cfg["researcher"].get("command") == "none":
        raise model.ModelError("researcher.command is none")
    lock = cfg.layout().locks / f"research-{learn.paths.safe_name(cfg.namespace)}.lock"
    try:
        with file_lock(lock, wait=False):
            with learn.locked_state(cfg) as state:
                items = [i for i in ranked(cfg, state) if i["status"] in READY
                         and i["priority"] >= cfg["curiosity"]["min_priority"]]
            active = learn.active_principles(cfg)
            results = [_one(cfg, i, active) for i in items[:max(max_gaps, 0)]]
    except LockBusy:
        return {"state": "busy", "results": []}
    out = {"state": "ok", "results": results}
    note = _after_run(cfg)
    if note:
        out["after_run"] = note
    learn._log(cfg, f"research {len(results)} gaps" + (f"; {note}" if note else ""))
    return out
