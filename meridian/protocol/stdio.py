"""The stdio transport: one process, two pipes, newline-delimited JSON.

Rules that bite people:

  * `stdout` carries MCP messages and nothing else. One stray `print()` in a
    library you depend on corrupts the stream and the client sees a parse error
    it cannot attribute. Log to `stderr`.
  * One message per line, no embedded newlines. `json.dumps` never emits a raw
    newline, so this is free as long as nobody pretty-prints.
  * The connection is not a session. Clients may interleave unrelated requests
    on the same pipe, so the server must not treat process identity as
    conversation identity.
  * Everything shares one channel, including notifications for every open
    subscription. That is why `subscriptionId` is mandatory in `_meta` here.
"""

from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from . import errors, jsonrpc
from .meta import KEY_SUBSCRIPTION_ID


class StdioServerTransport:
    """Serve a `Server` over stdin/stdout."""

    def __init__(self, server, stdin=None, stdout=None, stderr=None):
        self.server = server
        self._in = stdin or sys.stdin
        self._out = stdout or sys.stdout
        self._err = stderr or sys.stderr
        self._write_lock = threading.Lock()
        self._cancelled: set[Any] = set()
        server.attach_subscriber(self._make_broadcast_sink())

    # -- writing
    def _write(self, msg: dict) -> None:
        line = jsonrpc.encode(msg)
        with self._write_lock:
            self._out.write(line + "\n")
            self._out.flush()

    def log(self, *parts: Any) -> None:
        print(*parts, file=self._err, flush=True)

    def _make_broadcast_sink(self):
        transport = self

        class _Sink:
            """Placeholder sink. `serve_subscription` installs real ones."""
            def wants(self, key: str) -> bool: return False
            def wants_uri(self, uri: str) -> bool: return False
            def send(self, msg: dict) -> None: transport._write(msg)

        return _Sink()

    # -- progress
    def _progress_emitter(self):
        def emit(token, current, total=None, message=None):
            params: dict[str, Any] = {"progressToken": token, "progress": current}
            if total is not None:
                params["total"] = total
            if message:
                params["message"] = message
            self._write(jsonrpc.Notification("notifications/progress", params).to_json())
        return emit

    # -- main loop
    def serve_forever(self) -> None:
        """Read, dispatch, write, until stdin closes.

        Closing stdin is the primary graceful-shutdown signal and the only
        portable one, so honouring it promptly is what keeps clients from
        escalating to SIGKILL.
        """
        for line in self._in:
            line = line.strip()
            if not line:
                continue
            try:
                message = jsonrpc.parse_message(line)
            except errors.McpError as exc:
                self._write(jsonrpc.error_response(None, exc))
                continue

            if message.get("method") == "notifications/cancelled":
                target = (message.get("params") or {}).get("requestId")
                self._cancelled.add(target)
                continue

            if message.get("method") == "subscriptions/listen":
                threading.Thread(target=self._serve_subscription,
                                 args=(message,), daemon=True).start()
                continue

            self._dispatch(message)

    def _dispatch(self, message: dict) -> None:
        response = self.server.handle(
            message, emit_progress=self._progress_emitter()
        )
        req_id = message.get("id")
        # A request cancelled while in flight gets no further messages at all,
        # including its response.
        if response is not None and req_id not in self._cancelled:
            self._write(response)
        self._cancelled.discard(req_id)

    def _serve_subscription(self, message: dict) -> None:
        from .subscriptions import SubscriptionSink

        sub_id = message.get("id")
        params = message.get("params") or {}
        wanted = params.get("notifications") or {}
        sink = SubscriptionSink(sub_id, wanted, self._write)

        # Acknowledge first. Nothing else may precede it for this subscription.
        sink.acknowledge(self.server.capabilities())
        self.server.attach_subscriber(sink)
        try:
            while not sink.closed:
                time.sleep(0.05)
        finally:
            self.server.detach_subscriber(sink)


class StdioClientTransport:
    """Launch a server subprocess and talk to it.

    Cold start is the number that matters here and it is not small: Python
    interpreter boot plus imports plus whatever the server does at module
    scope. Chapter 3 measures it, and Chapter 17 amortises it with a warm pool.
    """

    def __init__(self, command: list[str], *, env: dict | None = None,
                 cwd: str | None = None, capture_stderr: bool = True):
        self.command = command
        self._proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else None,
            env={**os.environ, **(env or {})},
            cwd=cwd,
            text=True,
            bufsize=1,
        )
        self._pending: dict[Any, queue.Queue] = {}
        self._notifications: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._alive = True
        self.stderr_lines: list[str] = []

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if capture_stderr:
            threading.Thread(target=self._stderr_loop, daemon=True).start()

    def _read_loop(self) -> None:
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    waiter = self._pending.pop(msg["id"], None)
                if waiter:
                    waiter.put(msg)
            else:
                self._notifications.put(msg)
        self._alive = False

    def _stderr_loop(self) -> None:
        for line in self._proc.stderr:
            self.stderr_lines.append(line.rstrip())

    def send(self, message: dict, timeout: float = 30.0) -> dict:
        req_id = message.get("id")
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[req_id] = waiter
        self._proc.stdin.write(jsonrpc.encode(message) + "\n")
        self._proc.stdin.flush()
        try:
            return waiter.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._pending.pop(req_id, None)
            # Cancellation on stdio is an explicit notification, because there
            # is no per-request stream to close.
            self.notify("notifications/cancelled", {"requestId": req_id})
            raise TimeoutError(f"no response to {message.get('method')} in {timeout}s")

    def notify(self, method: str, params: dict) -> None:
        self._proc.stdin.write(
            jsonrpc.encode(jsonrpc.Notification(method, params).to_json()) + "\n")
        self._proc.stdin.flush()

    def drain_notifications(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self._notifications.get_nowait())
            except queue.Empty:
                return out

    def close(self, timeout: float = 5.0) -> None:
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Escalate only if the server ignored the EOF on stdin.
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for pipe in (self._proc.stdout, self._proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
