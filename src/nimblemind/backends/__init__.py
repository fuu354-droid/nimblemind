"""The storage interface and how a backend is chosen (DESIGN.md section 5).

A backend stores, recalls by query, deletes and lists records, always inside
one namespace the caller names. Ranking quality is the backend's own concern:
nothing here re-implements retrieval.
"""

from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

from nimblemind.records import Evidence, Record


class BackendError(RuntimeError):
    """The backend could not do what was asked (unreachable, refused, broken)."""


@runtime_checkable
class Backend(Protocol):
    name: str

    def store(self, rec: Record) -> str: ...

    def recall(self, query: str, namespace: str, k: int) -> list[Evidence]: ...

    def delete(self, source_id: str, namespace: str) -> bool: ...

    def list(self, namespace: str) -> list[Record]: ...


def load(cfg) -> Backend:
    """The backend the config names: `local`, `memila`, or `plugin`."""
    spec = cfg["backend"]
    name = spec.get("name", "local")
    if name == "local":
        from nimblemind.backends.local import LocalBackend

        return LocalBackend(cfg.layout().home)
    if name == "memila":
        from nimblemind.backends.memila import MemilaBackend

        return MemilaBackend.from_config(spec)
    if name == "plugin":
        target = spec.get("target", "")
        module, _, attr = target.partition(":")
        if not module or not attr:
            raise BackendError(f"backend.target must be 'package.module:Class', got {target!r}")
        cls = getattr(importlib.import_module(module), attr)
        backend = cls(**{k: v for k, v in spec.items() if k not in ("name", "target")})
        if not isinstance(backend, Backend):
            raise BackendError(f"{target} does not implement the Backend interface")
        return backend
    raise BackendError(f"unknown backend {name!r}; expected local, memila or plugin")
