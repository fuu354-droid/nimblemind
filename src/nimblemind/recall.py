"""Session start: one labeled block, inside one budget (DESIGN.md section 9).

Assembled in this order and counted together against `budget.total_chars`:
the source label, core memory (the newest stored journal, capped at
`core_chars`), recall (older records' recall sections, capped at
`recall_chars`), and a truncation note whenever anything was cut. When the
label and core alone exceed the total, recall is dropped first and core is
cut next. Every cut is named in the note; nothing is trimmed silently.

Fail-open: any error means nothing is injected, and session start is never
blocked. Fail-open never reaches the write side: recall only ever reads
stored records, never `pending/` or `held/`.
"""

from __future__ import annotations

from nimblemind import journal, writes
from nimblemind.backends import BackendError

RECALL_QUERY = "unfinished work next steps blockers decisions pitfalls"


def _cut(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", bool(text)
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip(), True


def label(cfg) -> str:
    return f"[nimblemind {cfg.namespace}]"


def latest_journal(cfg):
    """The newest stored journal in this namespace, or None."""
    for rec in writes.canonical(cfg).list(cfg.namespace):
        if rec.kind == "journal":
            return rec
    return None


def older(cfg, exclude: str, k: int) -> list[str]:
    """Up to k recall snippets, never the journal already shown in full."""
    try:
        hits = writes.recall_backend(cfg).recall(RECALL_QUERY, cfg.namespace, k + 1)
    except BackendError:
        return []
    out = []
    for ev in hits:
        if ev.source_id == exclude:
            continue
        view = journal.recall_view(cfg, ev.content) or ev.content
        out.append(f"[{ev.source_id[:100]}] {view.strip()}")
        if len(out) >= k:
            break
    return out


def assemble(cfg) -> str:
    """The block to inject, or "" when there is nothing to say."""
    b = cfg["budget"]
    core_rec = latest_journal(cfg)
    snippets = older(cfg, core_rec.source_id if core_rec else "", b["recall_k"])
    if not core_rec and not snippets:
        return ""

    head = label(cfg)
    notes: list[str] = []
    core, core_cut = _cut(core_rec.content.strip(), b["core_chars"]) if core_rec else ("", False)
    if core_cut:
        notes.append(f"core memory cut to {b['core_chars']} chars")
    recall_text, recall_cut = _cut("\n---\n".join(snippets), b["recall_chars"])
    if recall_cut:
        notes.append(f"recall cut to {b['recall_chars']} chars")

    def render(core: str, recall_text: str, notes: list[str]) -> str:
        parts = [head]
        if core:
            parts.append("Last session journal:\n" + core)
        if recall_text:
            parts.append("Earlier sessions:\n" + recall_text)
        if notes:
            parts.append("[nimblemind: " + "; ".join(notes) + "]")
        return "\n\n".join(parts)

    total = b["total_chars"]
    out = render(core, recall_text, notes)
    if len(out) > total and recall_text:
        notes = [n for n in notes if not n.startswith("recall")] + ["recall dropped to fit the total budget"]
        recall_text = ""
        out = render(core, recall_text, notes)
    if len(out) > total and core:
        # Cut core by exactly the overflow, plus room for the note it adds.
        note = f"core memory cut to fit the total budget of {total} chars"
        notes = [n for n in notes if not n.startswith("core")] + [note]
        overflow = len(render(core, "", notes)) - total
        core, _ = _cut(core, len(core) - overflow)
        out = render(core, "", notes)
    if len(out) > total:
        # Label and notes alone do not fit; inject nothing rather than break
        # the budget.
        return ""
    return out
