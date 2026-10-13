"""The two data shapes every backend speaks (DESIGN.md section 5).

Neither carries a tenant field. Isolation is by namespace, and a namespace
is an opaque string the caller chose.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

KINDS = ("journal", "answer", "note", "gap")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Record:
    source_id: str
    namespace: str
    kind: str
    content: str
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown record kind {self.kind!r}; expected one of {KINDS}")
        if not self.source_id or "/" in self.source_id or "\\" in self.source_id:
            raise ValueError(f"unusable source_id {self.source_id!r}")

    @property
    def digest(self) -> str:
        return sha256(self.content)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Record":
        return cls(source_id=d["source_id"], namespace=d["namespace"], kind=d["kind"],
                   content=d["content"], metadata=dict(d.get("metadata") or {}),
                   created_at=d.get("created_at") or now_iso())


@dataclass
class Evidence:
    """One recalled item, in the shared evidence shape."""

    source_id: str
    content: str
    scores: dict
    adapter: str
    collected_at: str = field(default_factory=now_iso)
    state: str = "ok"

    def to_dict(self) -> dict:
        return asdict(self)
