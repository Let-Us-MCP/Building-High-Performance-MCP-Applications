"""JSON-RPC 2.0 framing, plus the two things MCP adds on top.

MCP's additions are small and both matter enormously:

  1. `result.resultType` is required. `"complete"` means you have the answer.
     `"input_required"` means the server wants something before it can answer.
     Extensions add more; the Tasks extension adds `"task"`.
  2. Request ids must not be null, and must be unique among in-flight requests.

Everything else is stock JSON-RPC. That is deliberate: the interesting parts of
MCP are in the payloads and the patterns, not in a bespoke framing layer.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any, Iterator

from .errors import McpError, ParseError

JSONRPC_VERSION = "2.0"

RESULT_COMPLETE = "complete"
RESULT_INPUT_REQUIRED = "input_required"
RESULT_TASK = "task"  # from the io.modelcontextprotocol/tasks extension


class IdGenerator:
    """Monotonic request ids.

    MRTR requires a *different* id on the retry, because the retry is a new
    request and not a continuation of the old one. A shared counter is the
    least error-prone way to guarantee that.
    """

    def __init__(self, prefix: str = "r"):
        self._counter = itertools.count(1)
        self._prefix = prefix

    def next(self) -> str:
        return f"{self._prefix}{next(self._counter)}"


@dataclass
class Request:
    id: str | int
    method: str
    params: dict

    def to_json(self) -> dict:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "id": self.id,
            "method": self.method,
            "params": self.params,
        }


@dataclass
class Notification:
    method: str
    params: dict

    def to_json(self) -> dict:
        return {
            "jsonrpc": JSONRPC_VERSION,
            "method": self.method,
            "params": self.params,
        }


def result_response(request_id: str | int, result: dict) -> dict:
    """Wrap a result, defaulting `resultType` to `complete` when unset."""
    body = dict(result)
    body.setdefault("resultType", RESULT_COMPLETE)
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": body}


def error_response(request_id: str | int | None, error: McpError) -> dict:
    out: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "error": error.to_json()}
    # The id is omitted only when the request was too malformed to read one.
    if request_id is not None:
        out["id"] = request_id
    return out


def result_type(response: dict) -> str:
    """Read `resultType`, treating its absence as `complete`.

    Servers on 2025-11-25 and earlier do not send the field at all. The
    specification requires clients to read that as `complete`, so this one
    `.get` default is the entire backwards-compatibility story for results.
    """
    result = response.get("result")
    if not isinstance(result, dict):
        return RESULT_COMPLETE
    return result.get("resultType", RESULT_COMPLETE)


def is_error(response: dict) -> bool:
    return "error" in response


def parse_message(raw: str | bytes) -> dict:
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ParseError(f"Invalid JSON: {exc}") from exc
    if not isinstance(msg, dict):
        raise ParseError("Message must be a JSON object")
    return msg


def validate_request(msg: dict) -> None:
    """Check the framing invariants a receiver must enforce."""
    from .errors import InvalidRequest

    if msg.get("jsonrpc") != JSONRPC_VERSION:
        raise InvalidRequest("Missing or wrong `jsonrpc` version")
    if "method" not in msg or not isinstance(msg["method"], str):
        raise InvalidRequest("Missing or non-string `method`")
    if "id" in msg:
        if msg["id"] is None:
            raise InvalidRequest("Request id must not be null")
        if not isinstance(msg["id"], (str, int)) or isinstance(msg["id"], bool):
            raise InvalidRequest("Request id must be a string or integer")
    params = msg.get("params")
    if params is not None and not isinstance(params, dict):
        raise InvalidRequest("`params` must be an object")


def is_notification(msg: dict) -> bool:
    return "id" not in msg


def encode(msg: dict) -> str:
    """Serialise for the wire.

    `separators` drops the spaces json.dumps adds by default. On a catalogue of
    fifty tools that is a couple of kilobytes per `tools/list`, which is worth
    having for free. `ensure_ascii=False` keeps UTF-8 as UTF-8 rather than
    inflating every non-ASCII character into a six-byte escape.
    """
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False)


def iter_sse(stream: Iterator[bytes]) -> Iterator[dict]:
    """Parse an SSE byte stream into the JSON-RPC messages it carries.

    Lines beginning with a colon are comments, used as keep-alives on long-lived
    `subscriptions/listen` streams. They carry no data and must be ignored
    rather than treated as malformed input.
    """
    data_lines: list[str] = []
    for raw_line in stream:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith(":"):
            continue
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                try:
                    yield parse_message(payload)
                except ParseError:
                    continue
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))


def sse_event(msg: dict) -> bytes:
    return f"data: {encode(msg)}\n\n".encode("utf-8")


SSE_KEEPALIVE = b":\r\n"
