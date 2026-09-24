"""HTTP + websocket server for the HUD.

Serves `web/` at `/`, a JSON websocket at `/ws`, and the annotated camera feed
as MJPEG at `/feed.mjpg`. Every bus event is mirrored to connected HUD clients;
messages arriving from the HUD are handed to the orchestrator.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import weakref
from typing import Any, Awaitable, Callable, Optional, Set

from aiohttp import WSMsgType, web

WEB_ROOT = pathlib.Path(__file__).resolve().parent.parent / "web"

# Events the HUD never needs, kept off the wire so the vision stream stays light.
NOT_MIRRORED = {"log_verbose"}


@web.middleware
async def no_store(request: web.Request, handler):
    """Never let the HUD run a cached copy of itself.

    Chrome caches static assets aggressively, and a stale script against a new
    server is the worst kind of bug to chase: the file on disk is correct, the
    server serves it correctly, and the page still runs last week's code.
    """
    response = await handler(request)
    if request.path.startswith(("/js/", "/css/", "/fonts/")) \
            or request.path in ("/", "/mic-test.html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


class Server:
    def __init__(self, config: dict, bus,
                 on_message: Callable[[dict, web.WebSocketResponse], Awaitable[None]],
                 get_jpeg: Optional[Callable[[], Optional[bytes]]] = None,
                 hello: Optional[Callable[[], dict]] = None,
                 health: Optional[Callable[[], dict]] = None,
                 models=None,
                 edit_output: Optional[Callable[[str], Optional[object]]] = None,
                 permissions=None):
        self.cfg = config["server"]
        self.config = config
        self.bus = bus
        self.on_message = on_message
        self.get_jpeg = get_jpeg
        self.hello = hello
        self.health = health
        self.models = models
        self.edit_output = edit_output
        self.permissions = permissions
        self.clients: Set[web.WebSocketResponse] = set()
        self.app = web.Application(client_max_size=4 * 1024 * 1024,
                                   middlewares=[no_store])
        self._runner: Optional[web.AppRunner] = None
        self._routes()
        bus.on("*", self._mirror)

    def _routes(self) -> None:
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/ws", self.websocket)
        self.app.router.add_get("/feed.mjpg", self.feed)
        self.app.router.add_get("/api/config", self.api_config)
        self.app.router.add_get("/api/health", self.api_health)
        self.app.router.add_get("/api/models", self.api_models)
        self.app.router.add_get("/api/model", self.api_model)
        self.app.router.add_get("/api/model_edit", self.api_model_edit)
        self.app.router.add_get("/api/permissions", self.api_permissions)
        self.app.router.add_post("/api/permissions", self.api_permissions_request)
        self.app.router.add_static("/", WEB_ROOT, show_index=False, follow_symlinks=False)

    # ------------------------------------------------------------ handlers

    async def index(self, request: web.Request) -> web.FileResponse:
        return web.FileResponse(WEB_ROOT / "index.html",
                                headers={"Cache-Control": "no-store"})

    def _same_origin(self, request: web.Request) -> bool:
        """Only this interface may drive the machine.

        A browser lets any web page open a websocket to 127.0.0.1, so without
        this a site in another tab could connect and send "utterances" that
        type, click and open things. Browsers always send Origin on those
        requests and pages cannot forge it; a missing one is a local tool
        (a test, curl), not a page.
        """
        origin = request.headers.get("Origin")
        if not origin:
            return True
        port = self.cfg["port"]
        return origin in (f"http://{request.host}", f"http://127.0.0.1:{port}",
                          f"http://localhost:{port}")

    async def api_permissions(self, request: web.Request) -> web.Response:
        if self.permissions is None:
            raise web.HTTPNotFound()
        return web.json_response(await asyncio.to_thread(self.permissions.summary))

    async def api_permissions_request(self, request: web.Request) -> web.Response:
        if self.permissions is None:
            raise web.HTTPNotFound()
        if not self._same_origin(request):
            raise web.HTTPForbidden(text="cross-origin request refused")
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            raise web.HTTPBadRequest(text="expected JSON")
        if body.get("seen"):
            self.permissions.seen()
            return web.json_response({"said": "ok"})
        said = await asyncio.to_thread(self.permissions.request, str(body.get("id", "")),
                                       str(body.get("arg", "")))
        return web.json_response({"said": said})

    async def api_health(self, request: web.Request) -> web.Response:
        if self.health is None:
            raise web.HTTPNotFound()
        return web.json_response(self.health())

    async def api_models(self, request: web.Request) -> web.Response:
        if self.models is None:
            raise web.HTTPNotFound()
        query = request.query.get("q", "").strip()
        found = self.models.search(query) if query else self.models.scan()
        return web.json_response({"models": found[:400], "total": len(found)})

    async def api_model(self, request: web.Request) -> web.StreamResponse:
        """Serve one model file, only from an allow-listed root."""
        if self.models is None:
            raise web.HTTPNotFound()
        fine = request.query.get("detail") == "fine"
        path = await asyncio.to_thread(self.models.renderable, request.query.get("path", ""), fine)
        if path is None:
            detail = getattr(self.models, "last_error", "")
            raise web.HTTPForbidden(
                text=f"could not render that model. {detail}"[:500])
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Cache-Control": "no-store",
        })

    async def api_model_edit(self, request: web.Request) -> web.StreamResponse:
        """The live-edited build of the model on screen, by its build key."""
        if self.edit_output is None:
            raise web.HTTPNotFound()
        path = self.edit_output(request.query.get("key", ""))
        if path is None:
            raise web.HTTPNotFound(text="no such build")
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Cache-Control": "no-store",
        })

    async def api_config(self, request: web.Request) -> web.Response:
        payload = {"speech": self.config["speech"], "hud": self.config["hud"],
                   "gestures": {"bindings": self.config["gestures"]["bindings"],
                                "require_arm": self.config["gestures"]["require_arm"]}}
        return web.json_response(payload)

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        if not self._same_origin(request):
            raise web.HTTPForbidden(text="cross-origin websocket refused")
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.clients.add(ws)
        try:
            if self.hello is not None:
                await ws.send_json({"type": "hello", **self.hello()})
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                try:
                    await self.on_message(data, ws)
                except Exception as exc:
                    await ws.send_json({"type": "error", "text": f"{exc.__class__.__name__}: {exc}"})
        finally:
            self.clients.discard(ws)
        return ws

    async def feed(self, request: web.Request) -> web.StreamResponse:
        """MJPEG camera feed. A slow client drops frames; it never stalls tracking."""
        if self.get_jpeg is None:
            raise web.HTTPNotFound()
        response = web.StreamResponse(headers={
            "Content-Type": "multipart/x-mixed-replace; boundary=frame",
            "Cache-Control": "no-store, no-cache, must-revalidate",
        })
        await response.prepare(request)
        interval = 1.0 / max(1, int(self.config["vision"].get("fps", 30)))
        last = None
        try:
            while True:
                jpeg = self.get_jpeg()
                if jpeg is not None and jpeg is not last:
                    last = jpeg
                    await response.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                await asyncio.sleep(interval)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    # ---------------------------------------------------------- broadcast

    async def _mirror(self, event: dict) -> None:
        if event.get("type") in NOT_MIRRORED:
            return
        await self.broadcast(event)

    async def broadcast(self, payload: dict) -> None:
        if not self.clients:
            return
        message = json.dumps(payload, default=str)
        dead = []
        for ws in list(self.clients):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                await ws.send_str(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    # ---------------------------------------------------------- lifecycle

    async def start(self) -> str:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg["host"], int(self.cfg["port"]))
        await site.start()
        return f"http://{self.cfg['host']}:{self.cfg['port']}/"

    async def stop(self) -> None:
        for ws in list(self.clients):
            await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()
