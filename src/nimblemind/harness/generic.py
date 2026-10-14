"""Any harness that can run a command: JSON or plain text in, plain text out.

The payload may be the same JSON object the Claude Code adapter reads, or
plain text, which is taken as the prompt. A transcript is plain text, one
turn per line, `USER:` or `ASSISTANT:` prefixed where the harness can tell.
"""

from __future__ import annotations

import json

from nimblemind.fsutil import logical_cwd
from nimblemind.harness import condense


class Generic:
    name = "generic"

    def read_payload(self, raw: str) -> dict:
        """The payload, with the process's own directory as `cwd` whenever the
        payload names none: a hook runs in the session's directory, and a
        missing cwd must not mean "no project config"."""
        try:
            data = json.loads(raw) if raw.strip() else {}
        except ValueError:
            data = {"prompt": raw.strip()}
        if not isinstance(data, dict):
            data = {"prompt": raw.strip()}
        if "cwd" in data and not isinstance(data["cwd"], str):
            raise ValueError("hook payload cwd is not a string")
        out = {k: data[k] for k in ("session_id", "cwd", "transcript_path", "prompt")
               if isinstance(data.get(k), str)}
        out.setdefault("cwd", logical_cwd())
        return out

    def conversation(self, transcript_path: str, max_chars: int) -> tuple[str, int]:
        parts, human = [], 0
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                if line.upper().startswith("USER:"):
                    human += 1
                    parts.append("[USER] " + line[5:].strip())
                elif line.upper().startswith("ASSISTANT:"):
                    parts.append("[ASSISTANT] " + line[10:].strip())
                else:
                    parts.append(line)
        return condense(parts, max_chars), human

    def session_start_output(self, context: str) -> str:
        return context

    def prompt_output(self, context: str) -> str:
        return context
