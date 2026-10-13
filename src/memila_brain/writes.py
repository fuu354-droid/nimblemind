"""The one write path (DESIGN.md section 6).

Every persistence in the package comes through `write`: isolation check,
then approval, then storage. Nothing else calls a backend's `store`.

Storage has two halves. The canonical copy always goes to the local record
store under the data home, whatever backend is configured, so listing,
auditing and rebuilding never depend on a server. When the configured
backend is not `local`, the record is also sent there, and a failure of
that send is kept in an `unsynced` list for `flush` instead of being lost.

`pending/` (awaiting approval) and `held/` (refused or unscanned) are never
read by recall, on any backend.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from memila_brain import approval, isolation
from memila_brain import backends as backends_mod
from memila_brain.backends.local import LocalBackend
from memila_brain.config import APPROVAL_PATHS
from memila_brain.fsutil import append_line, atomic_write, file_lock, read_text
from memila_brain.records import Record, now_iso, sha256

STORED, PENDING, HELD, REJECTED = "stored", "pending", "held", "rejected"


@dataclass
class WriteResult:
    state: str
    source_id: str
    detail: str = ""
    entry_id: str = ""      # the pending/ or held/ entry, when there is one


def _log(cfg, msg: str) -> None:
    layout = cfg.layout()
    append_line(layout.logs / "write.log", f"{now_iso()} {msg}")


def canonical(cfg) -> LocalBackend:
    return LocalBackend(cfg.layout().home)


def index_backend(cfg):
    """The configured backend when it is not the local one, else None."""
    if cfg["backend"].get("name", "local") == "local":
        return None
    return backends_mod.load(cfg)


def recall_backend(cfg):
    return index_backend(cfg) or canonical(cfg)


def _new_id() -> str:
    return now_iso().replace(":", "").replace("-", "").replace("+0000", "Z") + "-" + secrets.token_hex(4)


def _entry_file(directory: Path, entry_id: str) -> Path:
    if not entry_id or any(c in entry_id for c in "/\\.:"):
        raise ValueError(f"bad entry id {entry_id!r}")
    return directory / f"{entry_id}.json"


def _save_entry(directory: Path, entry: dict) -> str:
    entry_id = _new_id()
    entry["id"] = entry_id
    atomic_write(_entry_file(directory, entry_id), json.dumps(entry, ensure_ascii=False, indent=1) + "\n")
    return entry_id


def _store(cfg, rec: Record) -> str:
    """Canonical copy first, then the index backend. Returns a detail note."""
    canonical(cfg).store(rec)
    idx = index_backend(cfg)
    if idx is None:
        return ""
    try:
        idx.store(rec)
        return ""
    except backends_mod.BackendError as e:
        _mark_unsynced(cfg, rec.source_id, rec.namespace)
        return f"stored locally; {idx.name} send failed ({e}), `memila-brain flush` retries it"


def _unsynced_file(cfg, namespace: str) -> Path:
    return cfg.layout(namespace).records.parent / "unsynced.json"


def _mark_unsynced(cfg, source_id: str, namespace: str) -> None:
    f = _unsynced_file(cfg, namespace)
    with file_lock(f.with_suffix(".lock")):
        try:
            ids = json.loads(read_text(f))
        except (OSError, ValueError):
            ids = []
        if source_id not in ids:
            ids.append(source_id)
        atomic_write(f, json.dumps(ids) + "\n")


def write(cfg, path: str, kind: str, content: str, *, source_id: str = "",
          namespace: str = "", metadata: dict | None = None,
          approved_by: str = "") -> WriteResult:
    """Persist `content` through isolation and approval.

    `path` names the write path whose approval level applies. `approved_by`
    is set only when a person is approving this exact write by running a
    command; it is the one way `ask` resolves to a store in a single call,
    and it is never set by a hook or a background run.
    """
    if path not in APPROVAL_PATHS:
        raise ValueError(f"unknown write path {path!r}")
    namespace = namespace or cfg.namespace
    source_id = source_id or f"{kind}:{_new_id()}"
    rec = Record(source_id=source_id, namespace=namespace, kind=kind,
                 content=content, metadata=dict(metadata or {}))
    layout = cfg.layout(namespace)

    verdict = isolation.check(content, namespace, cfg)
    if not verdict.ok:
        entry_id = _save_entry(layout.held, {
            "record": rec.to_dict(), "path": path, "sha256": rec.digest,
            "verdict": verdict.state, "report": verdict.report, "held_at": now_iso(),
        })
        word = "quarantine" if verdict.state == isolation.REFUSED else "gate unavailable"
        _log(cfg, f"{word} {path} {namespace} {source_id} -> held/{entry_id}: {verdict.report[:200]}")
        return WriteResult(HELD, source_id, f"{verdict.state}: {verdict.report}", entry_id)

    lvl = approval.level(cfg, path)
    if lvl == "deny":
        _log(cfg, f"reject {path} {namespace} {source_id}: approval.{path} = deny")
        return WriteResult(REJECTED, source_id, f"approval.{path} is deny")
    if lvl == "ask" and not approved_by:
        entry_id = _save_entry(layout.pending, {
            "record": rec.to_dict(), "path": path, "sha256": rec.digest,
            "queued_at": now_iso(),
        })
        _log(cfg, f"pending {path} {namespace} {source_id} -> pending/{entry_id}")
        return WriteResult(PENDING, source_id, "awaiting `memila-brain pending approve`", entry_id)

    if approved_by:
        by, rule = approved_by, "command"
    else:
        src = cfg.project_file if cfg.project_file else "config"
        by, rule = f"config:{src}#approval.{path}", "allow"
    approval.record(layout.approvals, source_id=source_id, namespace=namespace, path=path,
                    sha256=rec.digest, approved_by=by, rule=rule)
    detail = _store(cfg, rec)
    _log(cfg, f"ok {path} {namespace} {source_id} ({rule})" + (f"; {detail}" if detail else ""))
    return WriteResult(STORED, source_id, detail)


# ---- pending -------------------------------------------------------------

def _entries(directory: Path) -> list[dict]:
    out = []
    if not directory.is_dir():
        return out
    for f in sorted(directory.glob("*.json")):
        try:
            e = json.loads(read_text(f))
        except (OSError, ValueError):
            continue
        if isinstance(e, dict) and "record" in e:
            out.append(e)
    return out


def pending(cfg, namespace: str = "") -> list[dict]:
    return _entries(cfg.layout(namespace).pending)


def held(cfg, namespace: str = "") -> list[dict]:
    return _entries(cfg.layout(namespace).held)


def approve(cfg, entry_id: str, *, namespace: str = "", by: str = "") -> WriteResult:
    """Store one pending entry, after re-running the isolation check: config
    may have changed since it was queued, and approval never skips the gate."""
    namespace = namespace or cfg.namespace
    layout = cfg.layout(namespace)
    f = _entry_file(layout.pending, entry_id)
    try:
        entry = json.loads(read_text(f))
    except (OSError, ValueError):
        raise ValueError(f"no pending entry {entry_id}") from None
    rec = Record.from_dict(entry["record"])
    if rec.digest != entry.get("sha256"):
        raise ValueError(f"pending entry {entry_id} was modified after it was queued; refusing")
    verdict = isolation.check(rec.content, rec.namespace, cfg)
    if not verdict.ok:
        raise ValueError(f"pending entry {entry_id} no longer passes isolation: {verdict.report}")
    by = by or approval.who()
    approval.record(layout.approvals, source_id=rec.source_id, namespace=rec.namespace,
                    path=entry["path"], sha256=rec.digest, approved_by=by, rule="command")
    detail = _store(cfg, rec)
    f.unlink(missing_ok=True)
    _log(cfg, f"ok {entry['path']} {rec.namespace} {rec.source_id} (approved by {by})")
    return WriteResult(STORED, rec.source_id, detail)


def reject(cfg, entry_id: str, *, namespace: str = "") -> None:
    namespace = namespace or cfg.namespace
    layout = cfg.layout(namespace)
    f = _entry_file(layout.pending, entry_id)
    if not f.exists():
        raise ValueError(f"no pending entry {entry_id}")
    f.unlink()
    _log(cfg, f"reject pending/{entry_id} by {approval.who()}")


# ---- delete, flush, audit ------------------------------------------------

def delete(cfg, source_id: str, namespace: str = "") -> bool:
    namespace = namespace or cfg.namespace
    existed = canonical(cfg).delete(source_id, namespace)
    idx = index_backend(cfg)
    if idx is not None:
        idx.delete(source_id, namespace)
    _log(cfg, f"delete {namespace} {source_id} by {approval.who()}")
    return existed


def flush(cfg, namespace: str = "") -> tuple[int, int]:
    """Resend records the index backend missed. Returns (sent, failed)."""
    namespace = namespace or cfg.namespace
    idx = index_backend(cfg)
    f = _unsynced_file(cfg, namespace)
    if idx is None:
        f.unlink(missing_ok=True)
        return 0, 0
    with file_lock(f.with_suffix(".lock")):
        try:
            ids = json.loads(read_text(f))
        except (OSError, ValueError):
            ids = []
        sent, left = 0, []
        local = canonical(cfg)
        for sid in ids:
            rec = local.get(sid, namespace)
            if rec is None:
                continue
            try:
                idx.store(rec)
                sent += 1
            except backends_mod.BackendError:
                left.append(sid)
        atomic_write(f, json.dumps(left) + "\n")
    return sent, len(left)


def audit(cfg, namespace: str = "") -> list[str]:
    """Problems with stored records against the approval log; empty is clean."""
    namespace = namespace or cfg.namespace
    log = approval.latest(cfg.layout().approvals)
    problems = []
    for rec in canonical(cfg).list(namespace):
        entry = log.get((namespace, rec.source_id))
        if entry is None:
            problems.append(f"{rec.source_id}: stored with no approval entry")
        elif entry.get("sha256") != rec.digest:
            problems.append(f"{rec.source_id}: content differs from what was approved")
    return problems
