# J.A.R.V.I.S.

A local Stark-style assistant for macOS: voice in, voice out, hand and body
tracking, and real control of the machine — all behind an interface built to
look like the one in the films.

```
./jarvis-run                 # everything
./jarvis-run --no-vision     # no camera
./jarvis-run --no-browser    # headless; open http://127.0.0.1:8420/ yourself
```

---

## What it does

**Speech.** Say “Jarvis” and it wakes. Say “Jarvis, open Safari” and it does
both in one breath. It replies aloud in a British voice and stops listening
for exactly as long as it is speaking, so it never transcribes itself.

**Control.** 41 actions across apps, windows, browser tabs, keyboard, mouse,
media, volume, brightness, screenshots, clipboard, Shortcuts and telemetry.
The full list is in the COMMAND INDEX panel, and clicking an entry runs it.

**Gestures.** Hands and body are tracked at ~30 fps. Point to move the cursor,
pinch to click, pinch and move to drag, swipe to switch apps, make a fist to
close a window, hold an open palm to open a radial menu, thumbs up or down to
answer a confirmation.

**Thought.** Anything the local parser does not recognise goes to a language
model, which can call every one of those 41 actions as tools.

---

## First run

### 1. Permissions

Run this first — it checks what is granted and asks macOS for the rest:

```bash
cd ~/JARVIS && ./jarvis-run --permissions
```

**Run it from your own Terminal window, not from an editor or a detached
shell.** macOS attaches a permission grant to whichever app it holds
responsible for the process; a process started detached (`nohup`, `&`) has no
responsible app, so the grant has nowhere to land and control silently no-ops.

macOS will not let any of this work until you grant three things. Open
**System Settings → Privacy & Security**:

| Setting | Grant to | Without it |
|---|---|---|
| **Accessibility** | your terminal (Terminal, iTerm, VS Code…) | keyboard, mouse and window control silently do nothing |
| **Camera** | the same app | no hand or body tracking |
| **Microphone** | **Google Chrome** | no voice |

The microphone belongs to Chrome, not to the terminal, because speech
recognition runs in the HUD page. Chrome asks the first time; say yes.

The HUD's CONTROL chip turns amber when Accessibility is missing, and the
activity log says so explicitly. Accessibility only takes effect after you
restart the terminal app.

### 2. A model (optional)

Without a key, J.A.R.V.I.S. still runs: the local intent parser handles the
common commands. With one, it can answer anything and chain actions together.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."    # put this in ~/.zshrc
./jarvis-run
```

### 3. Say something

Click the page once (Chrome will not open a microphone before you interact
with it), then:

> “Jarvis.” … “open Safari”
> “Jarvis, what's my battery”
> “Jarvis, snap window to the left”
> “Jarvis, search for titanium alloys”

Or type into the box at the bottom right — the text path is identical to the
voice path, which makes it the easiest way to test anything.

---

## Connecting your own AI

`ai.backend` in `jarvis.json` picks the brain. They all use the same action
registry as tools, so switching one out changes only how well and how fast the
open questions get answered — the commands in the intent table are unaffected.

Three of them need no API key. Pick on latency: a spoken question you are
waiting on feels different at one second than at three.

| backend | key | latency | RAM | notes |
|---|---|---|---|---|
| `ollama` | none | 1–4 s | ~6 GB while loaded | local, private, works offline |
| `claude-code` | none | ~3 s | none | borrows your CLI login; much smarter |
| `anthropic` | yes | ~2 s | none | best answers, billed per call |

Two things decide whether a local model is usable, and both are set for you:

- **`reasoning_effort: "none"`.** qwen3 deliberates before every answer. Left
  on, setting the volume took 8.4 s and "take a screenshot" timed out
  outright; off, it is 1.4 s and picks the same tool.
- **`unload_after: 120`.** An 8B model holds ~6 GB resident and ollama keeps
  it for five minutes after each question — `keep_alive` is accepted on the
  OpenAI-compatible endpoint and ignored. On 16 GB also running the camera,
  Chrome and the HUD that is most of the headroom, held for a conversation
  that ended. Reloading costs 2.5 s, so it stays warm while you are talking
  and hands the memory back when you stop.

```jsonc
// Claude (default) — direct API calls, needs ANTHROPIC_API_KEY.
// Without the key this falls through to claude-code rather than going mute.
{ "ai": { "backend": "anthropic", "model": "claude-sonnet-5" } }

