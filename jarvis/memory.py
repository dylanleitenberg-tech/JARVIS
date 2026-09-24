"""What J.A.R.V.I.S. carries between sessions.

Without this he starts every launch blank. The activity log already recorded
every utterance, route and edit — and nothing ever read it back, so the record
existed purely for a human to grep after something went wrong.

Two kinds of memory, because they decay differently:

  facts     things stated once that stay true — what a part is for, how the
            user wants something done, a correction. Written deliberately,
            by the user saying "remember ..." or by the model deciding
            something is worth keeping. Never expires; can be forgotten.
  episodes  what happened — models opened, edits made, questions asked.
            Written automatically, kept newest-first, and trimmed. This is
            what makes "what was I doing yesterday" answerable.

Both are small, human-readable JSON. A memory you cannot read and correct by
hand is one you cannot trust.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Dict, List, Optional

MAX_FACTS = 300
MAX_EPISODES = 400
# What reaches the system prompt. The whole store would crowd out the request
# itself, and the oldest of it is the least likely to matter.
BRIEF_FACTS = 40
BRIEF_EPISODES = 12


def _stamp(when: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(when))


def _ago(when: float, now: Optional[float] = None) -> str:
    """"yesterday", not "1758691200" — the model reasons about the first."""
    gap = (now or time.time()) - when
    if gap < 3600:
        return f"{max(1, int(gap // 60))} min ago"
    if gap < 86400:
        return f"{int(gap // 3600)} h ago"
    days = int(gap // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"


class Memory:
    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.facts: List[Dict] = []
        self.episodes: List[Dict] = []
        self.load()

    # ------------------------------------------------------------- storage

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(data, dict):
            self.facts = [f for f in data.get("facts", []) if isinstance(f, dict)]
            self.episodes = [e for e in data.get("episodes", []) if isinstance(e, dict)]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"facts": self.facts[-MAX_FACTS:],
                   "episodes": self.episodes[-MAX_EPISODES:]}
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1))
            tmp.replace(self.path)          # never leave a half-written store
        except OSError:
            pass

    # -------------------------------------------------------------- facts

    def remember(self, text: str, source: str = "user") -> str:
        """Keep something stated once. Replaces a near-duplicate rather than
        stacking two versions of the same fact."""
        text = " ".join((text or "").split())
        if not text:
            return ""
        key = _key(text)
        self.facts = [f for f in self.facts if _key(f.get("text", "")) != key]
        self.facts.append({"text": text, "source": source, "at": time.time()})
        self.save()
        return text

    def forget(self, needle: str) -> int:
        """Drop every fact matching a phrase. Returns how many went."""
        needle = (needle or "").strip().lower()
        if not needle:
            return 0
        before = len(self.facts)
        self.facts = [f for f in self.facts
                      if needle not in str(f.get("text", "")).lower()]
        gone = before - len(self.facts)
        if gone:
            self.save()
        return gone

    # ----------------------------------------------------------- episodes

    def note(self, kind: str, summary: str, **detail) -> None:
        """Record something that happened. Cheap and frequent, so it never
        blocks: identical consecutive entries collapse instead of repeating."""
        summary = " ".join((summary or "").split())
        if not summary:
            return
        if self.episodes:
            last = self.episodes[-1]
            if last.get("kind") == kind and last.get("summary") == summary:
                last["at"] = time.time()
                last["count"] = int(last.get("count", 1)) + 1
                return
        entry = {"kind": kind, "summary": summary, "at": time.time()}
        if detail:
            entry["detail"] = {k: v for k, v in detail.items() if v is not None}
        self.episodes.append(entry)
        if len(self.episodes) > MAX_EPISODES:
            self.episodes = self.episodes[-MAX_EPISODES:]

    # -------------------------------------------------------------- recall

    def brief(self, now: Optional[float] = None) -> str:
        """The block that goes into the system prompt. Empty when there is
        nothing worth saying, so a first run reads exactly as it did before."""
        parts = []
        if self.facts:
            lines = [f"- {f['text']}" for f in self.facts[-BRIEF_FACTS:]]
            parts.append("What you have been told and should not need telling "
                         "again:\n" + "\n".join(lines))
        if self.episodes:
            lines = []
            for e in self.episodes[-BRIEF_EPISODES:]:
                times = f" (x{e['count']})" if int(e.get("count", 1)) > 1 else ""
                lines.append(f"- {_ago(e['at'], now)}: {e['summary']}{times}")
            parts.append("Recently, across earlier sessions as well as this "
                         "one:\n" + "\n".join(lines))
        if not parts:
            return ""
        return ("\n\n".join(parts) +
                "\n\nUse this only when it is relevant. Do not recite it, do not "
                "open by summarising it, and never claim to remember something "
                "that is not written above.")

    def last_session_line(self) -> str:
        """One sentence for the greeting: where things were left."""
        work = [e for e in self.episodes if e.get("kind") in ("model", "edit")]
        if not work:
            return ""
        last = work[-1]
        return f"{_ago(last['at'])} you were {last['summary']}"

    def stats(self) -> Dict[str, object]:
        return {"facts": len(self.facts), "episodes": len(self.episodes),
                "since": _stamp(self.episodes[0]["at"]) if self.episodes else ""}


def _key(text: str) -> str:
    """Loose identity for a fact, so restating one replaces it."""
    return "".join(c for c in text.lower() if c.isalnum())[:80]
