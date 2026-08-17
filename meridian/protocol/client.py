"""The client half: one connection to one server, plus the loops around it.

A client does four jobs the transports do not:

  1. Stamps `_meta` on every request, because there is no handshake to do it once.
  2. Runs the MRTR retry loop, so a caller sees "tool returned X" and not
     "tool returned a question, then X".
  3. Caches results by `ttlMs` and `cacheScope`, and invalidates on notifications.
  4. Works out whether the server is modern or legacy, once, and remembers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import errors, jsonrpc
from .cache import ResultCache
from .meta import (
    PROTOCOL_VERSION,
    ClientCapabilities,
    Implementation,
    build_request_meta,
)

MAX_MRTR_ROUNDS = 6


@dataclass
class CallStats:
    """The counters Chapter 16 turns into a dashboard."""
    requests: int = 0
    mrtr_rounds: int = 0
    retries: int = 0
    errors: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    latency_ms: list[float] = field(default_factory=list)

    def record(self, ms: float) -> None:
        self.latency_ms.append(ms)

    def percentile(self, p: float) -> float:
        if not self.latency_ms:
            return 0.0
        ordered = sorted(self.latency_ms)
        idx = min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1))))
        return ordered[idx]

    def to_json(self) -> dict:
        return {
            "requests": self.requests,
            "mrtrRounds": self.mrtr_rounds,
            "retries": self.retries,
            "errors": self.errors,
            "bytesSent": self.bytes_sent,
            "bytesReceived": self.bytes_received,
            "p50Ms": round(self.percentile(50), 3),
            "p95Ms": round(self.percentile(95), 3),
            "p99Ms": round(self.percentile(99), 3),
        }


class InputProvider:
    """How a host answers a server's questions.

    A real host renders a form and waits for a human. Tests supply canned
    answers. Both satisfy this interface, which is the point: the MRTR loop
    below does not care which one it is talking to.
    """

    def elicit(self, key: str, params: dict) -> dict:
        return {"action": "decline"}

    def sample(self, key: str, params: dict) -> dict:
        raise errors.InvalidParams("this client does not support sampling")

    def list_roots(self, key: str, params: dict) -> dict:
        return {"roots": []}


class DeclineAll(InputProvider):
    """The safe default. A host that has not been wired up should refuse
    rather than invent answers on the user's behalf."""


class ScriptedInput(InputProvider):
    """Canned answers keyed by elicitation key. Used throughout the tests."""

    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.seen: list[str] = []

    def elicit(self, key: str, params: dict) -> dict:
        self.seen.append(key)
        return self.answers.get(key, {"action": "decline"})

    def sample(self, key: str, params: dict) -> dict:
        self.seen.append(key)
        return self.answers.get(key, {
            "role": "assistant",
            "content": {"type": "text", "text": ""},
            "model": "stub",
            "stopReason": "endTurn",
        })


