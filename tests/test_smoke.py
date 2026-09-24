"""Start the real thing and talk to it: the whole path from a fresh install.

Launches `python -m jarvis.main` with no camera, no browser and no permission
prompts on a spare port, then checks what someone opening it for the first
time depends on: the setup panel's permissions list, a spoken command over
the websocket getting a spoken answer, and a page from another origin being
refused the controls.

    .venv/bin/python tests/test_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import time

import aiohttp

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("JARVIS_SMOKE_PORT", "8441"))
BASE = f"http://127.0.0.1:{PORT}"
FAILURES = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  (' + detail + ')' if detail else ''}")
    if not ok:
        FAILURES.append(name)


async def say(ws, text: str, timeout: float = 30.0) -> str:
    await ws.send_str(json.dumps({"type": "utterance", "text": text, "source": "smoke-test"}))
    end = time.time() + timeout
    while time.time() < end:
        try:
            msg = await ws.receive(timeout=end - time.time())
        except asyncio.TimeoutError:
            break
        if msg.type != aiohttp.WSMsgType.TEXT:
            break
        data = json.loads(msg.data)
        if data.get("type") == "say":
            return str(data.get("text", ""))
    return ""


async def run() -> None:
    async with aiohttp.ClientSession() as s:
        for _ in range(120):
            try:
                async with s.get(f"{BASE}/api/permissions") as r:
                    if r.status == 200:
                        perms = await r.json()
                        break
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(0.5)
        else:
            check("the server comes up", False, "no answer in 60 s")
            return
        check("the server comes up", True)
        ids = [row["id"] for row in perms["rows"]]
        check("the setup panel lists camera, microphone and control",
              {"camera", "microphone", "control"} <= set(ids), f"{perms['platform']}: {ids}")

        async with s.get(f"{BASE}/") as r:
            page = await r.text()
        check("the interface is served", r.status == 200 and "setup.js" in page)

        try:
            await s.ws_connect(f"{BASE}/ws", headers={"Origin": "https://example.com"})
            check("a page from another site cannot connect", False)
        except aiohttp.WSServerHandshakeError as exc:
            check("a page from another site cannot connect", exc.status == 403, str(exc.status))

        async with s.post(f"{BASE}/api/permissions", json={"id": "brain"},
                          headers={"Origin": "https://example.com"}) as r:
            check("nor press the setup buttons", r.status == 403, str(r.status))

        ws = await s.ws_connect(f"{BASE}/ws", headers={"Origin": BASE})
        hello = await ws.receive_json(timeout=10)
        check("the interface is greeted", hello.get("type") == "hello",
              f"{len(hello.get('actions', []))} actions")
        reply = await say(ws, "open CAD")
        check("a command gets a spoken answer", bool(reply), repr(reply))
        reply = await say(ws, "what can you do")
        check("so does a question", bool(reply), repr(reply[:80]))
        await ws.close()


def main() -> int:
    env = dict(os.environ, JARVIS_NO_PROMPTS="1", PYTHONUNBUFFERED="1")
    log = open(ROOT / "logs" / "smoke.log", "w") if (ROOT / "logs").is_dir() else subprocess.DEVNULL
    proc = subprocess.Popen([sys.executable, "-m", "jarvis.main", "--no-vision", "--no-browser",
                             "--port", str(PORT)], cwd=str(ROOT), env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    try:
        asyncio.run(run())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
