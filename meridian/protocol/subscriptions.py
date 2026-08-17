"""`subscriptions/listen`: one long-lived request that never quite finishes.

This replaced two things at once. It replaced the standalone HTTP GET stream,
which existed only so servers had somewhere to push from, and it replaced
`resources/subscribe` / `resources/unsubscribe`, which were RPCs that mutated
per-connection state and therefore could not survive statelessness.

The shape is worth appreciating. It is still request/response. The client sends
one request; the server's response is a stream that stays open. All the state
belongs to the request, not to the connection underneath it, which means the
mental model does not need a special case.

Two rules matter more than the rest:

  1. The server sends *only* the notification types the client asked for.
     Not "everything it might like". Only what it opted into.
  2. The acknowledgement goes first, and carries the subscription id. Nothing
     for that subscription may precede it.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from . import jsonrpc
from .meta import KEY_SUBSCRIPTION_ID

FILTER_KEYS = ("toolsListChanged", "promptsListChanged", "resourcesListChanged")


class SubscriptionSink:
    """One open `subscriptions/listen` stream.

    Holds the filter the client requested and the callable that puts bytes on
    the wire. The server calls `wants()` before every notification, so a
    subscriber that asked only for tool changes never sees a resource update.
    """

    def __init__(self, sub_id: Any, requested: dict,
                 send_raw: Callable[[dict], None]):
        self.id = sub_id
        self.requested = requested if isinstance(requested, dict) else {}
        self._send_raw = send_raw
        self.closed = False
        self.acknowledged = False
        self.sent_count = 0
        self._lock = threading.Lock()

        self.flags = {k: bool(self.requested.get(k)) for k in FILTER_KEYS}
        uris = self.requested.get("resourceSubscriptions")
        self.uris: set[str] = set(uris) if isinstance(uris, list) else set()

    # -- filtering
    def wants(self, key: str) -> bool:
        return self.acknowledged and not self.closed and self.flags.get(key, False)

    def wants_uri(self, uri: str) -> bool:
        return self.acknowledged and not self.closed and uri in self.uris

    # -- the granted filter, which may be narrower than the requested one
    def granted(self, capabilities) -> dict:
        caps = capabilities.to_json() if hasattr(capabilities, "to_json") else {}
        out: dict[str, Any] = {}
        supports_list_changed = {
            "toolsListChanged": bool((caps.get("tools") or {}).get("listChanged")),
            "promptsListChanged": bool((caps.get("prompts") or {}).get("listChanged")),
            "resourcesListChanged": bool((caps.get("resources") or {}).get("listChanged")),
        }
        for key in FILTER_KEYS:
            if self.flags[key] and supports_list_changed[key]:
                out[key] = True
            else:
                self.flags[key] = False
        if self.uris and (caps.get("resources") or {}).get("subscribe"):
            out["resourceSubscriptions"] = sorted(self.uris)
        else:
            self.uris = set()
        return out

    # -- sending
    def acknowledge(self, capabilities) -> None:
        """Send `notifications/subscriptions/acknowledged`, first and once.

        The `notifications` field reports the subset the server actually agreed
        to honour. A client that asked for resource subscriptions from a server
        that does not support them learns it here, rather than by waiting
        forever for updates that are never coming.
        """
        granted = self.granted(capabilities)
        self._send_raw(jsonrpc.Notification(
            "notifications/subscriptions/acknowledged",
            {"_meta": {KEY_SUBSCRIPTION_ID: self.id}, "notifications": granted},
        ).to_json())
        self.acknowledged = True

    def send(self, message: dict) -> None:
        """Tag every notification with the subscription id and write it.

        On stdio every subscription shares one pipe, so without this tag a
        client with two open subscriptions cannot tell which one a notification
        belongs to.
        """
        if self.closed:
            return
        with self._lock:
            params = message.setdefault("params", {})
            meta = params.setdefault("_meta", {})
            meta[KEY_SUBSCRIPTION_ID] = self.id
            self._send_raw(message)
            self.sent_count += 1

    def close_gracefully(self) -> None:
        """End the subscription with a proper JSON-RPC response.

        A stream that just dies tells the client nothing. A stream that closes
        with the response to the original `subscriptions/listen` request tells
        it "this ended on purpose, do not reconnect in a panic". The difference
        matters at three in the morning.
        """
        if self.closed:
            return
        self._send_raw(jsonrpc.result_response(
            self.id, {"_meta": {KEY_SUBSCRIPTION_ID: self.id}}
        ))
        self.closed = True

    def close(self) -> None:
        self.closed = True
