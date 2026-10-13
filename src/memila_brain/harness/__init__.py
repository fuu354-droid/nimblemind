"""Harness adapters: how a hook payload is read and how output is printed.

An adapter is small on purpose. Adding a harness (Codex, Hermes, an editor
plugin) means adding one class with these four methods.
"""

from __future__ import annotations

from typing import Protocol


class Harness(Protocol):
    name: str

    def read_payload(self, raw: str) -> dict:
        """The hook's stdin, as `{session_id, cwd, transcript_path, prompt}`
        with whatever of those the harness supplies."""

    def conversation(self, transcript_path: str, max_chars: int) -> tuple[str, int]:
        """(condensed conversation, count of turns a person wrote)."""

    def session_start_output(self, context: str) -> str:
        """What to print so `context` is injected at session start."""

    def prompt_output(self, context: str) -> str:
        """What to print so `context` is injected for one prompt."""


def load(name: str) -> Harness:
    if name == "claude-code":
        from memila_brain.harness.claude_code import ClaudeCode

        return ClaudeCode()
    if name == "generic":
        from memila_brain.harness.generic import Generic

        return Generic()
    raise ValueError(f"unknown harness {name!r}; expected claude-code or generic")


def condense(parts: list[str], max_chars: int) -> str:
    """Join turns, keeping the head and the larger tail when over budget:
    the end of a session carries the freshest state."""
    convo = "\n".join(parts)
    if len(convo) <= max_chars:
        return convo
    head = convo[: max_chars // 3]
    tail = convo[-(2 * max_chars // 3):]
    return head + "\n[... middle truncated ...]\n" + tail
