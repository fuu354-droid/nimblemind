"""Import journals and answers from an older on-disk layout (DESIGN.md
section 13).

The source directory is read, never changed. A plain `*.md` file is imported
as a stored record when it passes this install's isolation check, with an
approval entry of rule `migrated`; a file the old layout had marked
(`.quarantine.md`, `.unscanned.md`), or one this install's check refuses,
goes to `held/`. Re-running is safe: a source id already stored with the
same content is skipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from nimblemind import approval, isolation, writes
from nimblemind.fsutil import read_text
from nimblemind.records import Record

MARKS = (".quarantine.md", ".unscanned.md")


def _stem(name: str) -> tuple[str, str]:
    """(stem, mark): mark is "" for a plain file, else the old layout's mark."""
    for m in MARKS:
        if name.endswith(m):
            return name[: -len(m)], m
    return name[: -len(".md")], ""


def run(cfg, source: Path, kind: str, *, dry_run: bool = False) -> list[str]:
    """One line per file; the caller prints them."""
    lines: list[str] = []
    if not source.is_dir():
        return [f"FAIL {source}: not a directory"]
    prefix = "session:" if kind == "journal" else "gap:" if kind == "answer" else f"{kind}:"
    local = writes.canonical(cfg)
    who = approval.who()
    for f in sorted(source.glob("*.md")):
        stem, marked = _stem(f.name)
        if not stem:
            lines.append(f"skip {f.name}: no name left after its marks")
            continue
        try:
            content = read_text(f)
            mtime = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        except OSError as e:
            lines.append(f"FAIL {f.name}: {e}")
            continue
        try:
            rec = Record(source_id=prefix + stem, namespace=cfg.namespace, kind=kind,
                         content=content, metadata={"migrated_from": str(f)},
                         created_at=mtime.isoformat(timespec="seconds"))
        except ValueError as e:
            lines.append(f"FAIL {f.name}: {e}")
            continue
        existing = local.get(rec.source_id, cfg.namespace)
        if existing is not None and existing.digest == rec.digest:
            lines.append(f"skip {f.name}: already imported")
            continue
        if dry_run:
            verdict = isolation.Verdict(isolation.REFUSED) if marked \
                else isolation.check(content, cfg.namespace, cfg)
            if not verdict.ok:
                word = "hold"
            elif cfg["approval"].get("cli") == "deny":
                word = "reject (approval.cli is deny)"
            else:
                word = "import"
            lines.append(f"would {word} {f.name} as {rec.source_id}")
            continue
        if marked:
            # Judged and refused (or never judged) by the old layout's own
            # gate: held as it was, never re-judged into storage here.
            verdict = isolation.UNAVAILABLE if marked == ".unscanned.md" else isolation.REFUSED
            entry_id = writes._save_entry(cfg.layout().held, {
                "record": rec.to_dict(), "path": "cli", "sha256": rec.digest,
                "verdict": verdict, "report": f"marked {marked} in the source layout",
                "held_at": rec.created_at,
            })
            writes._log(cfg, f"held migrate {cfg.namespace} {rec.source_id} -> held/{entry_id}: "
                             f"marked {marked}")
            lines.append(f"held {f.name}: marked {marked} in the source layout")
            continue
        # The one write path: isolation, then this path's approval level
        # (`deny` refuses here too), with the person who ran migrate as the
        # approver.
        r = writes.write(cfg, "cli", kind, content, source_id=rec.source_id,
                         metadata=rec.metadata, approved_by=who, rule="migrated",
                         created_at=rec.created_at)
        if r.state == writes.STORED:
            lines.append(f"ok {f.name} -> {rec.source_id}" + (f" ({r.detail})" if r.detail else ""))
        elif r.state == writes.HELD:
            lines.append(f"held {f.name}: {r.detail[:120]}")
        else:
            lines.append(f"FAIL {f.name}: {r.state}: {r.detail}")
    return lines
