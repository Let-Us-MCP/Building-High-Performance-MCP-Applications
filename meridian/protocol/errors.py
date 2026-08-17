"""MCP error codes and the exception hierarchy that carries them.

Revision 2026-07-28 partitions the JSON-RPC implementation-defined range:

    -32000 .. -32019   legacy. Allocated before the policy existed. Do not use.
    -32020 .. -32099   reserved for the MCP specification itself.

Everything outside `-32768 .. -32000` is yours. If you invent an error code,
invent it out there, not in the reserved range.
"""

from __future__ import annotations

from typing import Any

# --- JSON-RPC 2.0 base -----------------------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# --- MCP-reserved sub-range ------------------------------------------------
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# --- Retired codes. Reserved forever, never re-issued. ---------------------
# -32002 was "resource not found" through 2025-11-25; it is -32602 now.
# -32042 was "URL elicitation required" in 2025-11-25 only.
RETIRED_RESOURCE_NOT_FOUND = -32002
RETIRED_URL_ELICITATION_REQUIRED = -32042

ERROR_NAMES = {
    PARSE_ERROR: "ParseError",
    INVALID_REQUEST: "InvalidRequest",
    METHOD_NOT_FOUND: "MethodNotFound",
    INVALID_PARAMS: "InvalidParams",
    INTERNAL_ERROR: "InternalError",
    HEADER_MISMATCH: "HeaderMismatch",
    MISSING_REQUIRED_CLIENT_CAPABILITY: "MissingRequiredClientCapability",
    UNSUPPORTED_PROTOCOL_VERSION: "UnsupportedProtocolVersion",
}

# The HTTP status a Streamable HTTP server must return alongside each code.
HTTP_STATUS = {
    PARSE_ERROR: 400,
    INVALID_REQUEST: 400,
    METHOD_NOT_FOUND: 404,
    INVALID_PARAMS: 400,
    INTERNAL_ERROR: 500,
    HEADER_MISMATCH: 400,
    MISSING_REQUIRED_CLIENT_CAPABILITY: 400,
    UNSUPPORTED_PROTOCOL_VERSION: 400,
}


class McpError(Exception):
    """A protocol error. Serialises straight into a JSON-RPC error response.

    Protocol errors are for problems the *model* cannot fix: an unknown method,
    a malformed request, a server that fell over. Problems the model *can* fix
    (a date in the wrong format, a value out of range) are not these. Those are
    tool execution errors, which ride back inside a successful result with
    `isError: true`. Chapter 5 is largely about not confusing the two.
    """

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 400)

    def to_json(self) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            err["data"] = self.data
        return err

    def __repr__(self) -> str:
        name = ERROR_NAMES.get(self.code, "Error")
        return f"McpError({name}, {self.code}, {self.message!r})"


class ParseError(McpError):
    def __init__(self, message: str = "Parse error", data: Any = None):
        super().__init__(PARSE_ERROR, message, data)


class InvalidRequest(McpError):
    def __init__(self, message: str = "Invalid request", data: Any = None):
        super().__init__(INVALID_REQUEST, message, data)


class MethodNotFound(McpError):
    def __init__(self, method: str):
        super().__init__(METHOD_NOT_FOUND, f"Method not found: {method}")


class InvalidParams(McpError):
    def __init__(self, message: str, data: Any = None):
        super().__init__(INVALID_PARAMS, message, data)


class InternalError(McpError):
    def __init__(self, message: str = "Internal error", data: Any = None):
        super().__init__(INTERNAL_ERROR, message, data)


class ResourceNotFound(InvalidParams):
    """Resource lookups fail with -32602 as of 2026-07-28, not -32002.

    Clients should still *accept* -32002 from older servers. Servers on this
    revision must not emit it.
    """

    def __init__(self, uri: str):
        super().__init__("Resource not found", {"uri": uri})
        self.uri = uri


class HeaderMismatch(McpError):
    """An HTTP header disagrees with the request body, or is missing.

    This exists because a gateway routing on `Mcp-Name` and a server executing
    on `params.name` must never be able to disagree. If they can, you have
    built a request-smuggling primitive.
    """

    def __init__(self, message: str):
        super().__init__(HEADER_MISMATCH, message)


class MissingRequiredClientCapability(McpError):
    def __init__(self, required: list[str]):
        super().__init__(
            MISSING_REQUIRED_CLIENT_CAPABILITY,
            "Missing required client capability: " + ", ".join(required),
            {"requiredCapabilities": required},
        )


class UnsupportedProtocolVersion(McpError):
    def __init__(self, requested: str, supported: list[str]):
        super().__init__(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": supported, "requested": requested},
        )
