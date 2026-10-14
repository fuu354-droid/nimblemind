"""The outward API (DESIGN.md section 10): what another tool calls to use
nimblemind as its memory.

Every write goes through the one write path on `provider`, so isolation and
approval apply exactly as they do to the package's own writes: with the
default `ask`, a provider's write waits in `pending/` for a person.
Results use the shared evidence shape and carry no tenant field.
"""

from __future__ import annotations

from nimblemind import config as config_mod
from nimblemind import learn, research, writes

# Notes only: a journal or an answer carries metadata the learning loop
# reads as evidence (belief checks, sessions, research confidence), and a
# provider under a standing grant must not be able to mint it.
STORE_KINDS = ("note",)


def _cfg(cwd: str | None, namespace: str | None) -> config_mod.Config:
    cfg = config_mod.load(cwd)
    if namespace:
        cfg.data["namespace"] = namespace
    return cfg


def store(content: str, *, kind: str = "note", source_id: str = "", metadata: dict | None = None,
          cwd: str | None = None, namespace: str | None = None) -> dict:
    """Write one record. Returns `{state, source_id, detail, entry_id}`;
    state is `stored`, `pending`, `held` or `rejected`."""
    if kind not in STORE_KINDS:
        raise ValueError(f"kind must be one of {STORE_KINDS}")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("nothing to store")
    cfg = _cfg(cwd, namespace)
    return writes.write(cfg, "provider", kind, content, source_id=source_id,
                        metadata=dict(metadata or {})).__dict__


def recall(query: str, k: int = 5, *, cwd: str | None = None,
           namespace: str | None = None) -> list[dict]:
    cfg = _cfg(cwd, namespace)
    return [e.to_dict() for e in writes.recall_backend(cfg).recall(query, cfg.namespace, k)]


def delete(source_id: str, *, cwd: str | None = None, namespace: str | None = None) -> bool:
    return writes.delete(_cfg(cwd, namespace), source_id)


def gaps_add(question: str, *, impact: int | None = None, cost: int | None = None,
             confidence: int | None = None, cwd: str | None = None,
             namespace: str | None = None) -> dict:
    """Ask a question. It is a `manual` gap, written on path `provider`."""
    if not isinstance(question, str) or not question.strip():
        raise ValueError("a gap needs a question")
    meta: dict = {"signal": "manual", "evidence": []}
    for name, v, lo, hi in (("impact", impact, 1, 5), ("cost", cost, 1, 5),
                            ("confidence", confidence, 0, 100)):
        if v is not None:
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError(f"{name} must be an integer from {lo} to {hi}")
            meta[name] = v
    cfg = _cfg(cwd, namespace)
    taken = {r.source_id.removeprefix("gap:") for r, _ in learn.known_gaps(cfg)}
    gid = learn.new_id("G", taken)
    return writes.write(cfg, "provider", "gap", learn.gap_text(gid, question, "manual"),
                        source_id=f"gap:{gid}", metadata=meta).__dict__


def gaps_list(*, cwd: str | None = None, namespace: str | None = None) -> list[dict]:
    cfg = _cfg(cwd, namespace)
    return [{k: v for k, v in g.items() if k != "_rec"} for g in research.ranked(cfg)]


def beliefs_list(*, cwd: str | None = None, namespace: str | None = None) -> list[dict]:
    """Every belief that is not retired, with status, confidence and counts."""
    return [b for b in learn.report(_cfg(cwd, namespace)) if b["status"] != "retired"]
