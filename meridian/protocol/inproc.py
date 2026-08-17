"""An in-process transport: the control group.

Same `send(message) -> response` interface as stdio and Streamable HTTP, but it
calls the server directly. Subtract its numbers from the real transports and
what remains is transport cost, cleanly separated from server execution cost.

That subtraction is the only honest way to answer "is my server slow, or is my
transport slow", and it is the first thing Chapter 16 teaches you to do.

It also makes tests fast and deterministic, which is why every contract test
uses it.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from . import jsonrpc


class InProcessTransport:
    """Talk to a `Server` object with no bytes on any wire.

    Serialisation is still performed, then immediately reversed. That is
    deliberate: skipping it would make the control group cheaper than any real
    transport could ever be, and the comparison would flatter the wrong things.
    """

    def __init__(self, server, *, auth: dict | None = None,
                 serialise: bool = True, latency_ms: float = 0.0):
        self.server = server
        self.auth = auth
        self.serialise = serialise
        self.latency_ms = latency_ms
        self.url = f"inproc://{server.info.name}"
        self.notifications: list[dict] = []
        self.request_count = 0
        self.serialise_ms = 0.0
        self._lock = threading.Lock()

    def send(self, message: dict, *, tool_schema: dict | None = None,
             on_notification: Callable[[dict], None] | None = None) -> dict:
        with self._lock:
            self.request_count += 1

        if self.serialise:
            started = time.perf_counter()
            wire = jsonrpc.encode(message)
            message = json.loads(wire)
            self.serialise_ms += (time.perf_counter() - started) * 1000.0

        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)

        def emit(token, current, total=None, msg=None):
            params: dict[str, Any] = {"progressToken": token, "progress": current}
            if total is not None:
                params["total"] = total
            if msg:
                params["message"] = msg
            note = jsonrpc.Notification("notifications/progress", params).to_json()
            self.notifications.append(note)
            if on_notification:
                on_notification(note)

        response = self.server.handle(message, auth=self.auth, emit_progress=emit)

        if self.serialise and response is not None:
            started = time.perf_counter()
            response = json.loads(jsonrpc.encode(response))
            self.serialise_ms += (time.perf_counter() - started) * 1000.0

        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)

        return response if response is not None else {}

    def close(self) -> None:
        return None
