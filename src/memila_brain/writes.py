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


def _anchor_file(cfg) -> Path:
    return cfg.layout().home / "pending.jsonl"


def _anchor(cfg, entry_id: str, namespace: str, digest: str) -> None:
    """Record a queued write's namespace and hash outside its own entry
    file, so editing the entry (content, hash and namespace together) cannot
    pass for what was queued."""
    f = _anchor_file(cfg)
    append_line(f, json.dumps({"id": entry_id, "namespace": namespace, "sha256": digest,
                               "queued_at": now_iso()}), lock=f.with_suffix(".lock"))


def _anchored(cfg, entry_id: str) -> dict | None:
    try:
        lines = read_text(_anchor_file(cfg)).splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("id") == entry_id:
            # A consumed anchor ends the entry: it cannot be replayed under
            # the same id after it was approved or rejected.
            found = None if e.get("consumed") else e
    return found


def _consume(cfg, entry_id: str) -> None:
    f = _anchor_file(cfg)
    append_line(f, json.dumps({"id": entry_id, "consumed": True, "at": now_iso()}),
                lock=f.with_suffix(".lock"))


def _commit(cfg, rec: Record, *, path: str, by: str, rule: str) -> str:
    """Store, then log the approval. In that order: a crash between the two
    leaves a record `audit` reports as unapproved, which is the safe side; the
    other order would leave an approval for something never stored."""
    detail = _store(cfg, rec)
    approval.record(cfg.layout(rec.namespace).approvals, source_id=rec.source_id,
                    namespace=rec.namespace, path=path, sha256=rec.digest,
                    approved_by=by, rule=rule)
    return detail


def write(cfg, path: str, kind: str, content: str, *, source_id: str = "",
          namespace: str = "", metadata: dict | None = None,
          approved_by: str = "", rule: str = "command",
          created_at: str = "") -> WriteResult:
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
    if created_at:
        rec.created_at = created_at
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
        _anchor(cfg, entry_id, namespace, rec.digest)
        _log(cfg, f"pending {path} {namespace} {source_id} -> pending/{entry_id}")
        return WriteResult(PENDING, source_id, "awaiting `memila-brain pending approve`", entry_id)

    if approved_by:
        by = approved_by
    else:
        by, rule = cfg.approval_source(path), "allow"
    detail = _commit(cfg, rec, path=path, by=by, rule=rule)
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
            e["_file"] = f.name
            out.append(e)
    return out


def pending(cfg, namespace: str = "") -> list[dict]:
    return _entries(cfg.layout(namespace).pending)


def held(cfg, namespace: str = "") -> list[dict]:
    return _entries(cfg.layout(namespace).held)


def load_pending(cfg, entry_id: str, namespace: str = "") -> dict:
    """One pending entry, checked against what was queued: its content hash
    and its namespace must match the anchor written when it was queued, and
    its record must belong to the namespace it is being read from."""
    namespace = namespace or cfg.namespace
    f = _entry_file(cfg.layout(namespace).pending, entry_id)
    try:
        entry = json.loads(read_text(f))
        rec = Record.from_dict(entry["record"])
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError(f"no pending entry {entry_id}") from None
    anchor = _anchored(cfg, entry_id)
    if (anchor is None or anchor.get("namespace") != namespace
            or rec.namespace != namespace
            or anchor.get("sha256") != rec.digest or entry.get("sha256") != rec.digest):
        raise ValueError(f"pending entry {entry_id} does not match what was queued; refusing")
    entry["_record"] = rec
    return entry


def approve(cfg, entry_id: str, *, namespace: str = "", by: str = "",
            expect_sha256: str = "") -> WriteResult:
    """Store one pending entry. Re-checks everything that could have changed
    since it was queued: the entry against its anchor, the isolation check,
    and the path's approval level (a path now set to `deny` stays unstored).
    `expect_sha256` is the hash the approver was shown; a mismatch refuses."""
    namespace = namespace or cfg.namespace
    entry = load_pending(cfg, entry_id, namespace)
    rec: Record = entry.pop("_record")
    if expect_sha256 and rec.digest != expect_sha256:
        raise ValueError(f"pending entry {entry_id} changed after it was shown; refusing")
    path = entry.get("path")
    if path not in APPROVAL_PATHS:
        raise ValueError(f"pending entry {entry_id} names an unknown write path {path!r}")
    if approval.level(cfg, path) == "deny":
        raise ValueError(f"approval.{path} is deny; pending entry {entry_id} cannot be stored")
    verdict = isolation.check(rec.content, rec.namespace, cfg)
    if not verdict.ok:
        raise ValueError(f"pending entry {entry_id} no longer passes isolation: {verdict.report}")
    f = _entry_file(cfg.layout(namespace).pending, entry_id)
    by = by or approval.who()
    detail = _commit(cfg, rec, path=path, by=by, rule="command")
    _consume(cfg, entry_id)
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
    _consume(cfg, entry_id)
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
    recs, problems = canonical(cfg).scan(namespace)
    for rec in recs:
        entry = log.get((namespace, rec.source_id))
        if entry is None:
            problems.append(f"{rec.source_id}: stored with no approval entry")
        elif entry.get("sha256") != rec.digest:
            problems.append(f"{rec.source_id}: content differs from what was approved")
    return problems
