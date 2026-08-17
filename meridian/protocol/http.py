"""Streamable HTTP, 2026-07-28 edition.

One endpoint. POST only. Every JSON-RPC request is its own HTTP request, and
the server decides per request whether to answer with a single JSON object or
an SSE stream scoped to that request.

Gone since 2025-11-25, and worth knowing are gone:

  * the GET stream endpoint (subscriptions/listen replaced it)
  * `Mcp-Session-Id` (there are no sessions)
  * `Last-Event-ID` resumability (a broken stream loses the request; retry it
    with a new id, which is why your server had better be idempotent)
  * server-initiated JSON-RPC requests on the stream (MRTR replaced them)

New and mandatory: `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name`
headers mirroring the body. The point is that a gateway can route, rate-limit,
and apply WAF policy without parsing JSON. The catch is that two sources of
truth can disagree, so the server must reject any request where they do. That
check is not optional and it is not paranoia: without it, a load balancer
routing on `Mcp-Name: read_only_tool` will happily forward a body that calls
`wire_transfer`.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import errors, jsonrpc
from .meta import KEY_PROTOCOL_VERSION
from .subscriptions import SubscriptionSink

HDR_VERSION = "MCP-Protocol-Version"
HDR_METHOD = "Mcp-Method"
HDR_NAME = "Mcp-Name"
HDR_PARAM_PREFIX = "Mcp-Param-"

B64_PREFIX = "=?base64?"
B64_SUFFIX = "?="

# Methods that must carry `Mcp-Name`, and where its value comes from.
NAME_SOURCE = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}


# ---------------------------------------------------------------------------
# Header value encoding
# ---------------------------------------------------------------------------


def encode_header_value(value: Any) -> str:
    """Render a value for an HTTP header, escaping to base64 when it cannot
    survive as plain ASCII.

    RFC 9110 allows visible ASCII, space, and tab. Anything else (non-ASCII,
    control characters, leading or trailing whitespace) gets wrapped in the
    `=?base64?...?=` sentinel. A plain value that happens to *look* like the
    sentinel must also be encoded, otherwise `"=?base64?literal?="` decodes
    into something the sender never wrote.
    """
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int):
        text = str(value)
    else:
        text = str(value)

    needs_encoding = (
        not text.isascii()
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)
        or text != text.strip()
        or (text.startswith(B64_PREFIX) and text.endswith(B64_SUFFIX))
    )
    if not needs_encoding:
        return text
    blob = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"{B64_PREFIX}{blob}{B64_SUFFIX}"


def decode_header_value(raw: str) -> str:
    if raw.startswith(B64_PREFIX) and raw.endswith(B64_SUFFIX):
        inner = raw[len(B64_PREFIX):-len(B64_SUFFIX)]
        try:
            return base64.b64decode(inner).decode("utf-8")
        except Exception as exc:
            raise errors.HeaderMismatch("header value is not valid base64") from exc
    return raw


def header_params_for(tool_schema: dict, arguments: dict) -> dict[str, str]:
    """Extract `x-mcp-header` parameters into `Mcp-Param-*` headers.

    Only statically reachable properties count: the path from the schema root
    must be nothing but `properties` keys. No arrays, no `oneOf`, no `$ref`.
    That restriction exists so an intermediary can know, from the schema alone,
    exactly which headers a call will carry, without evaluating the schema
    against the arguments.
    """
    out: dict[str, str] = {}

    def walk(schema: dict, values: Any) -> None:
        props = schema.get("properties")
        if not isinstance(props, dict) or not isinstance(values, dict):
            return
        for key, sub in props.items():
            if not isinstance(sub, dict):
                continue
            header = sub.get("x-mcp-header")
            if header and key in values and values[key] is not None:
                out[HDR_PARAM_PREFIX + header] = encode_header_value(values[key])
            if sub.get("type") == "object":
                walk(sub, values.get(key))

    walk(tool_schema or {}, arguments or {})
    return out


def validate_x_mcp_header(schema: dict) -> list[str]:
    """Check a tool's `x-mcp-header` annotations. Returns a list of problems.

    A client on Streamable HTTP must *exclude* a tool whose annotations violate
    these rules, rather than rejecting the whole catalogue. One malformed tool
    should not take down the other forty-nine.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}

    def walk(node: Any, path: str, reachable: bool) -> None:
        if not isinstance(node, dict):
            return
        header = node.get("x-mcp-header")
        if header is not None:
            if not reachable:
                problems.append(f"{path}: x-mcp-header is not statically reachable")
            elif not isinstance(header, str) or not header:
                problems.append(f"{path}: x-mcp-header must be a non-empty string")
            elif any(c in header for c in "\r\n") or not header.isprintable():
                problems.append(f"{path}: x-mcp-header contains control characters")
            elif header.lower() in seen:
                problems.append(
                    f"{path}: x-mcp-header {header!r} duplicates {seen[header.lower()]}"
                )
            elif node.get("type") not in ("string", "integer", "boolean"):
                problems.append(
                    f"{path}: x-mcp-header allows only string, integer, boolean "
                    f"(got {node.get('type')!r})"
                )
            else:
                seen[header.lower()] = path

        for key, sub in (node.get("properties") or {}).items():
            walk(sub, f"{path}.{key}", reachable)
        # Anything below these keywords is not statically reachable.
        for keyword in ("items", "oneOf", "anyOf", "allOf", "not", "if", "then", "else"):
            branch = node.get(keyword)
            for sub in (branch if isinstance(branch, list) else [branch]):
                walk(sub, f"{path}/{keyword}", False)

    walk(schema, "$", True)
    return problems


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------


