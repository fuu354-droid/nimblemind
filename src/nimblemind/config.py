"""Configuration: defaults, user config, environment, project config
(DESIGN.md section 4).

Layers, lowest first: built-in defaults, the user config file, the project
config found from the session's directory, then environment variables
(`NIMBLEMIND_<SECTION>_<KEY>`). Two exceptions: environment values for the
narrowing sections apply at the user's level, so a project still narrows
them, and `approval` is never read from the environment at all.

A project config arrives with whatever repository a person cloned, so it is
not trusted the way the user's own file is:

- In the narrowing sections (`approval`, `budget`, and the pattern parts of
  `isolation`) it can only make things stricter. The stricter of the user
  value and the project value wins, so a repository cannot grant itself write
  access or a bigger injection.
- Every key that runs a command, loads code, chooses where data is sent or
  which memory pool a session reads (`namespace`, `backend`, the distiller and
  researcher commands, the isolation command, `curiosity.after_run`) applies
  only once the person has trusted that exact file with `nimblemind trust`.
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

from nimblemind import paths
from nimblemind.fsutil import atomic_write, read_text

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
                  "command": [], "project_command": [], "timeout": 60},
    "curiosity": {"confidence_floor": 70, "gaps_file": "", "after_run": []},
}

# Keys a project config may set only once trusted. Dotted paths.
TRUST_REQUIRED = (
    "namespace", "harness", "backend", "distiller", "researcher",
    "isolation.command", "isolation.timeout", "curiosity.gaps_file",
    "curiosity.after_run",
)
NARROWING = ("approval", "budget", "isolation")

# Optional scalar keys that have no default but may still come from the
# environment (DESIGN.md section 4's NIMBLEMIND_<SECTION>_<KEY> applies to
# every documented scalar). The value shows the type to coerce to.
ENV_OPTIONAL = {"backend": {"url": "", "timeout": 30.0, "target": ""}}

# Environment overrides for scalar keys: NIMBLEMIND_<SECTION>_<KEY>, or
# NIMBLEMIND_<KEY> for a top-level one. Applied at the user layer.
ENV_PREFIX = "NIMBLEMIND_"


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

    def approval_source(self, path: str) -> str:
        """Where an `allow` for `path` came from, for the approval log: only
        ever the grants file (see `grants_file`)."""
        return f"grant:{grants_file()}#{path}"

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
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
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
    if isinstance(like, float):
        return float(raw)
    return raw


def _env_layer(defaults: dict, sections: tuple[str, ...] | None) -> dict:
    """Environment overrides for the named top-level keys (all when None).

    `approval` is never read from the environment: an agent can set a
    variable on its own command line, and an approval it can grant itself is
    no approval (DESIGN.md section 7). The only scripted way to `allow` a
    path is a standing grant from `nimblemind approval allow`.
    """
    layer: dict = {}
    for key, value in defaults.items():
        if key == "approval" or (sections is not None and key not in sections):
            continue
        if isinstance(value, dict):
            for sub, subval in {**ENV_OPTIONAL.get(key, {}), **value}.items():
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
            # NIMBLEMIND_HOME and _CONFIG are locations, not config keys.
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
        if k in out["budget"] and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
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


# ---- approval grants -----------------------------------------------------
#
# An `allow` lives only in <data home>/approval.toml, written by
# `nimblemind approval allow <path>` at a terminal. Not in the user config:
# the user config's location is chosen by environment variables
# (NIMBLEMIND_CONFIG, XDG_CONFIG_HOME, HOME) that an agent can set on its
# own command line, so an `allow` read from there is an approval the agent can
# grant itself. The grants file sits in the data home instead, so moving it
# also moves the store: an agent that points the data home elsewhere only
# approves writes into a store no real session reads.

def grants_file() -> Path:
    return paths.data_home() / "approval.toml"


def grants() -> set[str]:
    raw = _load_toml_quiet(grants_file()).get("allow", [])
    return {p for p in raw if p in APPROVAL_PATHS} if isinstance(raw, list) else set()


def set_grant(path: str, allowed: bool) -> set[str]:
    if path not in APPROVAL_PATHS:
        raise ConfigError(f"unknown write path {path!r}; expected one of {APPROVAL_PATHS}")
    current = grants()
    current = current | {path} if allowed else current - {path}
    body = ("# Written by `nimblemind approval`. Each path listed here stores\n"
            "# its writes without asking. Edit only through that command.\n"
            f"allow = [{', '.join(repr(p).replace(chr(39), chr(34)) for p in sorted(current))}]\n")
    atomic_write(grants_file(), body)
    return current


def _load_toml_quiet(path: Path) -> dict:
    try:
        return _load_toml(path)
    except ConfigError:
        return {}


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


def _trust_key(project_file: Path) -> str:
    """The path trust is recorded under: absolute, not resolved, so a file
    reached through a symlink is not the file someone trusted."""
    return str(Path(os.path.abspath(project_file)))


def is_trusted(project_file: Path) -> bool:
    if project_file.is_symlink():
        return False
    try:
        return _trusted().get(_trust_key(project_file)) == _digest(project_file)
    except OSError:
        return False


def trust(project_file: Path, expect: bytes | None = None) -> str:
    """Trust `project_file`. With `expect`, the bytes the person was shown,
    the file must still hold exactly those bytes, and their hash is what is
    recorded: a file swapped while the prompt was open is refused."""
    if project_file.is_symlink():
        raise ValueError(f"{project_file} is a symlink; trust the file it points to in its own project")
    project_file = Path(_trust_key(project_file))
    digest = _digest(project_file)
    if expect is not None:
        shown = hashlib.sha256(expect).hexdigest()
        if shown != digest:
            raise ValueError(f"{project_file} changed after it was shown; not trusted")
    data = _trusted()
    data[str(project_file)] = digest
    atomic_write(_trust_file(), json.dumps(data, indent=2, sort_keys=True) + "\n")
    return digest


def untrust(project_file: Path) -> bool:
    data = _trusted()
    removed = data.pop(_trust_key(project_file), None) is not None
    atomic_write(_trust_file(), json.dumps(data, indent=2, sort_keys=True) + "\n")
    return removed


# ---- load ----------------------------------------------------------------

def load(cwd: Path | str | None = None) -> Config:
    """The effective config for a session working in `cwd`. A relative `cwd`
    is refused rather than treated as "no project": that would quietly drop
    the project's restrictions."""
    if cwd and not Path(cwd).is_absolute():
        raise ConfigError(f"cwd must be an absolute path, got {str(cwd)!r}")
    notes: list[str] = []
    user = _load_toml(paths.user_config())
    scalars = [k for k, v in user.items() if isinstance(DEFAULTS.get(k), dict) and not isinstance(v, dict)]
    if scalars:
        raise ConfigError(f"{paths.user_config()}: {', '.join(scalars)} must be a table")
    user_approval = user.pop("approval", None) or {}
    data = _merge(DEFAULTS, user)
    # The user config can tighten approval (ask, deny) but not grant it.
    explicit: set[str] = set()
    for p, level in (user_approval.items() if isinstance(user_approval, dict) else []):
        if level == "allow":
            notes.append(f"{paths.user_config()}: approval.{p} = \"allow\" ignored; "
                         f"grant it with `nimblemind approval allow {p}`")
            continue
        data["approval"][p] = level
        explicit.add(p)
    # A grant lifts only the default `ask`. A path the user config names
    # explicitly (`ask` or `deny`) is a tightening, and a tightening wins.
    for p in grants():
        if p not in explicit and data["approval"].get(p) == "ask":
            data["approval"][p] = "allow"
    # The narrowing sections' environment values act at the user's level, so
    # a project can still only narrow them; every other key's environment
    # value sits above the project config (section 4's precedence).
    data = _merge(data, _env_layer(DEFAULTS, NARROWING))

    project_file = paths.find_project_config(cwd)
    trusted = False
    project_lowered_total = False
    if project_file:
        # Read once: the bytes that are hashed for trust are the bytes that
        # are parsed, so a file swapped between the two cannot be trusted.
        try:
            raw = project_file.read_bytes()
        except OSError as e:
            raise ConfigError(f"{project_file}: {e}") from None
        try:
            project = tomllib.loads(raw.decode("utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ConfigError(f"{project_file}: {e}") from None
        trusted = (not project_file.is_symlink()
                   and _trusted().get(_trust_key(project_file)) == hashlib.sha256(raw).hexdigest())
        if not trusted:
            dropped = [k for k in TRUST_REQUIRED if _strip(project, k)]
            if dropped:
                notes.append(f"{project_file}: not trusted, ignored {', '.join(dropped)} "
                              f"(run `nimblemind trust {project_file}`)")
        narrowing = {k: project.pop(k) for k in NARROWING if k in project}
        bad = [k for k, v in narrowing.items() if not isinstance(v, dict)]
        if bad:
            raise ConfigError(f"{project_file}: {', '.join(bad)} must be a table")
        # Whatever is left is free to set (journal shape, curiosity floor), or
        # was kept because the file is trusted.
        data = _merge(data, project)
        before = data["budget"].get("total_chars")
        data = _narrow(data, narrowing)
        project_lowered_total = data["budget"].get("total_chars") != before
        if trusted:
            # A trusted file may add an isolation command. It runs in
            # addition to the user's own, never instead of it: a project
            # only ever makes isolation stricter (section 8, every check
            # must pass).
            iso = narrowing.get("isolation") or {}
            if iso.get("command"):
                data["isolation"]["project_command"] = iso["command"]
                if "command" not in data["isolation"]["checks"]:
                    data["isolation"]["checks"].append("command")
            if isinstance(iso.get("timeout"), (int, float)) and iso["timeout"] > 0:
                data["isolation"]["timeout"] = min(data["isolation"]["timeout"], iso["timeout"]) \
                    if data["isolation"].get("timeout") else iso["timeout"]

    data = _merge(data, _env_layer(DEFAULTS, tuple(k for k in DEFAULTS if k not in NARROWING)))
    # A project may lower total_chars alone; the parts it bounds follow it
    # down rather than turning an authorized narrowing into a config error.
    b = data["budget"]
    if project_lowered_total:
        for k in ("core_chars", "recall_chars", "prompt_chars"):
            if isinstance(b.get(k), int) and isinstance(b.get("total_chars"), int):
                b[k] = min(b[k], b["total_chars"])
    _validate(data)
    return Config(data=data, project_file=project_file, project_trusted=trusted, notes=notes)
