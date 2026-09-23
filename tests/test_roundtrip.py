"""End-to-end check over the real websocket, using read-only actions only.

Run against a live server:  .venv/bin/python tests/test_roundtrip.py [port]
"""
import asyncio
import json
import sys

import aiohttp

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8421
CASES = [
    ("system status", {"action": "system_status"}),
    ("what's my battery", {"action": "system_status"}),
    ("hello", {"say": True}),
    ("what can you do", {"say": True}),
]


async def main() -> int:
    failures = []
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(f"http://127.0.0.1:{PORT}/ws") as ws:
            hello = json.loads((await ws.receive()).data)
            assert hello["type"] == "hello", hello
            print(f"hello: {len(hello['actions'])} actions, "
                  f"ai={hello['ai']['backend']}, accessibility={hello['accessibility']}")

            for text, expect in CASES:
                await ws.send_json({"type": "utterance", "text": text, "source": "test"})
                seen = {"action": None, "ok": None, "say": None}
                deadline = asyncio.get_event_loop().time() + 8
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.receive(), timeout=2.0)
                    except asyncio.TimeoutError:
                        break
                    event = json.loads(msg.data)
                    if event["type"] == "action_result":
                        seen["action"] = event["action"]
                        seen["ok"] = event["ok"]
                    elif event["type"] == "say":
                        seen["say"] = event["text"]
                        break

                ok = True
                if "action" in expect:
                    ok = seen["action"] == expect["action"] and seen["ok"]
                if expect.get("say"):
                    ok = ok and bool(seen["say"])
                print(f"  {'PASS' if ok else 'FAIL'}  {text!r:26} -> "
                      f"action={seen['action']} ok={seen['ok']} say={(seen['say'] or '')[:70]!r}")
                if not ok:
                    failures.append(text)

            # Confirmation gating: a dangerous action must not run unasked.
            await ws.send_json({"type": "action", "name": "lock_screen"})
            gated = False
            for _ in range(8):
                try:
                    event = json.loads((await asyncio.wait_for(ws.receive(), timeout=2.0)).data)
                except asyncio.TimeoutError:
                    break
                if event["type"] == "confirm_required" and event["action"] == "lock_screen":
                    gated = True
                    break
            print(f"  {'PASS' if gated else 'FAIL'}  lock_screen gated behind confirmation")
            if not gated:
                failures.append("confirm gate")
            await ws.send_json({"type": "confirm", "accept": False})
            await asyncio.sleep(0.3)

    print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
    return 1 if failures else 0


sys.exit(asyncio.run(main()))
