"""A Hermes-style memory provider on nimblemind's outward API.

This is the integration test DESIGN.md section 10 names, not a dependency:
it shows the shape a memory provider hook takes (save, search, forget) and
proves nimblemind can be its backend without the provider importing anything
but `nimblemind.api`.
"""

from __future__ import annotations

from nimblemind import api


class NimblemindMemoryProvider:
    """save/search/forget, the three calls a Hermes memory provider makes."""

    name = "nimblemind"

    def __init__(self, namespace: str | None = None, cwd: str | None = None):
        self.namespace = namespace
        self.cwd = cwd

    def save(self, text: str, tags: list[str] | None = None) -> dict:
        return api.store(text, kind="note", metadata={"tags": list(tags or [])},
                         cwd=self.cwd, namespace=self.namespace)

    def search(self, query: str, limit: int = 5) -> list[dict]:
        return [{"id": e["source_id"], "text": e["content"], "score": e["scores"]}
                for e in api.recall(query, limit, cwd=self.cwd, namespace=self.namespace)]

    def forget(self, memory_id: str) -> bool:
        """Forget one of this provider's own memories; anything else is the
        person's to delete, so it is refused (False)."""
        try:
            return api.delete(memory_id, cwd=self.cwd, namespace=self.namespace)
        except ValueError:
            return False
