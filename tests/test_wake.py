"""The wake window in web/js/speech.js, replayed on a virtual clock.

A dropped command never reaches the server, so logs/activity.log cannot show
it: the only place to catch this is the page. The timings here are the shape
of Chrome's real results for his voice (2026-09-24, from the demo recordings):
"Jarvis", a pause, then the command arriving as its own final once he has
finished saying it, which for a long command is well after the 6 s window.

Runs speech.js in headless Chrome with a fake recogniser and a fake
synthesiser. Nothing here opens a microphone: recognition is never started
through Speech.start(), which is the only path to getUserMedia, and permission
prompts are denied outright.

    .venv/bin/python tests/test_wake.py
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jarvis import platforms  # noqa: E402

CHROME = platforms.browser()[0] or ""

# (seconds, kind, text): kind is interim, final, or say (J.A.R.V.I.S. speaks;
# the fake synthesiser takes 3 s).
SCENARIOS = {
    "long command after a pause is sent": ([
        (1.0, "final", "Jarvis"),
        (3.0, "interim", "make the rear"),
        (7.0, "interim", "make the rear bell nozzle a cone"),
        (10.5, "interim", "make the rear bell nozzle a cone instead of a set of tubings"),
        (11.0, "final", "make the rear bell nozzle a cone instead of a set of tubings"),
    ], ["make the rear bell nozzle a cone instead of a set of tubings"]),
    "one breath, however long, is sent": ([
        (1.0, "interim", "Jarvis at the bottom"),
        (9.0, "final", "Jarvis at the bottom of the bell there is a cylinder remove it"),
    ], ["at the bottom of the bell there is a cylinder remove it"]),
    "talk that starts after the window is not": ([
        (1.0, "final", "Jarvis"),
        (8.0, "interim", "anyway so"),
        (10.0, "final", "anyway so what were we saying"),
    ], []),
    "talk with no wake word is not": ([
        (1.0, "interim", "did you see the"),
        (4.0, "final", "did you see the game last night"),
    ], []),
    "an answer to his question needs no name": ([
        (1.0, "final", "Jarvis reshape the nozzle"),
        (5.0, "say", "Which part, the bell or the chamber?"),
        (9.0, "interim", "the bell"),
        (12.0, "final", "the bell the lower one"),
    ], ["reshape the nozzle", "the bell the lower one"]),
    "a statement does not reopen the window": ([
        (1.0, "final", "Jarvis remove the cylinder"),
        (5.0, "say", "Removed the cylinder."),
        (15.0, "interim", "no not that"),
        (16.0, "final", "no not that one"),
    ], ["remove the cylinder"]),
}

PAGE = """<!doctype html><meta charset="utf-8"><body><pre id="out"></pre>
<script>
let now = 0, seq = 0; const q = [];
window.setTimeout = (fn, ms) => { const id = ++seq; q.push({at: now + (ms || 0), fn, id, seq: id}); return id; };
window.clearTimeout = (id) => { const i = q.findIndex(e => e.id === id); if (i >= 0) q.splice(i, 1); };
window.setInterval = () => 0; window.clearInterval = () => {};
window.requestAnimationFrame = () => 0;
Date.now = () => now;
let current = null;
class FakeSR {
  start() { current = this; this.onstart && this.onstart(); }
  stop() { this._end(); }
  abort() { this._end(); }
  _end() { if (current === this) { current = null; this.onend && this.onend(); } }
}
window.webkitSpeechRecognition = FakeSR;
window.SpeechRecognition = undefined;
Object.defineProperty(window, 'speechSynthesis', {value: {
  getVoices: () => [], cancel() {}, onvoiceschanged: null,
  speak(u) { setTimeout(() => u.onstart && u.onstart(), 0); setTimeout(() => u.onend && u.onend(), 3000); },
}});
window.SpeechSynthesisUtterance = function (text) { this.text = text; };
</script>
<script src="SPEECH"></script>
<script>
const SCENARIOS = DATA, results = {};
for (const [name, steps] of Object.entries(SCENARIOS)) {
  now = 0; q.length = 0; current = null;
  const sent = [];
  const sp = new Speech({wake_words: ['jarvis'], command_timeout: 6});
  sp.onCommand = (text) => sent.push(text);
  sp.wantListening = true;
  for (const [t, kind, text] of steps) {
    q.push({at: t * 1000, seq: ++seq, fn: () => {
      if (kind === 'say') { sp.say(text); return; }
      const result = [{transcript: text, confidence: 0.9}];
      result.isFinal = kind === 'final';
      current && current.onresult({resultIndex: 0, results: [result]});
    }});
  }
  sp._arm();
  while (q.length) {
    q.sort((a, b) => a.at - b.at || a.seq - b.seq);
    const e = q.shift(); now = Math.max(now, e.at); e.fn();
    if (now > 60000) break;
  }
  results[name] = sent;
}
document.getElementById('out').textContent = JSON.stringify(results);
</script>
"""


def run() -> dict:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="jarvis-wake-"))
    try:
        return _run(tmp)
    finally:
        # Chrome's helpers can still be writing into the profile for a moment
        # after the browser is gone; a leftover temp folder is not a failure.
        shutil.rmtree(tmp, ignore_errors=True)


def _run(tmp: pathlib.Path) -> dict:
    data = {name: steps for name, (steps, _) in SCENARIOS.items()}
    page = tmp / "wake.html"
    page.write_text(PAGE.replace("SPEECH", (ROOT / "web/js/speech.js").as_uri())
                        .replace("DATA", json.dumps(data)))
    out = tmp / "dom.html"
    with open(out, "w") as sink:
        # --dump-dom prints once the page has loaded, but Chrome does not
        # always exit afterwards; wait for the output, then end it.
        proc = subprocess.Popen(
            [CHROME, "--headless=new", f"--user-data-dir={tmp / 'profile'}",
             "--deny-permission-prompts", "--allow-file-access-from-files",
             "--no-first-run", "--dump-dom", page.as_uri()],
            stdout=sink, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.time() + 60
        while time.time() < deadline and "</pre>" not in out.read_text():
            time.sleep(0.2)
        try:
            os.killpg(proc.pid, signal.SIGKILL)     # the browser and its helpers
        except (OSError, AttributeError):           # AttributeError: Windows
            proc.kill()
        proc.wait(timeout=10)
    text = out.read_text()
    start = text.index('<pre id="out">') + len('<pre id="out">')
    raw = text[start:text.index("</pre>")]
    raw = raw.replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")
    return json.loads(raw)


def main() -> int:
    if not CHROME:
        print("  SKIP  no Chromium-family browser")
        return 0
    got = run()
    failures = []
    for name, (_, want) in SCENARIOS.items():
        ok = got.get(name) == want
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f": sent {got.get(name)}, wanted {want}"))
        if not ok:
            failures.append(name)
    print("\n" + ("ALL PASS" if not failures else f"{len(failures)} FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
