#!/usr/bin/env python
"""Export a Claude Code session into a readable, redacted Markdown transcript.

Used to produce `agent-transcripts/`. Redaction is applied to every emitted
line without exception, and the script refuses to write a file that still
matches a known secret pattern - a transcript is exactly the kind of artefact
where a leaked key would go unnoticed.

    python scripts/export_transcript.py <session.jsonl> <output.md>
"""

from __future__ import annotations

import io
import json
import pathlib
import re
import sys

# (pattern, replacement). Ordered: specific credential shapes first, then the
# generic KEY=VALUE catch-all, then personally identifying details.
REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "<REDACTED_API_KEY>"),
    (re.compile(r"gsk_[A-Za-z0-9_\-]{16,}"), "<REDACTED_GROQ_KEY>"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "<REDACTED_GOOGLE_KEY>"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "<REDACTED_GITHUB_TOKEN>"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "<REDACTED_SLACK_TOKEN>"),
    (
        re.compile(
            r"([A-Za-z_]*(?:API_KEY|APIKEY|SECRET|TOKEN|PASSWORD|PASSWD))"
            r"\s*[=:]\s*[\"']?[^\s\"',]{6,}",
            re.IGNORECASE,
        ),
        r"\1=<REDACTED>",
    ),
    (re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]{16,}"), "Bearer <REDACTED>"),
    (
        re.compile(r"postgresql(\+\w+)?://[^:\s]+:[^@\s]+@"),
        r"postgresql\1://<user>:<REDACTED>@",
    ),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<REDACTED_EMAIL>"),
    # Absolute home paths leak the operator's username.
    (re.compile(r"[Cc]:[\\/]{1,2}Users[\\/]{1,2}[A-Za-z0-9._\-]+"), r"C:/Users/<user>"),
    (re.compile(r"/c/Users/[A-Za-z0-9._\-]+"), "/c/Users/<user>"),
    (re.compile(r"/home/[A-Za-z0-9._\-]+"), "/home/<user>"),
]

# Run against the finished file. A hit here fails the export.
LEAK_CHECKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "OpenAI-style key"),
    (re.compile(r"gsk_[A-Za-z0-9]{20,}"), "Groq key"),
    (re.compile(r"AIza[A-Za-z0-9_\-]{20,}"), "Google key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "GitHub token"),
]

MAX_TURN_CHARS = 6000
MAX_TOOLS_PER_TURN = 25


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def flatten(content: object) -> tuple[str, list[str]]:
    """Return (visible text, tool-call summaries) for one message."""
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return "", []

    text_parts: list[str] = []
    tools: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text_parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            name = str(block.get("name", "?"))
            payload = block.get("input") or {}
            hint = ""
            if isinstance(payload, dict):
                for key in ("description", "file_path", "command", "pattern", "url"):
                    if payload.get(key):
                        hint = str(payload[key])
                        break
            tools.append(f"{name}: {hint[:150]}" if hint else name)
    return "\n".join(text_parts), tools


HEADER = """# Agent transcript: building The Lenny Growth Assistant

Exported from the Claude Code session that produced this repository.

**Redacted automatically:** API keys, bearer tokens, database credentials,
email addresses and absolute user paths. Tool *results* are omitted, since they
are largely file contents already present in the repo; tool **calls** are kept
so the sequence of work stays legible.

For the narrative account of what broke and how it was fixed, read
[`README.md`](README.md) in this folder first.

---
"""


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2

    source = pathlib.Path(argv[1])
    target = pathlib.Path(argv[2])
    if not source.is_file():
        print(f"error: {source} not found")
        return 1

    rows: list[dict] = []
    with io.open(source, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    out: list[str] = [HEADER]
    turn = 0

    for row in rows:
        role = row.get("type")
        if role not in {"user", "assistant"}:
            continue
        message = row.get("message") or {}
        text, tools = flatten(message.get("content"))
        text = text.strip()
        if not text and not tools:
            continue
        # Injected context, not a real turn.
        if role == "user" and text.startswith("<system-reminder>"):
            continue

        turn += 1
        out.append(f"\n### {'User' if role == 'user' else 'Assistant'} (turn {turn})\n")

        if text:
            body = redact(text)
            if len(body) > MAX_TURN_CHARS:
                body = body[:MAX_TURN_CHARS] + "\n\n*[truncated]*"
            out.append(body + "\n")

        if tools:
            out.append("<details><summary>Tool calls</summary>\n")
            for tool in tools[:MAX_TOOLS_PER_TURN]:
                out.append(f"- `{redact(tool)}`")
            if len(tools) > MAX_TOOLS_PER_TURN:
                out.append(f"- *(+{len(tools) - MAX_TOOLS_PER_TURN} more)*")
            out.append("\n</details>\n")

    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = "\n".join(out)

    leaks = [name for pattern, name in LEAK_CHECKS if pattern.search(rendered)]
    if leaks:
        print(f"REFUSING TO WRITE - possible secrets survived redaction: {leaks}")
        return 1

    target.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {target} ({target.stat().st_size / 1024:.0f} KB, {turn} turns) - "
        "leak check clean"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