// A model on this machine. One-time: `ollama pull qwen3:8b`
{ "ai": { "backend": "ollama", "local_model": "qwen3:8b" } }

// Claude Code — uses your existing CLI login, no API key. The CLI is a
// standalone binary; VS Code does not need to be open.
{ "ai": { "backend": "claude-code" } }

// Any other OpenAI-compatible endpoint — LM Studio, vLLM, a hosted provider
{ "ai": { "backend": "openai", "base_url": "http://localhost:1234/v1",
          "model": "llama3.1", "api_key_env": "OPENAI_API_KEY" } }

// Your own program: JSON in on stdin, reply text out on stdout
{ "ai": { "backend": "command", "command": ["python3", "/path/to/brain.py"] } }

// No model at all; local intent parser only
{ "ai": { "backend": "offline" } }
```

For `command`, stdin is `{"text": "...", "system": "..."}` and stdout is
either bare text or `{"text": "..."}`.

Write a config to edit with `./jarvis-run --write-config`; it lands in
`jarvis.json` and every key in `jarvis/config.py` can be overridden there.

---

## Gestures

Gestures are **disarmed** by default, so a wave at the camera cannot quit an
app. Arm them by holding an open palm to the camera for a second, saying
“gesture control on”, clicking ARM, or pressing `g`.

| Gesture | Does |
|---|---|
| index finger extended | moves the cursor |
| pinch (thumb + index) | click |
| pinch and move | drag |
| both hands pinched, moving apart | zoom in / out |
| open-palm swipe left / right | next / previous app |
| open-palm swipe up / down | Mission Control / desktop |
| fist | nothing — it is a resting shape, see below |
| open palm held still | radial menu — point at a wedge, pinch to choose |
| thumbs up / down | confirm / cancel a pending action |
| peace sign | screenshot |

Three things keep it from misfiring: arming, a stability requirement of four
consecutive frames per pose, and a cooldown between discrete gestures. All of
them are tunable under `gestures` in the config, and every binding is
remappable to any action in the registry.

---

## Safety

Actions that are annoying to undo — `quit_app`, `lock_screen`, `sleep_display`,
`run_shell` — stop and ask. The HUD raises a confirmation panel you can answer
by voice (“yes” / “cancel”), by gesture (thumbs up / down), or by clicking.
Edit `safety.confirm_actions` to add or remove entries.

`run_shell` is disabled outright until you set `safety.allow_shell` to true.

---

## Keys

| Key | Does |
|---|---|
| `/` | focus the command box |
| `Esc` | stop speaking, or dismiss a confirmation |
| `g` | arm / disarm gestures |
| `t` | cycle theme — Stark cyan, Mark 42 gold, combat red |
| `c` | hide the real mouse cursor |
| `h` | hand control drawer |
| `shift+Q` | power down (confirms first) |

---

## How it fits together

```
Chrome page ──microphone, speech recognition, speech synthesis, all visuals
     │  websocket
Python ──┬── intents.py    utterance → action, no model call, ~1 ms
         ├── brain.py      everything else → model, with tool use
         ├── tracker.py    camera → hand/body/face landmarks (worker thread)
         ├── gestures.py   landmarks → gestures → actions
         └── actions.py    the one place an action actually runs
```

Speech lives in the browser because that is the only way to have one object own
both the microphone and the voice, which is what makes suspending recognition
during replies exact rather than approximate.

The action registry is the spine: adding one `@action` in
`jarvis/control/actions.py` gives the language model a new tool, the gesture
bindings a new target, and the HUD a new command-index entry, all at once.

### Layout

```
jarvis/
  config.py            defaults + jarvis.json + env overrides
  bus.py               async pub/sub, thread-safe from the vision worker
  server.py            static files, websocket, MJPEG camera feed
  main.py              orchestration and utterance routing
  control/macos.py     every macOS capability, plain and synchronous
  control/actions.py   the registry and the confirm-gated dispatcher
  vision/tracker.py    MediaPipe hands + pose + face
  vision/gestures.py   arming, stability, hysteresis, swipes, radial menu
  ai/intents.py        the local fast path
  ai/brain.py          anthropic | openai | command | offline
