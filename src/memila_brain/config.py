"""Configuration: defaults, user config, environment, project config
(DESIGN.md section 4).

Layers, lowest first: built-in defaults, the user config file, environment
variables, then the project config found from the session's directory.

A project config arrives with whatever repository a person cloned, so it is
not trusted the way the user's own file is:

- In the narrowing sections (`approval`, `budget`, and the pattern parts of
  `isolation`) it can only make things stricter. The stricter of the user
  value and the project value wins, so a repository cannot grant itself write
  access or a bigger injection.
- Every key that runs a command, loads code, chooses where data is sent or
  which memory pool a session reads (`namespace`, `backend`, the distiller and
  researcher commands, the isolation command, `curiosity.after_run`) applies
  only once the person has trusted that exact file with `memila-brain trust`.
  Trust is recorded by content hash, so editing the file, or pulling a
  change to it, drops the trust until it is given again.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from memila_brain import paths
from memila_brain.fsutil import atomic_write, read_text

APPROVAL_PATHS = ("distill", "research", "gap", "provider", "cli")
APPROVAL_LEVELS = ("deny", "ask", "allow")  # strictest first

DEFAULTS: dict = {
    "namespace": "default",
    "harness": "claude-code",
    "backend": {"name": "local"},
    "distiller": {"command": "claude-cli", "model": "haiku", "argv": [], "timeout": 300},
    "researcher": {"command": "claude-cli", "model": "sonnet", "argv": [], "timeout": 900},
    "journal": {
        "sections": ["Decisions", "Rulings", "Pitfalls", "Unfinished",
                     "Lessons learned", "Improvements found"],
        "recall_sections": ["Unfinished", "Pitfalls"],
        "max_words": 500,
        "min_turns": 3,
        "language": "",
        "banned_chars": [],
        "instructions": "",
    },
    "budget": {"total_chars": 6000, "core_chars": 3500, "recall_chars": 2000,
               "prompt_chars": 3000, "recall_k": 3},
    "approval": {p: "ask" for p in APPROVAL_PATHS},
    "isolation": {"checks": ["builtin-secrets"], "deny": [], "identity": {},
                  "command": [], "timeout": 60},
    "curiosity": {"confidence_floor": 70, "gaps_file": "", "after_run": []},
}

# Keys a project config may set only once trusted. Dotted paths.
TRUST_REQUIRED = (
    "namespace", "harness", "backend", "distiller", "researcher",
    "isolation.command", "isolation.timeout", "curiosity.gaps_file",
    "curiosity.after_run",
)
NARROWING = ("approval", "budget", "isolation")

# Environment overrides for scalar keys: MEMILA_BRAIN_<SECTION>_<KEY>, or
# MEMILA_BRAIN_<KEY> for a top-level one. Applied at the user layer.
ENV_PREFIX = "MEMILA_BRAIN_"


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    data: dict
    project_file: Path | None = None
    project_trusted: bool = False
    notes: list[str] = field(default_factory=list)

    def __getitem__(self, key):
        return self.data[key]

    @property
    def namespace(self) -> str:
        return self.data["namespace"]

    def layout(self, namespace: str = "") -> paths.Layout:
        return paths.Layout(paths.data_home(), namespace or self.namespace)


def _load_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError) as e:
        raise ConfigError(f"{path}: {e}") from None


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _coerce(raw: str, like):
    if isinstance(like, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        return int(raw)
    return raw


def _env_layer(defaults: dict) -> dict:
    layer: dict = {}
    for key, value in defaults.items():
        if isinstance(value, dict):
            for sub, subval in value.items():
                if isinstance(subval, (dict, list)):
                    continue
                raw = os.environ.get(f"{ENV_PREFIX}{key}_{sub}".upper())
                if raw is not None:
                    try:
                        layer.setdefault(key, {})[sub] = _coerce(raw, subval)
                    except ValueError:
                        raise ConfigError(f"{ENV_PREFIX}{key}_{sub}".upper()
                                          + f": not a valid value: {raw!r}") from None
        elif not isinstance(value, list):
            raw = os.environ.get(f"{ENV_PREFIX}{key}".upper())
            # MEMILA_BRAIN_HOME and _CONFIG are locations, not config keys.
            if raw is not None and key not in ("home", "config"):
                layer[key] = _coerce(raw, value)
    return layer


def _strip(project: dict, dotted: str) -> bool:
    head, _, rest = dotted.partition(".")
    if head not in project:
        return False
    if not rest:
        del project[head]
        return True
    sub = project[head]
    return isinstance(sub, dict) and _strip(sub, rest)


def _stricter_level(a: str, b: str) -> str:
    return min(a, b, key=APPROVAL_LEVELS.index)


def _narrow(user: dict, project: dict) -> dict:
    """Apply the project's narrowing sections to `user`, stricter wins."""
    out = copy.deepcopy(user)
    for p, level in (project.get("approval") or {}).items():
        if p in out["approval"] and level in APPROVAL_LEVELS:
            out["approval"][p] = _stricter_level(out["approval"][p], level)
    for k, v in (project.get("budget") or {}).items():
        if k in out["budget"] and isinstance(v, int) and not isinstance(v, bool):
            out["budget"][k] = min(out["budget"][k], v)
    iso = project.get("isolation") or {}
    checks = iso.get("checks")
    if isinstance(checks, list):
        out["isolation"]["checks"] = list(dict.fromkeys([*out["isolation"]["checks"], *checks]))
    if isinstance(iso.get("deny"), list):
        out["isolation"]["deny"] = list(dict.fromkeys([*out["isolation"]["deny"], *iso["deny"]]))
    if isinstance(iso.get("identity"), dict):
        merged = copy.deepcopy(out["isolation"]["identity"])
        for ns, strings in iso["identity"].items():
            if isinstance(strings, list):
                merged[ns] = list(dict.fromkeys([*merged.get(ns, []), *strings]))
        out["isolation"]["identity"] = merged
    return out


