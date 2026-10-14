"""Claude Code: hook JSON on stdin, JSONL transcripts, hookSpecificOutput."""

from __future__ import annotations

import json

from nimblemind.fsutil import logical_cwd
from nimblemind.harness import condense


def _text_blocks(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                        and isinstance(b.get("text"), str))
    return ""


class ClaudeCode:
    name = "claude-code"

    def read_payload(self, raw: str) -> dict:
        """Claude Code always sends a JSON object. Anything else is a failed
        hook call, raised so the hook stays silent rather than injecting
        memory under a config that never saw the session's project. A
        payload without `cwd` uses the process's directory, which Claude
        Code sets to the project."""
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            raise ValueError("hook payload is not a JSON object")
        if "cwd" in data and not isinstance(data["cwd"], str):
            raise ValueError("hook payload cwd is not a string")
        out = {k: data[k] for k in ("session_id", "cwd", "transcript_path", "prompt")
               if isinstance(data.get(k), str)}
        out.setdefault("cwd", logical_cwd())
        return out

    def conversation(self, transcript_path: str, max_chars: int) -> tuple[str, int]:
        parts: list[str] = []
        human = 0
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                msg = d.get("message") if isinstance(d.get("message"), dict) else {}
                if d.get("type") == "user":
                    # Only turns a person typed: tool results, hook output and
                    # harness notices arrive as user turns too.
                    origin = d.get("origin") if isinstance(d.get("origin"), dict) else {}
                    if origin.get("kind", "human") != "human" or d.get("isMeta"):
                        continue
                    content = msg.get("content", "")
                    if isinstance(content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                        continue
                    text = _text_blocks(content).strip()
                    if text:
                        human += 1
                        parts.append(f"[USER] {text}")
                elif d.get("type") == "assistant":
                    text = _text_blocks(msg.get("content", [])).strip()
                    if text:
                        parts.append(f"[ASSISTANT] {text}")
        return condense(parts, max_chars), human

    def session_start_output(self, context: str) -> str:
        return json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                  "additionalContext": context}},
                          ensure_ascii=False)

    def prompt_output(self, context: str) -> str:
        return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                  "additionalContext": context}},
                          ensure_ascii=False)