class Client:
    """One client, one server. The host owns many of these."""

    def __init__(
        self,
        transport,
        *,
        name: str = "meridian-host",
        version: str = "1.0.0",
        capabilities: ClientCapabilities | None = None,
        cache: ResultCache | None = None,
        input_provider: InputProvider | None = None,
        server_label: str | None = None,
        auth_context: str = "anon",
    ):
        self.transport = transport
        self.info = Implementation(name=name, version=version)
        self.capabilities = capabilities or ClientCapabilities(
            elicitation={"form": {}, "url": {}}
        )
        self.cache = cache if cache is not None else ResultCache()
        self.inputs = input_provider or DeclineAll()
        self.label = server_label or getattr(transport, "url", "server")
        self.auth_context = auth_context
        self.ids = jsonrpc.IdGenerator()
        self.stats = CallStats()
        self.protocol_version = PROTOCOL_VERSION

        self._discover_cache: dict | None = None
        self._tool_schemas: dict[str, dict] = {}
        self._lock = threading.RLock()

    # -- low level ----------------------------------------------------------

    def _envelope(self, method: str, params: dict | None = None,
                  *, progress_token: str | int | None = None,
                  traceparent: str | None = None) -> dict:
        body = dict(params or {})
        body["_meta"] = build_request_meta(
            self.capabilities, self.info,
            protocol_version=self.protocol_version,
            progress_token=progress_token,
            traceparent=traceparent,
        )
        return jsonrpc.Request(self.ids.next(), method, body).to_json()

    def send_raw(self, message: dict, *, tool_schema: dict | None = None,
                 on_notification: Callable[[dict], None] | None = None) -> dict:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {}
        if tool_schema is not None and hasattr(self.transport, "_headers"):
            kwargs["tool_schema"] = tool_schema
        if on_notification is not None and hasattr(self.transport, "_headers"):
            kwargs["on_notification"] = on_notification

        response = self.transport.send(message, **kwargs)

        elapsed = (time.perf_counter() - started) * 1000.0
        self.stats.requests += 1
        self.stats.record(elapsed)
        self.stats.bytes_sent += len(jsonrpc.encode(message))
        self.stats.bytes_received += len(jsonrpc.encode(response))
        if jsonrpc.is_error(response):
            self.stats.errors += 1
        return response

    def _unwrap(self, response: dict) -> dict:
        if jsonrpc.is_error(response):
            err = response["error"]
            raise errors.McpError(err["code"], err.get("message", ""), err.get("data"))
        return response.get("result") or {}

    # -- the MRTR loop ------------------------------------------------------

    def call(self, method: str, params: dict | None = None, *,
             progress_token: str | int | None = None,
             traceparent: str | None = None,
             on_notification: Callable[[dict], None] | None = None,
             tool_schema: dict | None = None,
             use_cache: bool = True) -> dict:
        """Issue a request, answering any input the server asks for on the way.

        Callers see one call and one result. Underneath, this may be three
        round trips, each a fresh request with a fresh id, each carrying the
        opaque `requestState` the server handed back. Every one of those extra
        trips costs a network round trip and often a model turn, which is why
        Chapter 17 spends so long on getting rid of them.
        """
        base_params = dict(params or {})

        if use_cache:
            cached = self.cache.get(self.label, method, base_params, self.auth_context)
            if cached is not None:
                return cached

        pending_state: str | None = None
        pending_responses: dict | None = None

        for round_no in range(MAX_MRTR_ROUNDS):
            call_params = dict(base_params)
            if pending_responses is not None:
                call_params["inputResponses"] = pending_responses
            if pending_state is not None:
                call_params["requestState"] = pending_state

            message = self._envelope(
                method, call_params,
                progress_token=progress_token, traceparent=traceparent,
            )
            response = self.send_raw(message, tool_schema=tool_schema,
                                     on_notification=on_notification)
            result = self._unwrap(response)

            if result.get("resultType") != jsonrpc.RESULT_INPUT_REQUIRED:
                if use_cache and pending_responses is None and pending_state is None:
                    self.cache.put(self.label, method, base_params, result,
                                   self.auth_context)
                return result

            self.stats.mrtr_rounds += 1
            requests = result.get("inputRequests") or {}
            pending_state = result.get("requestState")
            pending_responses = self._answer(requests) if requests else {}
            if requests and not pending_responses:
                # Nothing could be answered. Retrying would loop forever.
                raise errors.InvalidParams(
                    "server requested input this client cannot provide"
                )

        raise errors.InternalError(
            f"{method} still asking for input after {MAX_MRTR_ROUNDS} rounds"
        )

    def _answer(self, requests: dict) -> dict:
        """Fulfil each `inputRequests` entry, keyed the way the server keyed it."""
        out: dict[str, Any] = {}
        for key, req in requests.items():
            if not isinstance(req, dict):
                continue
            method = req.get("method")
            params = req.get("params") or {}
            if method == "elicitation/create":
                out[key] = self.inputs.elicit(key, params)
            elif method == "sampling/createMessage":
                out[key] = self.inputs.sample(key, params)
            elif method == "roots/list":
                out[key] = self.inputs.list_roots(key, params)
        return out

    # -- convenience --------------------------------------------------------

    def discover(self, refresh: bool = False) -> dict:
        with self._lock:
            if self._discover_cache is not None and not refresh:
                return self._discover_cache
            result = self.call("server/discover")
            self._discover_cache = result
            return result

    def list_tools(self, all_pages: bool = True) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.call("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor or not all_pages:
                break
        with self._lock:
            for tool in tools:
                self._tool_schemas[tool["name"]] = tool.get("inputSchema") or {}
        return tools

    def call_tool(self, name: str, arguments: dict | None = None, **kw) -> dict:
        schema = self._tool_schemas.get(name)
        return self.call("tools/call",
                         {"name": name, "arguments": arguments or {}},
                         tool_schema=schema, use_cache=False, **kw)

    def read_resource(self, uri: str, **kw) -> dict:
        return self.call("resources/read", {"uri": uri}, **kw)

    def list_resources(self, all_pages: bool = True) -> list[dict]:
        return self._paged("resources/list", "resources", all_pages)

    def list_prompts(self, all_pages: bool = True) -> list[dict]:
        return self._paged("prompts/list", "prompts", all_pages)

    def _paged(self, method: str, key: str, all_pages: bool) -> list[dict]:
        """Walk a paginated list to the end.

        Each page is cached under its own cursor, so a client that stops early
        and resumes later still gets the pages it already has for free.
        """
        items: list[dict] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.call(method, params)
            items.extend(result.get(key, []))
            cursor = result.get("nextCursor")
            if not cursor or not all_pages:
                return items

    def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        return self.call("prompts/get", {"name": name, "arguments": arguments or {}},
                         use_cache=False)

    # -- notifications ------------------------------------------------------

    def on_notification(self, msg: dict) -> None:
        """Turn a change notification into a cache invalidation.

        This is the whole reason `listChanged` and `ttlMs` coexist. The TTL
        stops you refetching while nothing has changed; the notification stops
        you serving stale data the moment something has.
        """
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if method == "notifications/tools/list_changed":
            self.cache.invalidate_method(self.label, "tools/list")
        elif method == "notifications/prompts/list_changed":
            self.cache.invalidate_method(self.label, "prompts/list")
        elif method == "notifications/resources/list_changed":
            self.cache.invalidate_method(self.label, "resources/list")
        elif method == "notifications/resources/updated":
            uri = params.get("uri")
            if uri:
                self.cache.invalidate_uri(self.label, uri)

    # -- era detection ------------------------------------------------------

    def probe_era(self) -> str:
        """Decide whether this server is `modern` or `legacy`, once.

        The trap: a modern server also answers 400 for an unsupported version,
        a missing capability, and a header mismatch. So the status code alone
        cannot drive the fallback. You have to read the body. A recognised
        modern JSON-RPC error means "modern server, fix your request". Anything
        else means "legacy server, go and say `initialize`".
        """
        try:
            self.discover()
            return "modern"
        except errors.McpError as exc:
            if exc.code in (errors.UNSUPPORTED_PROTOCOL_VERSION,
                            errors.MISSING_REQUIRED_CLIENT_CAPABILITY,
                            errors.HEADER_MISMATCH):
                return "modern"
            return "legacy"
        except Exception:
            return "legacy"

    def close(self) -> None:
        if hasattr(self.transport, "close"):
            self.transport.close()
