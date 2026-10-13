"""Session end: transcript to journal, through the one write path.

The hook half detaches a child process and returns at once, so a closing
session never waits on a model. The child condenses the transcript, asks the
distiller, validates the result (one retry with the problems named), and
hands the journal to `writes.write` on the `distill` path, where isolation
and approval decide whether it is stored, queued or held.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone

from nimblemind import harness as harness_mod
from nimblemind import journal, model, writes
from nimblemind.fsutil import append_line
from nimblemind.records import now_iso

MAX_TRANSCRIPT_CHARS = 60000
MAX_ATTEMPTS = 2


def _log(cfg, msg: str) -> None:
    append_line(cfg.layout().logs / "distill.log", f"{now_iso()} {msg}")


def spawn(transcript_path: str, session_id: str, cwd: str, harness_name: str = "") -> None:
    """Start the distill child, detached from the hook's process group. The
    hook's effective harness is passed on, so a `--harness` override on the
    hook command reaches the child that reads the transcript."""
    argv = [sys.executable, "-m", "nimblemind", "distill",
            "--transcript", transcript_path, "--session-id", session_id, "--cwd", cwd]
    if harness_name:
        argv += ["--harness", harness_name]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                    "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)


def source_id_for(session_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sid = "".join(c for c in session_id if c.isalnum())[:8] or "unknown"
    return f"session:{stamp}-{sid}"


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

    system = journal.instructions(cfg)
    extra, text = "", ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text = model.run(cfg["distiller"], system, extra + "Transcript:\n" + convo).text
        except model.ModelError as e:
            _log(cfg, f"fail {session_id}: {e}")
            return "fail"
        found = journal.problems(cfg, text)
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
    result = writes.write(cfg, "distill", "journal", header + text.strip() + "\n",
                          source_id=sid, metadata={"session_id": session_id})
    _log(cfg, f"{result.state} {session_id}: {sid}" + (f" ({result.detail})" if result.detail else ""))
    return result.state


def hook(cfg, payload: dict, harness_name: str = "") -> None:
    """SessionEnd: never blocks, never raises."""
    if model.is_child():
        return
    transcript = payload.get("transcript_path", "")
    if not transcript or not os.path.isfile(transcript):
        return
    spawn(transcript, payload.get("session_id", "unknown"), payload.get("cwd", ""),
          harness_name or cfg["harness"])
