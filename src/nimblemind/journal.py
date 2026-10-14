"""A session journal: the distiller's instructions, its validation, and the
sections recall reads back.

The section list, word cap, language rule and banned characters all come
from config (`[journal]`), so nothing here assumes one workspace's habits.
"""

from __future__ import annotations

import re

# Ideographs, kana and hangul: "CJK" for the language rule below.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")
EMPTY = "- none"


def headers(cfg) -> list[str]:
    return [f"## {s}" for s in cfg["journal"]["sections"]]


CHECKS = "Belief checks"


def instructions(cfg, beliefs: list[tuple[str, str]] | None = None) -> str:
    """The distiller's instructions. `beliefs` is (B-id, statement) for
    every belief that applied to the session (DESIGN.md section 15.7)."""
    j = cfg["journal"]
    lines = [
        "You are a session distiller. Below is a condensed transcript of an AI coding",
        f"session. Distill it into a journal of AT MOST {j['max_words']} words, in markdown,",
        'with exactly these sections (write "- none" under a section with nothing in it,',
        "but keep every header):",
        "",
        *headers(cfg),
        *([f"## {CHECKS}"] if beliefs else []),
        "",
        "Rules:",
        "- Unfinished = concrete next steps a fresh session needs to resume.",
        "- Facts only, no praise, no filler. Never include secrets, tokens or credentials.",
    ]
    if "Surprises" in j["sections"]:
        lines.append("- Surprises = where a result differed from what was expected: a fix that did "
                     "not fix, a check that passed while the thing failed, a cost or limit that "
                     "did not move. State what was expected and what happened.")
    if "Changes" in j["sections"]:
        lines.append("- Changes = what changed in the environment, a tool or library version, a "
                     "dependency or a requirement during the session.")
    if beliefs:
        lines.append(f"- {CHECKS}: one line per belief below, `- <id>: followed`, "
                     "`- <id>: violated` or `- <id>: n/a`, then a colon and the evidence from "
                     "the session. Use violated when the session did what the belief advises "
                     "against or skipped what it requires, even if it was corrected later.")
        lines.append("Beliefs:")
        lines += [f"  {bid}: {statement}" for bid, statement in beliefs]
    if j.get("language"):
        lines.append(f"- Write in {j['language']} only, whatever language the transcript is in.")
    if j.get("banned_chars"):
        shown = " ".join(repr(c) for c in j["banned_chars"])
        lines.append(f"- Never use these characters: {shown}.")
    if j.get("instructions"):
        lines.append(j["instructions"].strip())
    lines.append("- Output ONLY the journal markdown, nothing else.")
    return "\n".join(lines)


def _is_english_rule(language: str) -> bool:
    return language.strip().lower() in ("en", "english")


def problems(cfg, text: str, checks: bool = False) -> list[str]:
    """Everything wrong with a journal that can be named without a model.
    `checks`: beliefs were listed, so their section is required too."""
    j = cfg["journal"]
    out = []
    banned = sorted({c for c in text if c in set(j.get("banned_chars") or [])})
    if banned:
        out.append(f"contains banned characters {banned!r}")
    if j.get("language") and _is_english_rule(j["language"]):
        n = len(_CJK.findall(text))
        if n:
            out.append(f"contains {n} CJK characters; the journal must be in {j['language']} only")
    missing = [h for h in headers(cfg) + ([f"## {CHECKS}"] if checks else [])
               if h not in text.split("\n")]
    if missing:
        out.append("missing required sections: " + ", ".join(missing))
    words = len(text.split())
    if words > j["max_words"] * 1.5:
        out.append(f"{words} words, far over the {j['max_words']} word cap")
    return out


def section(text: str, name: str) -> str:
    """The body under `## name`, up to the next `## ` header."""
    lines = text.split("\n")
    try:
        start = lines.index(f"## {name}") + 1
    except ValueError:
        return ""
    body = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def is_journal(cfg, text: str) -> bool:
    """Whether `text` is shaped like a journal: it carries any configured
    section header."""
    lines = set(text.split("\n"))
    return any(h in lines for h in headers(cfg))


def recall_view(cfg, text: str) -> str:
    """What recall shows of an older journal: its recall sections only."""
    parts = []
    for name in cfg["journal"]["recall_sections"]:
        body = section(text, name)
        if body and body != EMPTY:
            parts.append(f"{name}:\n{body}")
    return "\n".join(parts)