def _validate(data: dict) -> None:
    for p, level in data["approval"].items():
        if p not in APPROVAL_PATHS:
            raise ConfigError(f"approval.{p}: unknown write path; expected one of {APPROVAL_PATHS}")
        if level not in APPROVAL_LEVELS:
            raise ConfigError(f"approval.{p}: {level!r} is not one of {APPROVAL_LEVELS}")
    b = data["budget"]
    for k, v in b.items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ConfigError(f"budget.{k}: must be a non-negative integer")
    if b["core_chars"] > b["total_chars"]:
        raise ConfigError("budget.core_chars cannot exceed budget.total_chars")
    if b["recall_chars"] > b["total_chars"]:
        raise ConfigError("budget.recall_chars cannot exceed budget.total_chars")
    if b["prompt_chars"] > b["total_chars"]:
        raise ConfigError("budget.prompt_chars cannot exceed budget.total_chars")
    if not isinstance(data["namespace"], str) or not data["namespace"]:
        raise ConfigError("namespace: must be a non-empty string")
    paths.safe_name(data["namespace"])
    known = {"builtin-secrets", "builtin-patterns", "command"}
    for c in data["isolation"]["checks"]:
        if c not in known:
            raise ConfigError(f"isolation.checks: unknown check {c!r}; expected one of {sorted(known)}")


# ---- trust ---------------------------------------------------------------

def _trust_file() -> Path:
    return paths.data_home() / "trusted-projects.json"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _trusted() -> dict:
    try:
        data = json.loads(read_text(_trust_file()))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_trusted(project_file: Path) -> bool:
    try:
        return _trusted().get(str(project_file.resolve())) == _digest(project_file)
    except OSError:
        return False


def trust(project_file: Path) -> str:
    project_file = project_file.resolve()
    digest = _digest(project_file)
    data = _trusted()
    data[str(project_file)] = digest
    atomic_write(_trust_file(), json.dumps(data, indent=2, sort_keys=True) + "\n")
    return digest


def untrust(project_file: Path) -> bool:
    data = _trusted()
    removed = data.pop(str(project_file.resolve()), None) is not None
    atomic_write(_trust_file(), json.dumps(data, indent=2, sort_keys=True) + "\n")
    return removed


# ---- load ----------------------------------------------------------------

def load(cwd: Path | str | None = None) -> Config:
    """The effective config for a session working in `cwd`."""
    notes: list[str] = []
    data = _merge(DEFAULTS, _load_toml(paths.user_config()))
    data = _merge(data, _env_layer(DEFAULTS))

    project_file = paths.find_project_config(cwd)
    trusted = False
    if project_file:
        project = _load_toml(project_file)
        trusted = is_trusted(project_file)
        if not trusted:
            dropped = [k for k in TRUST_REQUIRED if _strip(project, k)]
            if dropped:
                notes.append(f"{project_file}: not trusted, ignored {', '.join(dropped)} "
                              f"(run `memila-brain trust {project_file}`)")
        narrowing = {k: project.pop(k) for k in NARROWING if k in project}
        # Whatever is left is free to set (journal shape, curiosity floor), or
        # was kept because the file is trusted.
        data = _merge(data, project)
        data = _narrow(data, narrowing)
        if trusted:
            # A trusted file may also set the isolation command and timeout,
            # which _narrow does not carry.
            iso = narrowing.get("isolation") or {}
            for k in ("command", "timeout"):
                if k in iso:
                    data["isolation"][k] = iso[k]

    _validate(data)
    return Config(data=data, project_file=project_file, project_trusted=trusted, notes=notes)
