"""Write approval and its audit log (DESIGN.md section 7).

Each write path has its own level: `deny`, `ask` or `allow`. `ask` is the
default for every path, so a background run is never authorized by being in
the background. Every stored record gets one line in `approvals.jsonl`
naming its content hash, who approved it and under which rule, and `audit`
checks every stored record against that log.
"""

from __future__ import annotations

import getpass
import json
from pathlib import Path

from nimblemind.fsutil import append_line, read_text
from nimblemind.records import now_iso


def level(cfg, path: str) -> str:
    return cfg["approval"].get(path, "ask")


def who() -> str:
    try:
        return getpass.getuser()
    except Exception:  # no login name on some service accounts
        return "unknown"


def record(log: Path, *, source_id: str, namespace: str, path: str, sha256: str,
           approved_by: str, rule: str) -> dict:
    entry = {"source_id": source_id, "namespace": namespace, "path": path,
             "sha256": sha256, "approved_by": approved_by, "approved_at": now_iso(),
             "rule": rule}
    append_line(log, json.dumps(entry, ensure_ascii=False), lock=log.with_suffix(".lock"))
    return entry


def entries(log: Path) -> list[dict]:
    try:
        text = read_text(log)
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def latest(log: Path) -> dict[tuple[str, str], dict]:
    """The newest approval per (namespace, source_id)."""
    out: dict[tuple[str, str], dict] = {}
    for e in entries(log):
        out[(e.get("namespace", ""), e.get("source_id", ""))] = e
    return out
