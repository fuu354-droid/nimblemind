"""Session end: transcript to journal, through the one write path.

The hook half detaches a child process and returns at once, so a closing
session never waits on a model. The child condenses the transcript, asks the
distiller, validates the result (one retry with the problems named), and
hands the journal to `writes.write` on the `distill` path, where isolation
and approval decide whether it is stored, queued or held.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone

from nimblemind import harness as harness_mod
from nimblemind import journal, learn, model, writes
from nimblemind.fsutil import append_line, run_dir
from nimblemind.records import now_iso

MAX_TRANSCRIPT_CHARS = 60000
MAX_ATTEMPTS = 2


def _log(cfg, msg: str) -> None:
    append_line(cfg.layout().logs / "distill.log", f"{now_iso()} {msg}")


def spawn(transcript_path: str, session_id: str, cwd: str, harness_name: str = "") -> None:
    """Start the distill child, detached from the hook's process group. The
    hook's effective harness is passed on, so a `--harness` override on the
    hook command reaches the child that reads the transcript."""
    # -P: no current directory on sys.path, and cwd: not the project. A
    # hook runs in the session's repository, and `-m` there would import a
    # `nimblemind/` (or any stdlib name) the repository planted.
    argv = [sys.executable, "-P", "-m", "nimblemind", "distill",
            f"--transcript={os.path.abspath(transcript_path)}", f"--session-id={session_id}",
            f"--cwd={cwd}"]
    if harness_name:
        argv.append(f"--harness={harness_name}")
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True, "cwd": run_dir()}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def source_id_for(session_id: str) -> str:
    """A journal's identity: readable stamp and short session id, made
    unique by microseconds, a hash of the full session id (two ids sharing
    their first eight characters stay apart) and a random suffix."""
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond:06d}Z"
    sid = "".join(c for c in session_id if c.isalnum())[:8] or "unknown"
    full = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    return f"session:{stamp}-{sid}-{full}{secrets.token_hex(2)}"


def _listed_beliefs(cfg, session_id: str = "") -> tuple[list[tuple[str, str]], list[str]]:
    """(B-id, statement) of the beliefs to check, within `learn.check_chars`,
    and the B-ids the session was shown by the hooks. Checked: the beliefs
    that apply, plus any other belief the session was shown, in the order
    session start ranks them. A broken learning state lists none rather than
    losing the journal."""
    from nimblemind import recall

    shown = sorted(x.removeprefix("belief:") for x in recall.injected(session_id)
                   if x.startswith("belief:"))
    try:
        state = learn.load_state(cfg)
        applies = {r.source_id for r, _, _ in learn.applying(cfg, state)}
        rows = [(r, p, e) for r, p, e in learn.beliefs(cfg, state)
                if e["status"] != "retired" and (p["bid"] in shown or r.source_id in applies)]
    except learn.StateError as e:
        _log(cfg, f"belief checks skipped: {e}")
        return [], shown
    rows.sort(key=lambda x: recall._rank(x[2]))
    out, used = [], 0
    for _, p, _e in rows:
        line = len(p["bid"]) + len(p["statement"]) + 4
        if used + line > cfg["learn"]["check_chars"]:
            break
        out.append((p["bid"], p["statement"]))
        used += line
    return out, shown


def distill(cfg, transcript_path: str, session_id: str) -> str:
    """Run one distill. Returns the outcome word it logged."""
    if cfg["distiller"].get("command") == "none":
        _log(cfg, f"skip {session_id}: distiller.command is none")
        return "skip"
    h = harness_mod.load(cfg["harness"])
    convo, turns = h.conversation(transcript_path, MAX_TRANSCRIPT_CHARS)
    if turns < cfg["journal"]["min_turns"]:
        _log(cfg, f"skip {session_id}: only {turns} human turns")
        return "skip"

    from nimblemind import recall

    # The key the hooks kept this session's shown set under (its id, else
    # its transcript); none means what it was shown cannot be known.
    key = recall.session_key_for(session_id, os.path.abspath(transcript_path))
    # A key made from the transcript is only good if the hooks used it too.
    if key.startswith("t:") and not recall.recorded(key):
        key = ""
    listed, shown = _listed_beliefs(cfg, key)
    system = journal.instructions(cfg, listed)
    extra, text = "", ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text = model.run(cfg["distiller"], system, extra + "Transcript:\n" + convo).text
        except model.ModelError as e:
            _log(cfg, f"fail {session_id}: {e}")
            return "fail"
        found = journal.problems(cfg, text, checks=bool(listed))
        if not found:
            break
        _log(cfg, f"retry {session_id} attempt {attempt}: {'; '.join(found)}")
        if attempt == MAX_ATTEMPTS:
            _log(cfg, f"reject {session_id}: still invalid after {MAX_ATTEMPTS} attempts")
            return "reject"
        extra = ("Your previous attempt was rejected. Fix exactly these and change "
                 "nothing else:\n- " + "\n- ".join(found) + "\n\n")

    sid = source_id_for(session_id)
    header = f"# Session journal {sid.removeprefix('session:')}\n\n"
    meta = {"session_id": key or "unknown"}
    if not key:
        meta["shown_unknown"] = True
    if shown:
        meta["beliefs_shown"] = shown
    # Every record the hooks injected: a belief drawn from one of them (a
    # research answer) is not independently restated by this session.
    seen = sorted(x for x in recall.injected(key) if x and len(x) <= 200 and not any(
        c.isspace() for c in x))
    if seen:
        meta["records_shown"] = seen
    if listed:
        meta["beliefs_listed"] = [bid for bid, _ in listed]
        checks = learn.belief_checks(text, {bid for bid, _ in listed})
        if checks:
            meta["belief_checks"] = checks
    result = writes.write(cfg, "distill", "journal", header + text.strip() + "\n",
                          source_id=sid, metadata=meta)
    _log(cfg, f"{result.state} {session_id}: {sid}" + (f" ({result.detail})" if result.detail else ""))
    return result.state


def hook(cfg, payload: dict, harness_name: str = "") -> None:
    """SessionEnd: never blocks, never raises."""
    if model.is_child():
        return
    transcript = payload.get("transcript_path", "")
    if not transcript or not os.path.isfile(transcript):
        return
    spawn(transcript, payload.get("session_id", ""), payload.get("cwd", ""),
          harness_name or cfg["harness"])
