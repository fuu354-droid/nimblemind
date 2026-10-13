"""Running a model for the distiller and the researcher.

`command = "claude-cli"` runs the Claude Code CLI in print mode with a model
alias (never a dated build, so it follows whatever the alias points at).
`command = "argv"` runs any program that reads a prompt on stdin and prints
its answer: `argv = ["ollama", "run", "llama3"]`, a wrapper script, anything.
`command = "none"` turns the step off.

The child runs in a fresh private directory under the data home, outside
any project and outside the shared system temp directory, with
`NIMBLEMIND_CHILD=1` set, so a child Claude Code session's own hooks see it
and do not distill or recall recursively.
"""

from __future__ import annotations

import json
import re
import os
import subprocess
from dataclasses import dataclass

from nimblemind.fsutil import private_dir, which

CHILD_ENV = "NIMBLEMIND_CHILD"
_SAFE_ARG = re.compile(r"^[A-Za-z0-9._:][A-Za-z0-9._:-]{0,99}$")  # no leading "-"


class ModelError(RuntimeError):
    pass


@dataclass
class Answer:
    text: str
    usage: dict


def is_child() -> bool:
    return bool(os.environ.get(CHILD_ENV))


def run(spec: dict, system: str, user: str, *, tools: list[str] | None = None) -> Answer:
    command = spec.get("command", "claude-cli")
    timeout = float(spec.get("timeout", 300))
    env = dict(os.environ, **{CHILD_ENV: "1"})
    if command == "none":
        raise ModelError("model command is `none`")
    if command == "claude-cli":
        exe = which("claude")
        if not exe:
            raise ModelError("the `claude` CLI is not on PATH; install it or set command = \"argv\"")
        model_name = spec.get("model") or "haiku"
        if not _SAFE_ARG.fullmatch(model_name):
            raise ModelError(f"model name {model_name!r} has characters a command line cannot carry safely")
        # Only fixed, safe tokens go on the command line. The instructions
        # travel on stdin with the transcript: on Windows an npm-installed
        # `claude` is a .cmd shim run through cmd.exe, whose quoting no
        # argv escaping survives, and the instructions carry text a project
        # config may set.
        argv = [exe, "-p", "--model", model_name, "--output-format", "json"]
        if tools:
            argv += ["--allowedTools", ",".join(tools)]
        stdin = system + "\n\n" + user
    elif command == "argv":
        raw_argv = spec.get("argv") or []
        if any("{model}" in a for a in raw_argv) and not _SAFE_ARG.fullmatch(spec.get("model") or ""):
            raise ModelError(f"model name {spec.get('model')!r} has characters a command line "
                             "cannot carry safely")
        argv = [a.replace("{model}", spec.get("model", "")) for a in raw_argv]
        if not argv:
            raise ModelError('command = "argv" needs a non-empty argv list')
        resolved = argv[0] if os.path.isabs(argv[0]) else which(argv[0])
        if not resolved:
            raise ModelError(f"model command {argv[0]!r} is not an absolute path and was not "
                             "found on PATH")
        argv[0] = resolved
        stdin = system + "\n\n" + user
    else:
        raise ModelError(f"unknown model command {command!r}; expected claude-cli, argv or none")
    try:
        with private_dir() as cwd:
            r = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout, env=env,
                               cwd=cwd)
    except subprocess.TimeoutExpired:
        raise ModelError(f"timed out after {timeout:.0f}s") from None
    except OSError as e:
        raise ModelError(f"{type(e).__name__}: {e}") from None
    if command == "argv":
        if r.returncode != 0 or not r.stdout.strip():
            raise ModelError(f"exit {r.returncode}: {r.stderr.strip()[:200]}")
        return Answer(r.stdout.strip(), {})
    try:
        payload = json.loads(r.stdout)
    except ValueError:
        payload = None
    usage = {}
    if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
        usage = {k: v for k, v in payload["usage"].items() if isinstance(v, (int, float))}
    if r.returncode != 0:
        raise ModelError(f"claude -p exit {r.returncode}: {r.stderr.strip()[:200]}")
    if not isinstance(payload, dict):
        raise ModelError(f"claude -p reply is not a JSON object: {r.stdout[:200]!r}")
    result = payload.get("result")
    text = result.strip() if isinstance(result, str) else ""
    if not text:
        raise ModelError("claude -p returned no result text")
    return Answer(text, usage)
