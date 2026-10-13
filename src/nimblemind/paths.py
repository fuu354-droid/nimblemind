"""Where things live on disk, resolved per platform (DESIGN.md section 3).

Every location resolves in one order: environment variable, then the
platform default. Nothing here names a user, a machine or a repository.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from pathlib import Path

APP = "nimblemind"
PROJECT_CONFIG = ".nimblemind.toml"
# Longest escaped namespace kept verbatim in a path component. Short enough
# that the deepest file under a namespace stays far below Windows' 260
# character path limit from any ordinary data home.
MAX_COMPONENT = 48


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else None


def data_home() -> Path:
    """The data home: records, pending and held writes, logs, locks.
    Environment variable, then `home` in the user config, then the platform
    default (DESIGN.md section 3)."""
    explicit = _env_path("NIMBLEMIND_HOME")
    if explicit:
        return explicit
    configured = _configured_home()
    if configured:
        return configured
    return default_data_home()


def _configured_home() -> Path | None:
    try:
        raw = tomllib.loads(user_config().read_bytes().decode("utf-8-sig")).get("home")
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    return Path(raw).expanduser() if isinstance(raw, str) and raw.strip() else None


def default_data_home() -> Path:
    if sys.platform == "win32":
        base = _env_path("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return base / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    base = _env_path("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return base / APP


def user_config() -> Path:
    """The user config file. It may not exist; defaults then apply."""
    explicit = _env_path("NIMBLEMIND_CONFIG")
    if explicit:
        return explicit
    if sys.platform == "win32":
        base = _env_path("APPDATA") or Path.home() / "AppData" / "Roaming"
        return base / APP / "config.toml"
    if sys.platform == "darwin":
        return default_data_home() / "config.toml"
    base = _env_path("XDG_CONFIG_HOME") or Path.home() / ".config"
    return base / APP / "config.toml"


def find_project_config(start: Path | str | None) -> Path | None:
    """The nearest `.nimblemind.toml` at or above `start`, or None.

    A relative or missing start finds nothing: resolving it would resolve
    against this process's own directory, which is not the session's.
    """
    if not start:
        return None
    start = Path(start)
    if not start.is_absolute():
        return None
    for d in (start, *start.parents):
        candidate = d / PROJECT_CONFIG
        if candidate.is_file():
            return candidate
    return None


class Layout:
    """The directories under one data home, for one namespace."""

    def __init__(self, home: Path, namespace: str):
        self.home = home
        self.namespace = namespace
        ns = home / "namespaces" / safe_name(namespace)
        self.records = ns / "records"
        self.pending = ns / "pending"
        self.held = ns / "held"
        self.curiosity = home / "curiosity" / safe_name(namespace)
        self.approvals = home / "approvals.jsonl"
        self.logs = home / "logs"
        self.locks = home / "locks"


def safe_name(namespace: str) -> str:
    """A namespace as a single path component, on every filesystem.

    Namespaces are opaque strings (`claude:brain` is a real one), so they are
    escaped rather than refused. Only lowercase ASCII letters, digits, `-` and
    `_` pass through: uppercase is escaped because Windows and macOS compare
    names case-insensitively, so `A` and `a` would share a directory; `.` is
    escaped because Windows drops a trailing one. Every escape is fixed width
    (`%XX`, or `%uXXXXXX` past 0xFF) and `%` is itself escaped, so the mapping
    is injective. The `ns-` prefix keeps a namespace like `con` or `nul` off
    the names Windows reserves.
    """
    if not namespace:
        raise ValueError("empty namespace")
    out = []
    for ch in namespace:
        if ch in "abcdefghijklmnopqrstuvwxyz0123456789-_":
            out.append(ch)
        elif ord(ch) < 256:
            out.append("%%%02X" % ord(ch))
        else:
            out.append("%%u%06X" % ord(ch))
    name = "".join(out)
    if len(name) > MAX_COMPONENT:
        # Long or heavily escaped names keep a readable prefix and end in a
        # hash of the full namespace, so the component stays well inside
        # every filesystem's name limit and Windows path limits. `~` never
        # appears in an escaped name, so a hashed name cannot equal a short
        # one.
        digest = hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:20]
        name = name[:MAX_COMPONENT - 21] + "~" + digest
    return "ns-" + name
