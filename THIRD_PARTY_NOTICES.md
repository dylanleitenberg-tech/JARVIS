# Third-party files bundled with JARVIS

JARVIS itself is under the MIT License (`LICENSE`). These files are included
unmodified from their authors and keep their own licenses:

| Files | Author | License | Text |
|---|---|---|---|
| `web/vendor/three.min.js` | Three.js Authors | MIT | `LICENSES/three.js-MIT.txt` |
| `web/fonts/Orbitron-*.woff2` | The Orbitron Project Authors | SIL Open Font License 1.1 | `LICENSES/OFL-orbitron.txt` |
| `web/fonts/Rajdhani-*.woff2` | Indian Type Foundry | SIL Open Font License 1.1 | `LICENSES/OFL-rajdhani.txt` |
| `web/fonts/ShareTechMono-*.woff2` | Carrois Type Design | SIL Open Font License 1.1 | `LICENSES/OFL-sharetechmono.txt` |

## Downloaded by the installer, not shipped here

The installer fetches these from their own sources; each keeps its own license,
and every one permits use in an MIT-licensed project (checked 2026-09-24
against each package's own listing).

| Package | Used for | License |
|---|---|---|
| aiohttp | local web server | Apache-2.0 and MIT |
| numpy | array math | BSD-3-Clause (and permissive sub-licenses) |
| opencv-python | camera frames | Apache-2.0 (its wheels bundle LGPL libraries such as FFmpeg, used unmodified as shared libraries) |
| mediapipe | hand, body and face tracking, with its bundled models | Apache-2.0 |
| psutil | system gauges | BSD-3-Clause |
| anthropic | Claude API client (your own key) | MIT |
| httpx | HTTP client | BSD-3-Clause |
| pyobjc-framework-Quartz, -Cocoa | macOS input and windows | MIT |
| pyautogui | Windows and Linux input | BSD-3-Clause |
| pillow | screenshots on Windows and Linux | MIT-CMU |
| pycaw | Windows volume | MIT |
| cadquery, cadquery-ocp (optional) | STEP file conversion and editing | Apache-2.0 (OCP wraps Open CASCADE, LGPL-2.1 with an exception) |
| uv | installs Python and the packages | MIT or Apache-2.0 |

## Separate programs JARVIS can call, installed by you

| Program | License | How JARVIS uses it |
|---|---|---|
| Ollama, with a model such as Qwen3 | MIT; Qwen3 is Apache-2.0 | local model over its HTTP API |
| OpenSCAD | GPL-2.0 or later | runs it as a separate command to compile `.scad` files; nothing of it is copied or linked into JARVIS |

Anthropic's Claude API is a service used under Anthropic's own terms with your own key.
