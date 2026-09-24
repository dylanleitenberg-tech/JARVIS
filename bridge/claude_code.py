#!/usr/bin/env python3
"""Route J.A.R.V.I.S.'s thinking through the Claude Code CLI.

Set `ai.backend` to "command" and `ai.command` to this file, and the assistant
uses your existing Claude Code login instead of an ANTHROPIC_API_KEY. Nothing
is billed to the API; it goes through whatever plan `claude` is already signed
in with.

Protocol (the `command` backend in jarvis/ai/brain.py):
    stdin   {"text": "...", "system": "...", "tools": [{name, description,
             input_schema}, ...]}
    stdout  {"text": "spoken reply", "actions": [{"name": ..., "args": {...}}]}

The actions come back as data rather than as real tool calls because the CLI
runs its own agent loop; J.A.R.V.I.S. executes them itself through the usual
dispatcher, so the confirmation gate still applies.

Each invocation is a fresh conversation. The CLI takes a few seconds, which is
why the local intent parser handles the common commands first — only open
questions reach this.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys

TIMEOUT = float(os.environ.get("JARVIS_CLAUDE_TIMEOUT", "45"))
MODEL = os.environ.get("JARVIS_CLAUDE_MODEL", "sonnet")

PROTOCOL = """
You are the reasoning behind a voice assistant. You are NOT operating a
terminal and you have no files to read; answer from what you know.

Reply with a single JSON object and nothing else — no prose, no code fence:

  {"text": "<what to say aloud>", "actions": [{"name": "<action>", "args": {}}]}

Rules:
- "text" is SPOKEN ALOUD: one or two sentences, no markdown, no lists, no
  emoji. Keep it short.
- "actions" is optional. Include it only when the user asked for something to
  happen on the computer, and only using the action names listed below with
  exactly their documented arguments.
- If you are unsure which action is meant, ask one short question in "text"
  and return no actions.
""".strip()


def find_binary() -> str:
    """Locate the Claude Code CLI; the VS Code extension path moves on update."""
    explicit = os.environ.get("JARVIS_CLAUDE_BIN")
    if explicit and os.path.exists(explicit):
        return explicit
    matches = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")))
    if matches:
        return matches[-1]
    for path in ("/usr/local/bin/claude", os.path.expanduser("~/.claude/local/claude")):
        if os.path.exists(path):
            return path
    from shutil import which
    found = which("claude")
    if found:
        return found
    raise SystemExit("claude CLI not found; set JARVIS_CLAUDE_BIN")


def extract_json(raw: str) -> dict:
    """Pull the JSON object out of the reply, fenced or not."""
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {"text": text[:400]}   # not JSON at all: speak it as-is


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        print(json.dumps({"text": "I could not read that request."}))
        return 0

    utterance = str(payload.get("text", "")).strip()
    tools = payload.get("tools") or []

    def signature(t: dict) -> str:
        # Allowed values matter: without them "direction" came back as
        # "increase" where the tool takes "bigger" or "smaller".
        props = (t.get("input_schema") or {}).get("properties", {})
        parts = []
        for name, spec in props.items():
            enum = spec.get("enum") if isinstance(spec, dict) else None
            kind = spec.get("type", "") if isinstance(spec, dict) else ""
            parts.append(f"{name}: {'|'.join(map(str, enum))}" if enum else f"{name}: {kind}".rstrip(": "))
        return f"  {t['name']}({', '.join(parts)}) — {t['description']}"

    catalogue = "\n".join(signature(t) for t in tools) or "  (none available)"

    # Each call is a fresh CLI conversation, so the last few exchanges ride
    # along: "yes, do it" means nothing without the question before it.
    history = payload.get("history") or []
    recent = "\n".join(f"User: {h.get('user', '')}\nJ.A.R.V.I.S.: {h.get('reply', '')}"
                       for h in history[-6:] if isinstance(h, dict))

    system = "\n\n".join(filter(None, [
        payload.get("system") or "You are J.A.R.V.I.S.",
        PROTOCOL,
        "Available actions:\n" + catalogue,
        ("Conversation so far (most recent last):\n" + recent) if recent else "",
    ]))

    # Run detached from whatever project the CLI would otherwise find. Without
    # this it loads the user's CLAUDE.md, settings and MCP servers: a question
    # as simple as "what is two plus two" came back with "nothing to checkpoint
    # this time", which is another project's instructions speaking through
    # J.A.R.V.I.S. It also removes the MCP servers from the startup cost.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="jarvis-brain-") as sandbox:
        try:
            proc = subprocess.run(
                [find_binary(), "-p", utterance,
                 "--system-prompt", system,
                 "--output-format", "text",
                 "--model", MODEL,
                 "--setting-sources", "",
                 "--strict-mcp-config",
                 "--allowed-tools", ""],
                capture_output=True, text=True, timeout=TIMEOUT, cwd=sandbox,
            )
        except subprocess.TimeoutExpired:
            print(json.dumps({"text": "That took too long, Sir. I gave up on it."}))
            return 0
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        print(json.dumps({"text": "My link to Claude Code failed.",
                          "error": detail[-1] if detail else "unknown"}))
        return 0

    result = extract_json(proc.stdout)
    out = {"text": str(result.get("text") or "Done.")}
    actions = result.get("actions")
    if isinstance(actions, list) and actions:
        allowed = {t["name"] for t in tools}
        out["actions"] = [
            {"name": a["name"], "args": a.get("args") or {}}
            for a in actions
            if isinstance(a, dict) and a.get("name") in allowed
        ]
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
