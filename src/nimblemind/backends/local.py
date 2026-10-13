"""The default backend: records as files on disk, no server, no network.

One JSON file per record under `namespaces/<ns>/records/`, plus an
`index.json` holding each record's kind, creation time and token set. The
index is a cache: `rebuild_index` recreates it from the record files, and a
missing or unreadable index is rebuilt rather than trusted.

Recall matches and orders, nothing more: records sharing at least one
token with the query, ordered by how many distinct query tokens they share,
then by recency. An empty query returns the newest records. That is the
floor DESIGN.md section 1 allows; a semantic ranking is what another backend
is for.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from nimblemind import paths
from nimblemind.fsutil import atomic_write, file_lock, read_text
from nimblemind.records import Evidence, Record

_WORD = re.compile(r"\w+", re.UNICODE)


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x3040 <= o <= 0x30FF or 0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF
            or 0xAC00 <= o <= 0xD7AF or 0xF900 <= o <= 0xFAFF)


def tokens(text: str) -> set[str]:
    """Lowercase word tokens; scripts written without spaces contribute each
    character, so a query in Chinese or Japanese can match at all."""
    out: set[str] = set()
    for w in _WORD.findall(text.lower()):
        if any(_is_cjk(c) for c in w):
            out.update(c for c in w if _is_cjk(c))
            rest = "".join(c if not _is_cjk(c) else " " for c in w).split()
            out.update(rest)
        else:
            out.add(w)
    return out


def _when(iso: str) -> float:
    """An ISO timestamp as a number, so second and microsecond precision
    compare correctly; an unparseable one sorts oldest."""
    try:
        return datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return 0.0


class LocalBackend:
    name = "local"

    def __init__(self, home: Path):
        self.home = Path(home)

    # -- layout ------------------------------------------------------------

    def _dir(self, namespace: str) -> Path:
        return paths.Layout(self.home, namespace).records

    def _file(self, namespace: str, source_id: str) -> Path:
        h = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:40]
        return self._dir(namespace) / f"{h}.json"

    def _lock(self, namespace: str) -> Path:
        return self.home / "locks" / f"records-{paths.safe_name(namespace)}.lock"

    def _index_path(self, namespace: str) -> Path:
        return self._dir(namespace) / "index.json"

    # -- index -------------------------------------------------------------

    def _read_index(self, namespace: str, locked: bool = False) -> dict:
        """The index, rebuilt from the record files when it is missing or
        unreadable. A rebuild writes the index, so it runs under the
        namespace lock; `locked` says the caller already holds it (the lock
        is not reentrant)."""
        try:
            data = json.loads(read_text(self._index_path(namespace)))
            if isinstance(data, dict) and isinstance(data.get("records"), dict):
                return data
        except (OSError, ValueError):
            pass
        if locked:
            return self.rebuild_index(namespace)
        with file_lock(self._lock(namespace)):
            return self.rebuild_index(namespace)

    def _write_index(self, namespace: str, index: dict) -> None:
        atomic_write(self._index_path(namespace), json.dumps(index, ensure_ascii=False) + "\n")

    def rebuild_index(self, namespace: str) -> dict:
        index = {"version": 1, "records": {}}
        d = self._dir(namespace)
        if d.is_dir():
            for f in d.glob("*.json"):
                if f.name == "index.json":
                    continue
                rec = self._read_record(f)
                if rec is not None and rec.namespace == namespace:
                    index["records"][rec.source_id] = self._entry(rec, f)
        if d.is_dir():
            self._write_index(namespace, index)
        return index

    @staticmethod
    def _entry(rec: Record, f: Path) -> dict:
        return {"file": f.name, "kind": rec.kind, "created_at": rec.created_at,
                "tokens": sorted(tokens(rec.content))}

    @staticmethod
    def _read_record(f: Path) -> Record | None:
        try:
            return Record.from_dict(json.loads(read_text(f)))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # -- interface ---------------------------------------------------------

    def store(self, rec: Record) -> str:
        f = self._file(rec.namespace, rec.source_id)
        with file_lock(self._lock(rec.namespace)):
            atomic_write(f, json.dumps(rec.to_dict(), ensure_ascii=False, indent=1) + "\n")
            index = self._read_index(rec.namespace, locked=True)
            index["records"][rec.source_id] = self._entry(rec, f)
            self._write_index(rec.namespace, index)
        return rec.source_id

    def get(self, source_id: str, namespace: str) -> Record | None:
        rec = self._read_record(self._file(namespace, source_id))
        if rec is None or rec.namespace != namespace or rec.source_id != source_id:
            return None
        return rec

    def recall(self, query: str, namespace: str, k: int) -> list[Evidence]:
        if k <= 0:
            return []
        index = self._read_index(namespace)["records"]
        q = tokens(query)
        scored = []
        for sid, entry in index.items():
            overlap = len(q & set(entry.get("tokens", []))) if q else 0
            if q and overlap == 0:
                continue
            scored.append((overlap, _when(entry.get("created_at", "")), sid))
        scored.sort(reverse=True)
        out = []
        for overlap, _, sid in scored:
            rec = self.get(sid, namespace)
            if rec is None:
                continue
            out.append(Evidence(source_id=sid, content=rec.content,
                                scores={"overlap": overlap}, adapter=self.name))
            if len(out) >= k:
                break
        return out

    def delete(self, source_id: str, namespace: str) -> bool:
        f = self._file(namespace, source_id)
        with file_lock(self._lock(namespace)):
            existed = f.exists()
            f.unlink(missing_ok=True)
            index = self._read_index(namespace, locked=True)
            existed = index["records"].pop(source_id, None) is not None or existed
            self._write_index(namespace, index)
        return existed

    def scan(self, namespace: str) -> tuple[list[Record], list[str]]:
        """Every record file on disk for this namespace, bypassing the index,
        plus the files that are not a readable record of this namespace.
        What `audit` reads, so a record the index lost is still checked."""
        d = self._dir(namespace)
        recs, bad = [], []
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                if f.name == "index.json":
                    continue
                rec = self._read_record(f)
                if rec is None:
                    bad.append(f"{f.name}: not a readable record")
                elif rec.namespace != namespace or self._file(namespace, rec.source_id) != f:
                    bad.append(f"{f.name}: holds {rec.namespace}/{rec.source_id}, "
                               "which does not belong under this name")
                else:
                    recs.append(rec)
        return recs, bad

    def list(self, namespace: str) -> list[Record]:
        index = self._read_index(namespace)["records"]
        recs = [r for sid in index if (r := self.get(sid, namespace)) is not None]
        recs.sort(key=lambda r: (_when(r.created_at), r.source_id), reverse=True)
        return recs
