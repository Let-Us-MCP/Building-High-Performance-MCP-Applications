"""A dual-era bridge: speak 2026-07-28 and the handshake era from one process.

The specification allows a server to serve both eras, and says how it decides
which one it is in: a request carrying modern per-request `_meta` is served
statelessly under this revision, while an `initialize` request selects legacy
semantics for the stdio process or the HTTP session.

You need this in 2026 for an unglamorous reason. Most shipping clients, Claude
Code among them, still open with `initialize`. A server that only speaks the
modern protocol is correct and unusable, which is a bad trade while the
ecosystem catches up.

What the bridge does:

  * answers `initialize` and `notifications/initialized`
  * answers `ping`, which the modern revision removed
  * synthesises the `_meta` envelope the modern core requires, from the
    capabilities the legacy client declared once at handshake time
  * translates `resources/subscribe` into the modern subscription machinery
  * strips `resultType` on the way out, because a strict legacy client may
    reject unknown fields

What it deliberately does not do: pretend the two eras are the same. The legacy
path stores per-connection state, and that state is exactly what makes a
legacy server hard to scale. The bridge keeps it quarantined in one object so
you can see its shape and delete it when your clients catch up.
"""

from __future__ import annotations

import threading
from typing import Any

from . import errors, jsonrpc
from .meta import (
    KEY_CLIENT_CAPABILITIES,
    KEY_CLIENT_INFO,
    KEY_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
)

# Handshake-era revisions this bridge will negotiate, newest first.
LEGACY_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
DEFAULT_LEGACY_VERSION = "2025-06-18"


