"""The isolation check every write passes before anything else (DESIGN.md
section 8).

Three verdicts, kept apart because they need different handling: `pass`,
`refused` (a check read the content and would not let it through) and
`unavailable` (a check could not reach a verdict). A write is stored only on
`pass`; both other verdicts hold it.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from nimblemind.fsutil import is_absolute, private_dir, which

PASS, REFUSED, UNAVAILABLE = "pass", "refused", "unavailable"

# Credential shapes common enough to be worth refusing everywhere. Each is
# anchored on a distinctive prefix so ordinary prose does not trip it.
SECRET_PATTERNS = {
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "AWS access key id": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "Anthropic key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "OpenAI-style key": r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b",
    "Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "bearer token": r"(?i)\bauthorization:\s*bearer\s+[A-Za-z0-9._~+/-]{20,}=*",
}


@dataclass
class Verdict:
    state: str
    report: str = ""

    @property
    def ok(self) -> bool:
        return self.state == PASS


def _secrets(content: str) -> list[str]:
    return [name for name, pat in SECRET_PATTERNS.items() if re.search(pat, content)]


def _patterns(content: str, namespace: str, iso: dict) -> list[str]:
    found = []
    for pat in iso.get("deny", []):
        try:
            if re.search(pat, content):
                found.append(f"deny pattern {pat!r}")
        except re.error as e:
            raise ValueError(f"isolation.deny: bad pattern {pat!r}: {e}") from None
    lowered = content.casefold()
    for other, strings in (iso.get("identity") or {}).items():
        if other == namespace:
            continue
        for s in strings:
            if s and s.casefold() in lowered:
                found.append(f"identity of namespace {other!r}")
                break
    return found


def _command(content: str, iso: dict, argv: list, project: str = "") -> Verdict:
    """Run one isolation command. `{file}` is the content's temp file and
    `{project}` the directory of the project config, so a project-relative
    checker can be named without depending on where the child runs. The
    executable must resolve to an absolute path; anything else is
    `unavailable` and the write is held."""
    if any("{project}" in a for a in argv) and not project:
        return Verdict(UNAVAILABLE, "isolation command uses {project} but no trusted project "
                                    "config applies")
    argv = [a.replace("{project}", project) for a in argv]
    if not argv:
        return Verdict(UNAVAILABLE, "isolation.checks names `command` but isolation.command is empty")
    # The content file and the child's working directory both live in one
    # private directory under the data home, removed afterwards.
    with private_dir() as cwd:
        path = os.path.join(cwd, "content.md")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        cmd = [a.replace("{file}", path) for a in argv]
        if not any("{file}" in a for a in argv):
            cmd.append(path)
        resolved = cmd[0] if is_absolute(cmd[0]) else which(cmd[0])
        if not resolved:
            return Verdict(UNAVAILABLE, f"isolation command {cmd[0]!r} is not an absolute path "
                                        "and was not found on PATH")
        cmd[0] = resolved
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=float(iso.get("timeout", 60)), cwd=cwd)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return Verdict(UNAVAILABLE, f"{type(e).__name__}: {e}")
    if r.returncode == 0:
        return Verdict(PASS)
    if r.returncode == 1:
        # Section 8: stdout is the refusal's report (stderr only if it is empty).
        return Verdict(REFUSED, r.stdout.strip() or r.stderr.strip() or "refused by the isolation command")
    out = (r.stdout + r.stderr).strip()
    return Verdict(UNAVAILABLE, f"isolation command exited {r.returncode}: {out[:500]}")


def check(content: str, namespace: str, cfg) -> Verdict:
    """Run every configured check; the first refusal or failure decides."""
    iso = cfg["isolation"]
    checks = iso.get("checks", [])
    # `{project}` names a repository's own files, which may run only once
    # that repository's config is trusted (section 4); an untrusted one
    # leaves it unset, and a command using it is `unavailable`, so held.
    project = (str(cfg.project_file.parent)
               if getattr(cfg, "project_file", None) and getattr(cfg, "project_trusted", False)
               else "")
    try:
        if "builtin-secrets" in checks:
            hits = _secrets(content)
            if hits:
                return Verdict(REFUSED, "credential shapes found: " + ", ".join(hits))
        if "builtin-patterns" in checks or iso.get("deny") or iso.get("identity"):
            hits = _patterns(content, namespace, iso)
            if hits:
                return Verdict(REFUSED, "; ".join(hits))
    except ValueError as e:
        return Verdict(UNAVAILABLE, str(e))
    if "command" in checks:
        user_cmd, project_cmd = iso.get("command") or [], iso.get("project_command") or []
        if not user_cmd and not iso.get("project_added_command"):
            # The user asked for a command check and set none: that check
            # cannot run, whatever a project adds beside it.
            return Verdict(UNAVAILABLE, "isolation.checks names `command` but isolation.command "
                                        "is empty")
        for argv in (user_cmd, project_cmd):
            if argv:
                v = _command(content, iso, argv, project)
                if not v.ok:
                    return v
    return Verdict(PASS)
