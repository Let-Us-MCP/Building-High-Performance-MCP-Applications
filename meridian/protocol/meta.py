"""The `_meta` envelope: what replaced the handshake.

Before 2026-07-28, a client opened a connection, sent `initialize`, got back a
protocol version and a capability set, and both sides remembered that for the
life of the session. The server was allowed to be a stateful thing.

Now every single request carries its own version and its own capabilities, and
the server is required to forget everything the moment it answers. That is the
whole change, and this module is where it lives.

Required on every client request:

    io.modelcontextprotocol/protocolVersion      string
    io.modelcontextprotocol/clientCapabilities   ClientCapabilities

Recommended, never trusted for security decisions:

    io.modelcontextprotocol/clientInfo           Implementation
    io.modelcontextprotocol/serverInfo           Implementation (on results)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "2026-07-28"

# Versions this codebase can speak, newest first.
SUPPORTED_VERSIONS = ["2026-07-28"]

# Revisions that used the `initialize` handshake. Kept for the era probe.
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

# --- Reserved `_meta` keys -------------------------------------------------
KEY_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
KEY_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
KEY_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
KEY_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
KEY_LOG_LEVEL = "io.modelcontextprotocol/logLevel"
KEY_SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
KEY_PROGRESS_TOKEN = "progressToken"

# W3C trace context rides in `_meta` under its own names, as an explicit
# exception to the reverse-DNS prefix rule. Chapter 16 leans on this hard.
KEY_TRACEPARENT = "traceparent"
KEY_TRACESTATE = "tracestate"
KEY_BAGGAGE = "baggage"

REQUIRED_REQUEST_KEYS = (KEY_PROTOCOL_VERSION, KEY_CLIENT_CAPABILITIES)

# Prefixes whose second label is `modelcontextprotocol` or `mcp` are reserved.
_META_KEY_RE = re.compile(
    r"^(?:(?P<prefix>[A-Za-z](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*)/)?"
    r"(?P<name>|[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)$"
)
_RESERVED_SECOND_LABEL = {"modelcontextprotocol", "mcp"}


def is_reserved_meta_key(key: str) -> bool:
    """True if `key` sits under a prefix the specification reserves for itself.

    `io.modelcontextprotocol/` and `dev.mcp/` are reserved. `com.example.mcp/`
    is not, because the reservation is on the *second* label, not any label.
    """
    if key in (KEY_TRACEPARENT, KEY_TRACESTATE, KEY_BAGGAGE):
        return True
    m = _META_KEY_RE.match(key)
    if not m or not m.group("prefix"):
        return False
    labels = m.group("prefix").split(".")
    return len(labels) >= 2 and labels[1] in _RESERVED_SECOND_LABEL


def validate_meta_key(key: str) -> bool:
    return bool(_META_KEY_RE.match(key))


@dataclass(frozen=True)
class Implementation:
    """Self-reported identity. Display and logging only.

    The specification is blunt about this: it is not verified, so do not branch
    on it and never make a security decision with it. Servers that sniff
    `clientInfo.name` to enable behaviour have reinvented the browser
    user-agent string, including the part where everyone eventually lies.
    """

    name: str
    version: str
    title: str | None = None

    def to_json(self) -> dict:
        out = {"name": self.name, "version": self.version}
        if self.title:
            out["title"] = self.title
        return out

    @staticmethod
    def from_json(d: Any) -> "Implementation | None":
        if not isinstance(d, dict) or "name" not in d:
            return None
        return Implementation(
            name=str(d.get("name", "")),
            version=str(d.get("version", "")),
            title=d.get("title"),
        )


@dataclass
class ClientCapabilities:
    """What the client can do, restated on every single request.

    `elicitation` and `sampling` gate what a server may ask for in an
    `InputRequiredResult`. A server that returns an `elicitation/create` to a
    client that never declared elicitation support has broken the protocol,
    and the client is entitled to fail the call.
    """

    elicitation: dict | None = None
    sampling: dict | None = None
    roots: dict | None = None
    extensions: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        out: dict[str, Any] = {}
        if self.elicitation is not None:
            out["elicitation"] = self.elicitation
        if self.sampling is not None:
            out["sampling"] = self.sampling
        if self.roots is not None:
            out["roots"] = self.roots
        if self.extensions:
            out["extensions"] = self.extensions
        return out

    @staticmethod
    def from_json(d: Any) -> "ClientCapabilities":
        d = d if isinstance(d, dict) else {}
        return ClientCapabilities(
            elicitation=d.get("elicitation"),
            sampling=d.get("sampling"),
            roots=d.get("roots"),
            extensions=d.get("extensions") or {},
        )

    def supports_elicitation(self, mode: str = "form") -> bool:
        if self.elicitation is None:
            return False
        # An empty object means form mode, for backwards compatibility.
        if not self.elicitation:
            return mode == "form"
        return mode in self.elicitation

    def supports_sampling(self) -> bool:
        return self.sampling is not None

    def supports_extension(self, ident: str) -> bool:
        return ident in self.extensions


@dataclass
class ServerCapabilities:
    tools: dict | None = None
    resources: dict | None = None
    prompts: dict | None = None
    completions: dict | None = None
    extensions: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        out: dict[str, Any] = {}
        for name in ("tools", "resources", "prompts", "completions"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.extensions:
            out["extensions"] = self.extensions
        return out

    @staticmethod
    def from_json(d: Any) -> "ServerCapabilities":
        d = d if isinstance(d, dict) else {}
        return ServerCapabilities(
            tools=d.get("tools"),
            resources=d.get("resources"),
            prompts=d.get("prompts"),
            completions=d.get("completions"),
            extensions=d.get("extensions") or {},
        )


@dataclass
class RequestContext:
    """Everything a server may know about a request. Nothing more.

    Note what is absent: there is no connection, no session, no conversation.
    If a handler wants state that outlives one call, it has to get it from an
    argument the client sent, which is exactly the point of the redesign.
    """

    method: str
    params: dict
    protocol_version: str
    capabilities: ClientCapabilities
    client_info: Implementation | None = None
    log_level: str | None = None
    progress_token: str | int | None = None
    traceparent: str | None = None
    tracestate: str | None = None
    baggage: str | None = None
    auth: dict | None = None
    request_id: str | int | None = None
    raw_meta: dict = field(default_factory=dict)

    # Set by the transport so handlers can stream progress without knowing how.
    emit_progress: Any = None

    @property
    def arguments(self) -> dict:
        return self.params.get("arguments") or {}

    @property
    def input_responses(self) -> dict:
        return self.params.get("inputResponses") or {}

    @property
    def request_state(self) -> str | None:
        return self.params.get("requestState")

    def require_capability(self, *names: str) -> None:
        missing = [n for n in names if getattr(self.capabilities, n, None) is None]
        if missing:
            from .errors import MissingRequiredClientCapability

            raise MissingRequiredClientCapability(missing)

    def progress(self, current: float, total: float | None = None,
                 message: str | None = None) -> None:
        """Emit a progress notification, if the client asked for one.

        Silently does nothing when the client omitted `progressToken`, which is
        the correct behaviour: the specification forbids sending progress for a
        request that did not opt in.
        """
        if self.progress_token is None or self.emit_progress is None:
            return
        self.emit_progress(self.progress_token, current, total, message)


def build_request_meta(
    capabilities: ClientCapabilities,
    client_info: Implementation | None = None,
    *,
    protocol_version: str = PROTOCOL_VERSION,
    progress_token: str | int | None = None,
    log_level: str | None = None,
    traceparent: str | None = None,
    tracestate: str | None = None,
    baggage: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Construct the `_meta` block a conforming client puts on every request."""
    meta: dict[str, Any] = {
        KEY_PROTOCOL_VERSION: protocol_version,
        KEY_CLIENT_CAPABILITIES: capabilities.to_json(),
    }
    if client_info is not None:
        meta[KEY_CLIENT_INFO] = client_info.to_json()
    if progress_token is not None:
        meta[KEY_PROGRESS_TOKEN] = progress_token
    if log_level is not None:
        meta[KEY_LOG_LEVEL] = log_level
    if traceparent:
        meta[KEY_TRACEPARENT] = traceparent
    if tracestate:
        meta[KEY_TRACESTATE] = tracestate
    if baggage:
        meta[KEY_BAGGAGE] = baggage
    if extra:
        meta.update(extra)
    return meta