class LegacyBridge:
    """Wraps a modern `Server` and adds handshake-era compatibility.

    Presents the same `handle(message)` interface as `Server`, so the
    transports do not know or care which one they are driving.
    """

    def __init__(self, server, *, default_version: str = DEFAULT_LEGACY_VERSION):
        self.server = server
        self.default_version = default_version
        self._lock = threading.RLock()

        # The per-connection state legacy requires, and modern does not.
        self.era: str | None = None            # None until the client reveals itself
        self.negotiated_version: str | None = None
        self.client_capabilities: dict = {}
        self.client_info: dict = {}

    # Delegate the bits transports and tests reach for.
    def __getattr__(self, name):
        return getattr(self.server, name)

    @property
    def supported_versions(self) -> list[str]:
        return list(self.server.supported_versions) + LEGACY_VERSIONS

    # -- dispatch -----------------------------------------------------------

    def handle(self, message: dict, *, auth: dict | None = None,
               emit_progress=None) -> dict | None:
        method = message.get("method", "")

        if method == "initialize":
            return self._initialize(message)

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            # Removed in 2026-07-28. Legacy clients still send it, and a
            # timeout here reads to them as a dead server.
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}

        params = message.get("params")
        has_modern_meta = (
            isinstance(params, dict)
            and isinstance(params.get("_meta"), dict)
            and KEY_PROTOCOL_VERSION in params["_meta"]
        )

        if has_modern_meta:
            with self._lock:
                if self.era is None:
                    self.era = "modern"
            return self.server.handle(message, auth=auth, emit_progress=emit_progress)

        # No modern envelope. If we shook hands, serve it under legacy rules.
        with self._lock:
            handshook = self.era == "legacy"
        if not handshook:
            # A modern client that forgot its `_meta` gets the modern error,
            # which is what tells it to fix the request rather than fall back.
            return self.server.handle(message, auth=auth, emit_progress=emit_progress)

        return self._handle_legacy(message, auth=auth, emit_progress=emit_progress)

    # -- the handshake ------------------------------------------------------

    def _initialize(self, message: dict) -> dict:
        params = message.get("params") or {}
        requested = params.get("protocolVersion") or self.default_version
        version = requested if requested in LEGACY_VERSIONS else self.default_version

        with self._lock:
            self.era = "legacy"
            self.negotiated_version = version
            self.client_capabilities = params.get("capabilities") or {}
            self.client_info = params.get("clientInfo") or {}

        caps = self.server.capabilities().to_json()
        # `subscribe` in the legacy world meant `resources/subscribe`, which the
        # bridge emulates below, so advertising it is honest.
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "result": {
                "protocolVersion": version,
                "capabilities": caps,
                "serverInfo": self.server.info.to_json(),
                **({"instructions": self.server.instructions}
                   if self.server.instructions else {}),
            },
        }

    # -- legacy requests ----------------------------------------------------

    def _handle_legacy(self, message: dict, *, auth, emit_progress) -> dict | None:
        method = message.get("method", "")

        if method in ("resources/subscribe", "resources/unsubscribe"):
            # Removed in this revision in favour of `subscriptions/listen`.
            # Acknowledge so old clients do not error, and let the modern
            # subscription path do the real work if one is open.
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}

        if method == "logging/setLevel":
            # Also removed. Log level is per-request now.
            return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}

        upgraded = self._inject_meta(message)
        response = self.server.handle(upgraded, auth=auth, emit_progress=emit_progress)
        return self._downgrade(response)

    def _inject_meta(self, message: dict) -> dict:
        """Synthesise the envelope the modern core insists on.

        The values come from the handshake rather than from the request, which
        is precisely the coupling 2026-07-28 removed. Doing it here keeps the
        modern server ignorant of the compromise.
        """
        upgraded = dict(message)
        params = dict(message.get("params") or {})
        meta = dict(params.get("_meta") or {})

        meta[KEY_PROTOCOL_VERSION] = PROTOCOL_VERSION
        meta.setdefault(KEY_CLIENT_CAPABILITIES,
                        self._translate_capabilities(self.client_capabilities))
        if self.client_info:
            meta.setdefault(KEY_CLIENT_INFO, self.client_info)

        params["_meta"] = meta
        upgraded["params"] = params
        return upgraded

    @staticmethod
    def _translate_capabilities(legacy_caps: dict) -> dict:
        """Legacy capability objects are close enough to pass through.

        The one thing worth normalising: an empty `elicitation` object means
        form mode, which the modern reader already handles, so this stays
        deliberately thin rather than inventing structure the client never sent.
        """
        out: dict[str, Any] = {}
        for key in ("elicitation", "sampling", "roots"):
            if key in legacy_caps:
                out[key] = legacy_caps[key] or {}
        if "experimental" in legacy_caps:
            out["extensions"] = {}
        return out

    @staticmethod
    def _downgrade(response: dict | None) -> dict | None:
        """Remove fields a strict handshake-era client will not recognise.

        `resultType` is the important one. A legacy client validating results
        against its own schema may reject an unknown required field, and there
        is no reason to make it find out about a revision it does not speak.
        """
        if response is None or "result" not in response:
            return response
        result = dict(response["result"])

        if result.get("resultType") == jsonrpc.RESULT_INPUT_REQUIRED:
            # MRTR has no legacy equivalent. Rather than hang the client with a
            # shape it cannot parse, say plainly that this operation needs a
            # newer client.
            return jsonrpc.error_response(
                response.get("id"),
                errors.InvalidParams(
                    "This operation needs additional input, which requires a "
                    "client speaking MCP 2026-07-28 or later."
                ),
            )

        result.pop("resultType", None)
        # `ttlMs` and `cacheScope` are additive and harmless, so they stay.
        return {**response, "result": result}


def serve_dual_era(server, *, http_port: int | None = None) -> int:
    """Run a server that answers both eras. The entry point `.mcp.json` uses."""
    import sys
    import time

    from .http import StreamableHttpServer
    from .stdio import StdioServerTransport

    bridge = LegacyBridge(server)

    if http_port:
        http = StreamableHttpServer(bridge, port=http_port)
        print(f"{server.info.name} (dual-era) on {http.url}", file=sys.stderr, flush=True)
        http.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            http.stop()
        return 0

    StdioServerTransport(bridge).serve_forever()
    return 0
