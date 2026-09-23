"""A tiny async pub/sub bus.

Every subsystem (vision, gestures, speech, brain, HUD) publishes plain dict
events here; the websocket layer mirrors whatever the HUD cares about. Vision
runs on its own thread, so `publish_threadsafe` exists for that side.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List

Handler = Callable[[dict], Awaitable[None]]


class Bus:
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None):
        self._handlers: Dict[str, List[Handler]] = defaultdict(list)
        self._loop = loop

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def on(self, topic: str, handler: Handler) -> Handler:
        """Subscribe to a topic. '*' receives everything."""
        self._handlers[topic].append(handler)
        return handler

    async def publish(self, topic: str, **payload: Any) -> None:
        event = {"type": topic, "t": time.time(), **payload}
        handlers = list(self._handlers.get(topic, ())) + list(self._handlers.get("*", ()))
        for handler in handlers:
            try:
                await handler(event)
            except Exception as exc:  # a broken subscriber must not stall the bus
                print(f"[bus] handler for {topic} failed: {exc!r}")

    def publish_threadsafe(self, topic: str, **payload: Any) -> None:
        """Publish from a non-asyncio thread (the vision worker)."""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self.publish(topic, **payload), self._loop)