def parse_request_context(
    method: str,
    params: dict,
    *,
    request_id: str | int | None = None,
    auth: dict | None = None,
    supported_versions: list[str] | None = None,
) -> RequestContext:
    """Validate and unpack `_meta` into a `RequestContext`.

    Raises `InvalidParams` when a required field is missing, and
    `UnsupportedProtocolVersion` when the declared version is not one we speak.
    Both map to HTTP 400, which is why a client cannot use the status code
    alone to tell a modern server from a legacy one. See `client.probe_era`.
    """
    from .errors import InvalidParams, UnsupportedProtocolVersion

    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise InvalidParams("Request is missing the required `_meta` object")

    missing = [k for k in REQUIRED_REQUEST_KEYS if k not in meta]
    if missing:
        raise InvalidParams("Request `_meta` is missing required fields: "
                            + ", ".join(missing))

    version = meta[KEY_PROTOCOL_VERSION]
    supported = supported_versions or SUPPORTED_VERSIONS
    if version not in supported:
        raise UnsupportedProtocolVersion(str(version), list(supported))

    return RequestContext(
        method=method,
        params=params,
        protocol_version=version,
        capabilities=ClientCapabilities.from_json(meta.get(KEY_CLIENT_CAPABILITIES)),
        client_info=Implementation.from_json(meta.get(KEY_CLIENT_INFO)),
        log_level=meta.get(KEY_LOG_LEVEL),
        progress_token=meta.get(KEY_PROGRESS_TOKEN),
        traceparent=meta.get(KEY_TRACEPARENT),
        tracestate=meta.get(KEY_TRACESTATE),
        baggage=meta.get(KEY_BAGGAGE),
        auth=auth,
        request_id=request_id,
        raw_meta=meta,
    )