web/                   the HUD: index.html, css/, js/, vendored fonts
tests/                 synthetic gesture tests, websocket round-trip test
```

---

## Tests

```bash
.venv/bin/python tests/test_gestures.py          # no camera needed
./jarvis-run --no-vision --no-browser &          # then, against a live server:
.venv/bin/python tests/test_roundtrip.py 8420
```

`test_gestures.py` builds MediaPipe-shaped hands in software for each pose and
runs them through the real recogniser, so gesture logic can be changed and
checked without ever opening the camera.

---

## Troubleshooting

**Nothing happens when it says it did something.** Accessibility is not
granted, or was granted before the terminal was restarted.

**It talks over itself / hears itself.** Only one HUD tab may be open. Close
duplicates.

**“camera would not open”.** Another app holds it, or Camera permission is
missing for the terminal. `--no-vision` starts without it.

**Speech does nothing.** Chrome only. Safari and Firefox have no usable
`SpeechRecognition`; the typed command box works in any browser.

**Wrong voice.** `speech.voice_hint` is matched against the browser's voice
list. Run `speech.listVoices()` in the page console to see the options; for a
better British voice, download an Enhanced one in System Settings → 
Accessibility → Spoken Content → System Voice → Manage Voices.

---

## Is it actually working?

```bash
curl -s http://127.0.0.1:8420/api/health | python3 -m json.tool
```

Reports, factually: whether the camera is running and has ever seen a hand,
whether the HUD has a live microphone and how many voice commands have been
transcribed, whether gestures are armed and how many have fired, and whether
Accessibility is granted. The `problems` list names what is missing and what
to do about it. Empty means everything is wired up.


---

## Staying open

The HUD is meant to stay up, so it resists being closed:

* closing the window asks first (Chrome's "Leave site?" prompt);
* if it goes away anyway, the backend reopens it after `server.relaunch_after`
  seconds — long enough that a reload is not mistaken for a close;
* after `server.max_relaunches` it gives up rather than fight you.

Three deliberate ways out, none of which are second-guessed:

| Way | How |
|---|---|
| the ⏻ button, top right | confirms, then powers down |
| `shift+Q` | same |
| voice | "shut down", "power down", "goodbye" |
| the terminal | `Ctrl-C`, or `pkill -f jarvis.main` |

"Shut down **Spotify**" still quits Spotify — only a bare "shut down" powers
J.A.R.V.I.S. down.

Set `server.persist_hud` to false to turn all of this off.


---

## The model viewer

Say "show the worm" (or "open astrowilly cad") and the part appears inside the
interface as a hologram, on a grid, and you turn it with your hands. Nothing
else has to be installed or open: STL is read directly, and a `.scad` file is
compiled to STL on the spot, so the part you are turning is the current
source, and a STEP export (what Onshape produces) is converted the first time
it is asked for and cached after that. `m` opens the viewer with a search box;
every STL, SCAD and STEP under the configured roots is indexed by name.

| hand | does |
|---|---|
| open hand, moved | turns the model |
| open hand pulled toward the camera, or fingers closing | zooms in |
| open hand pushed away, or fingers opening | zooms out |
| pinch and move | turns |
| two fingers | slides |
| index out, thumb and finger apart or together | zooms |
| both palms apart or together | zooms |

The camera sits in a small box in the corner while the viewer is up, so the
hand doing the turning stays in view (and in a screen recording). While the
viewer is open an open palm belongs to the model: it neither switches apps
nor opens the radial menu.

Only your hands drive. The hand already being followed keeps control even if
a bigger hand appears, a second person's hand cannot take over until yours has
been out of frame for a second, and a second hand counts as yours only when it
is the other hand, near the first, and about the same size. Commands survive
other people talking: "show the worm, ok so anyway" still shows the worm.

Zoom holds where it is the moment your hand touches the edge of the frame
instead of springing back, and goes in until the camera is at the surface.

## Editing the model live

An OpenSCAD part is a program whose top-level numbers are its dimensions
(`rim_t = 3.5;`), so JARVIS can change them without a CAD application. When a
`.scad` model is on screen a panel lists those numbers with sliders, and the
part is rebuilt with `openscad -D name=value` on OpenSCAD's Manifold engine and
swapped in place, same camera, same scale, so a longer part looks longer.
The glasses frame rebuilds in about 0.1 s, most parts in about a second, the
whole MITE in 8 s. While a slider or a hand is moving only the latest value is
built; a new build stops the one in flight.

| say | does |
|---|---|
| "set rim thickness to 3" | sets a dimension (the spoken name only has to be close) |
| "make the wall thicker", "make it a bit shorter" | 10% either way (a count: one) |
| "increase lens width by 2 mm", "reduce the gap by 20 percent" | by an amount |
| "turn on explode" | switches a true/false setting |
| "adjust the bridge gap" | picks it for the hand: pinch and move up or down |
| "done" | lets go of it |
| "what can I change" | reads out the dimensions |
| "undo" | takes back the last edit (a whole drag is one edit) |
| "reset changes" | back to the file as saved |
| "save" | writes the numbers into the `.scad` |

The file on disk is untouched until "save", which rewrites only the number on
each changed line, keeps the comments, and first copies the original to
`build/scad_backups/`. With no editable model open, "undo" and "save" are
Cmd-Z and Cmd-S for the front app. STEP and STL are finished geometry with no
dimensions in them, so they can be turned and zoomed but not edited.

## CAD mode

Orbit, pan and zoom are what a 3D viewport needs constantly, and they are what
a hand does better than a mouse. Say **"CAD mode"** to enter, make a fist or
say **"exit CAD mode"** to leave.

| Hand | Viewport |
|---|---|
| pinch + move | orbit |
| two fingers + move | pan |
| open palm + move up/down | zoom (one-handed) |
| both hands pinched, moving apart | zoom |
| fist | leave CAD mode |
| say "fit" | zoom to fit, where the app has a shortcut |

Two things make it usable in a real application rather than a demo:

**Movement is relative.** The cursor is never warped to your hand. When a drag
starts, wherever the cursor already is becomes the anchor, and the hand only
supplies deltas. Put the pointer in the viewport once and then forget about it.

**Profiles per application.** Every package orbits with a different button, so
the frontmost app is looked up and the same gesture does the right thing:

| Application | Orbit | Pan |
|---|---|---|
| OpenSCAD | left-drag | right-drag |
| Blender | middle | shift+middle |
| Fusion 360 | shift+middle | middle |
| SolidWorks | middle | ctrl+middle |
| Rhino | right | shift+right |
| Onshape and other browser CAD | left | right |
| anything else | middle | shift+middle |

Sensitivity is `cad.orbit_gain`, `cad.pan_gain` and `cad.zoom_gain` in
`jarvis.json` — pixels of drag per frame-width of hand travel. Add an
application by editing `PROFILES` in `jarvis/vision/cad.py`.

Like every other gesture, this synthesises mouse events, so it does nothing
until Accessibility is granted.

---

## If control never turns on

Ticking your terminal is not enough, and the reason is not obvious.

`.venv/bin/python3` normally symlinks Apple's own `com.apple.python3`, whose
signature reads `Authority=Software Signing`. macOS calls that a *platform
binary* and makes it its **own responsible process**, so it cannot inherit an
Accessibility grant from the terminal that launched it. Nothing you tick in
that list will reach it.

Two steps fix it:

```bash
./fix-control-permission.sh     # re-sign the interpreter (reversible: --undo)
open ~/JARVIS                   # then drag JARVIS.app into Accessibility
```

`JARVIS.app` is a real bundle with its own identifier, so macOS can attribute
it, and it becomes the responsible process for everything it launches. Run the
assistant from it:

```bash
open -a ~/JARVIS/JARVIS.app
```

Prove control at any time:

```bash
./check-control.sh
```

It moves the cursor, reads the position back, restores it, and exercises the
middle-button drag CAD orbit needs. It reports how it was launched, because a
grant given to the bundle does not apply to a plain shell process.


---

## Turning gesture control on and off

Use the **GESTURES** switch in the top bar, the `g` key, or say "gesture
control on" / "off". Holding an open palm to the camera also turns it on.

**No hand shape turns it off.** That is deliberate. Every closed-hand shape is
also a *resting* shape — a chin propped on a fist, a hand lowered out of frame,
a hand caught mid-transition between poses — so a gesture bound to "off"
switches itself off while you sit still. It did exactly that, about a second
after being switched on, and again whenever an arm was lowered.

The switch shows three states:

| | |
|---|---|
| green **ON** | on, and able to move things |
| amber **ON** | on, but macOS control permission is missing, so nothing will move |
| grey **OFF** | off |

Switching it on deliberately keeps it on. Only a palm-hold arm times out, and
only after `gestures.disarm_after_idle` with no hand seen at all.