class _QuietHTTPServer(ThreadingHTTPServer):
    """A client hanging up mid-stream is normal here, not an incident.

    Cancellation on this transport *is* a closed socket, so the stock handler's
    habit of dumping a traceback for every reset connection turns routine
    operation into alarming log noise.
    """

    def handle_error(self, request, client_address):
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


class StreamableHttpServer:
    """Host a `Server` on a single POST endpoint."""

    def __init__(self, server, host: str = "127.0.0.1", port: int = 0,
                 path: str = "/mcp", *, allowed_origins: set[str] | None = None,
                 authenticator: Callable[[str | None], dict | None] | None = None,
                 stream_threshold_ms: float = 0.0):
        self.server = server
        self.path = path
        self.allowed_origins = allowed_origins
        self.authenticator = authenticator
        self.stream_threshold_ms = stream_threshold_ms
        self._http = _QuietHTTPServer((host, port), self._make_handler())
        self._http.daemon_threads = True
        self.host, self.port = self._http.server_address[0], self._http.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}{self.path}"

    def start(self) -> "StreamableHttpServer":
        self._thread = threading.Thread(target=self._http.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._http.shutdown()
        self._http.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- request handling
    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "meridian-mcp/1.0"

            def log_message(self, *args):  # quiet by default
                pass

            # The GET and DELETE endpoints are gone. Say so unambiguously
            # rather than 404ing, so an old client can tell the difference
            # between "wrong path" and "wrong protocol era".
            def do_GET(self):
                self.send_error(405, "Method Not Allowed")

            def do_DELETE(self):
                self.send_error(405, "Method Not Allowed")

            def do_POST(self):
                outer._handle_post(self)

        return Handler

    def _fail(self, h, status: int, exc: errors.McpError, req_id=None) -> None:
        body = jsonrpc.encode(jsonrpc.error_response(req_id, exc)).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _handle_post(self, h) -> None:
        if h.path.split("?")[0] != self.path:
            self._fail(h, 404, errors.MethodNotFound(h.path))
            return

        # DNS rebinding defence. A browser on any origin can POST to localhost;
        # checking Origin is what stops a web page from driving a local server.
        origin = h.headers.get("Origin")
        if origin and self.allowed_origins is not None and origin not in self.allowed_origins:
            self._fail(h, 403, errors.InvalidRequest(f"Origin not allowed: {origin}"))
            return

        length = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(length) if length else b""

        try:
            message = jsonrpc.parse_message(raw)
        except errors.McpError as exc:
            self._fail(h, 400, exc)
            return

        req_id = message.get("id")

        # Notifications get 202 and no body.
        if jsonrpc.is_notification(message):
            self.server.handle_notification(message.get("method", ""),
                                            message.get("params") or {})
            h.send_response(202)
            h.send_header("Content-Length", "0")
            h.end_headers()
            return

        try:
            self._validate_headers(h.headers, message)
        except errors.McpError as exc:
            self._fail(h, exc.http_status, exc, req_id)
            return

        auth = None
        if self.authenticator is not None:
            auth = self.authenticator(h.headers.get("Authorization"))
            if auth is None:
                h.send_response(401)
                h.send_header(
                    "WWW-Authenticate",
                    'Bearer resource_metadata='
                    f'"http://{self.host}:{self.port}/.well-known/oauth-protected-resource"',
                )
                h.send_header("Content-Length", "0")
                h.end_headers()
                return

        if message.get("method") == "subscriptions/listen":
            self._serve_subscription(h, message)
            return

        wants_stream = (message.get("params") or {}).get("_meta", {}).get("progressToken")
        if wants_stream is not None:
            self._serve_streaming(h, message, auth)
        else:
            self._serve_single(h, message, auth)

    def _validate_headers(self, headers, message: dict) -> None:
        """Reject any disagreement between headers and body.

        Header names are case-insensitive; values are not. `http.client`
        already folds names for us, so only the values need care.
        """
        method = message.get("method", "")
        params = message.get("params") or {}

        version_header = headers.get(HDR_VERSION)
        if version_header is None:
            raise errors.HeaderMismatch(f"Missing required header {HDR_VERSION}")
        body_version = (params.get("_meta") or {}).get(KEY_PROTOCOL_VERSION)
        if body_version is not None and version_header != body_version:
            raise errors.HeaderMismatch(
                f"{HDR_VERSION} header {version_header!r} does not match "
                f"body value {body_version!r}"
            )
        if version_header not in self.server.supported_versions:
            raise errors.UnsupportedProtocolVersion(
                version_header, list(self.server.supported_versions)
            )

        method_header = headers.get(HDR_METHOD)
        if method_header is None:
            raise errors.HeaderMismatch(f"Missing required header {HDR_METHOD}")
        if method_header != method:
            raise errors.HeaderMismatch(
                f"{HDR_METHOD} header {method_header!r} does not match "
                f"body method {method!r}"
            )

        source = NAME_SOURCE.get(method)
        if source:
            name_header = headers.get(HDR_NAME)
            if name_header is None:
                raise errors.HeaderMismatch(f"Missing required header {HDR_NAME}")
            expected = params.get(source)
            if decode_header_value(name_header) != expected:
                raise errors.HeaderMismatch(
                    f"{HDR_NAME} header does not match body {source} {expected!r}"
                )

        # Mirrored tool parameters, when the tool declares any.
        if method == "tools/call":
            tool = self.server._tools.get(params.get("name"))
            if tool is not None:
                expected_headers = header_params_for(tool.input_schema,
                                                     params.get("arguments") or {})
                for key, want in expected_headers.items():
                    got = headers.get(key)
                    if got is None:
                        raise errors.HeaderMismatch(f"Missing required header {key}")
                    if decode_header_value(got) != decode_header_value(want):
                        raise errors.HeaderMismatch(
                            f"{key} header does not match the request body"
                        )

    def _serve_single(self, h, message: dict, auth) -> None:
        response = self.server.handle(message, auth=auth)
        body = jsonrpc.encode(response).encode("utf-8")
        status = 200
        if response and "error" in response:
            code = response["error"]["code"]
            status = errors.HTTP_STATUS.get(code, 400)
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def _serve_streaming(self, h, message: dict, auth) -> None:
        """Answer with an SSE stream scoped to this one request.

        Progress notifications flow here, and only here. They never go on the
        `subscriptions/listen` stream, because they belong to a request, and
        that request has its own stream to ride on.
        """
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        # Tell nginx and friends to stop buffering, or "streaming" becomes
        # "one big chunk at the end" and the whole exercise is pointless.
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        h.end_headers()

        lock = threading.Lock()

        def write(msg: dict) -> None:
            with lock:
                try:
                    h.wfile.write(jsonrpc.sse_event(msg))
                    h.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    raise _ClientGone()

        def emit(token, current, total=None, msg=None):
            params: dict[str, Any] = {"progressToken": token, "progress": current}
            if total is not None:
                params["total"] = total
            if msg:
                params["message"] = msg
            write(jsonrpc.Notification("notifications/progress", params).to_json())

        try:
            response = self.server.handle(message, auth=auth, emit_progress=emit)
            if response is not None:
                write(response)
        except _ClientGone:
            # Closing the stream *is* the cancellation signal on this transport.
            # No `notifications/cancelled` is coming, and none is expected.
            return

    def _serve_subscription(self, h, message: dict) -> None:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-cache")
        h.send_header("X-Accel-Buffering", "no")
        h.send_header("Connection", "close")
        h.end_headers()

        lock = threading.Lock()
        gone = threading.Event()

        def write(msg: dict) -> None:
            with lock:
                try:
                    h.wfile.write(jsonrpc.sse_event(msg))
                    h.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    gone.set()

        sink = SubscriptionSink(message.get("id"),
                                (message.get("params") or {}).get("notifications") or {},
                                write)
        sink.acknowledge(self.server.capabilities())
        self.server.attach_subscriber(sink)
        try:
            # Keep-alive comments stop intermediaries reaping an idle stream.
            while not gone.is_set() and not sink.closed:
                if gone.wait(timeout=15.0):
                    break
                with lock:
                    try:
                        h.wfile.write(jsonrpc.SSE_KEEPALIVE)
                        h.wfile.flush()
                    except Exception:
                        gone.set()
        finally:
            self.server.detach_subscriber(sink)
            sink.close()


class _ClientGone(Exception):
    pass


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------


class StreamableHttpClient:
    """The client half. Keeps connections warm, because handshakes are not free."""

    def __init__(self, url: str, *, token: str | None = None, timeout: float = 30.0,
                 max_connections: int = 6):
        from urllib.parse import urlparse

        parsed = urlparse(url)
        self.url = url
        self.host = parsed.hostname or "127.0.0.1"
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.path = parsed.path or "/mcp"
        self.scheme = parsed.scheme
        self.token = token
        self.timeout = timeout
        self.max_connections = max(1, max_connections)
        self._idle: list = []
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(self.max_connections)
        self.reused_connections = 0
        self.new_connections = 0

    def _acquire(self):
        """Take an idle connection, or make one.

        A pool rather than a single connection, because HTTP/1.1 has no
        multiplexing: one connection carries one request at a time, so three
        parallel tool calls sharing one connection run in sequence and the
        fan-out buys nothing. The pool is what makes the host's parallel path
        parallel when every call goes to the same server.
        """
        import http.client

        self._slots.acquire()
        with self._lock:
            if self._idle:
                self.reused_connections += 1
                return self._idle.pop()
            self.new_connections += 1
        cls = (http.client.HTTPSConnection if self.scheme == "https"
               else http.client.HTTPConnection)
        return cls(self.host, self.port, timeout=self.timeout)

    def _release(self, conn, *, reusable: bool = True) -> None:
        """Return a connection to the pool, or discard it."""
        if reusable and conn is not None:
            with self._lock:
                self._idle.append(conn)
        elif conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        self._slots.release()

    def _headers(self, message: dict, tool_schema: dict | None = None) -> dict:
        method = message.get("method", "")
        params = message.get("params") or {}
        meta = params.get("_meta") or {}
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            HDR_VERSION: meta.get(KEY_PROTOCOL_VERSION, ""),
            HDR_METHOD: method,
        }
        source = NAME_SOURCE.get(method)
        if source and params.get(source) is not None:
            headers[HDR_NAME] = encode_header_value(params[source])
        if tool_schema is not None:
            headers.update(header_params_for(tool_schema, params.get("arguments") or {}))
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def send(self, message: dict, *, tool_schema: dict | None = None,
             on_notification: Callable[[dict], None] | None = None) -> dict:
        """POST one request and return its response.

        If the server answers with SSE, drain the stream: notifications go to
        `on_notification`, and the first response terminates it.
        """
        import http.client

        body = jsonrpc.encode(message).encode("utf-8")
        headers = self._headers(message, tool_schema)

        conn = self._acquire()
        try:
            try:
                conn.request("POST", self.path, body=body, headers=headers)
                resp = conn.getresponse()
            except (http.client.HTTPException, OSError):
                # A dropped keep-alive is normal. Reconnect once and retry.
                try:
                    conn.close()
                except Exception:
                    pass
                cls = (http.client.HTTPSConnection if self.scheme == "https"
                       else http.client.HTTPConnection)
                conn = cls(self.host, self.port, timeout=self.timeout)
                conn.request("POST", self.path, body=body, headers=headers)
                resp = conn.getresponse()

            content_type = resp.getheader("Content-Type") or ""
            if "text/event-stream" in content_type:
                final = None
                for msg in jsonrpc.iter_sse(resp):
                    if "id" in msg and ("result" in msg or "error" in msg):
                        final = msg
                        break
                    if on_notification:
                        on_notification(msg)
                # The stream is single-use; do not put it back in the pool.
                self._release(conn, reusable=False)
                conn = None
                if final is None:
                    raise ConnectionError("stream ended before a response arrived")
                return final

            payload = resp.read()
            reusable = resp.getheader("Connection", "").lower() != "close"
            self._release(conn, reusable=reusable)
            conn = None
            return jsonrpc.parse_message(payload)
        except BaseException:
            if conn is not None:
                self._release(conn, reusable=False)
                conn = None
            raise

    def listen(self, message: dict, on_notification: Callable[[dict], None],
               stop: threading.Event | None = None) -> None:
        """Open a `subscriptions/listen` stream and pump notifications until told to stop."""
        import http.client

        cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        conn = cls(self.host, self.port, timeout=None)
        conn.request("POST", self.path,
                     body=jsonrpc.encode(message).encode("utf-8"),
                     headers=self._headers(message))
        resp = conn.getresponse()
        try:
            for msg in jsonrpc.iter_sse(resp):
                on_notification(msg)
                if stop is not None and stop.is_set():
                    break
        finally:
            conn.close()

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for conn in idle:
            try:
                conn.close()
            except Exception:
                pass
