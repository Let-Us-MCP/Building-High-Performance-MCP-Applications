"""A complete, dependency-free implementation of MCP revision 2026-07-28.

Written for the book. The SDKs are excellent and you should use them in
production. They are also, at the time of writing, still shipping the
handshake-based protocol, and this book is about the wire. So here is the wire.

Roughly two thousand lines, no third-party imports, both transports, all three
primitives, MRTR, subscriptions, caching, and the Tasks extension.

    from meridian.protocol import Server, Client, text_result

    server = Server("example", "1.0.0")

    @server.tool("add", "Add two numbers",
                 {"type": "object",
                  "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                  "required": ["a", "b"]})
    def add(ctx):
        args = ctx.arguments
        return text_result(str(args["a"] + args["b"]))
"""

from .cache import CACHEABLE_METHODS, CacheStats, ResultCache
from .client import Client, DeclineAll, InputProvider, ScriptedInput
from .errors import (
    HeaderMismatch,
    InternalError,
    InvalidParams,
    InvalidRequest,
    McpError,
    MethodNotFound,
    MissingRequiredClientCapability,
    ParseError,
    ResourceNotFound,
    UnsupportedProtocolVersion,
)
from .http import StreamableHttpClient, StreamableHttpServer, validate_x_mcp_header
from .inproc import InProcessTransport
from .jsonrpc import (
    RESULT_COMPLETE,
    RESULT_INPUT_REQUIRED,
    RESULT_TASK,
    IdGenerator,
    Notification,
    Request,
    encode,
    result_type,
)
from .meta import (
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    ClientCapabilities,
    Implementation,
    RequestContext,
    ServerCapabilities,
    build_request_meta,
    parse_request_context,
)
from .mrtr import (
    ElicitResponse,
    StateExpired,
    StateSealer,
    StateTampered,
    elicit_form,
    elicit_url,
    input_required,
    read_elicit,
)
from .server import (
    Prompt,
    Resource,
    ResourceTemplate,
    Server,
    Tool,
    text_result,
    tool_error,
    validate_against_schema,
)
from .stdio import StdioClientTransport, StdioServerTransport
from .subscriptions import SubscriptionSink

__version__ = "1.0.0"
__protocol__ = PROTOCOL_VERSION

__all__ = [
    "PROTOCOL_VERSION", "SUPPORTED_VERSIONS", "__protocol__", "__version__",
    # core
    "Server", "Client", "Tool", "Resource", "ResourceTemplate", "Prompt",
    "RequestContext", "ClientCapabilities", "ServerCapabilities", "Implementation",
    "text_result", "tool_error", "validate_against_schema",
    "build_request_meta", "parse_request_context",
    # transports
    "StdioServerTransport", "StdioClientTransport",
    "StreamableHttpServer", "StreamableHttpClient", "validate_x_mcp_header",
    "InProcessTransport",
    # patterns
    "input_required", "elicit_form", "elicit_url", "read_elicit",
    "ElicitResponse", "StateSealer", "StateExpired", "StateTampered",
    "SubscriptionSink",
    # caching
    "ResultCache", "CacheStats", "CACHEABLE_METHODS",
    # jsonrpc
    "Request", "Notification", "IdGenerator", "encode", "result_type",
    "RESULT_COMPLETE", "RESULT_INPUT_REQUIRED", "RESULT_TASK",
    # errors
    "McpError", "ParseError", "InvalidRequest", "MethodNotFound", "InvalidParams",
    "InternalError", "ResourceNotFound", "HeaderMismatch",
    "MissingRequiredClientCapability", "UnsupportedProtocolVersion",
    # client helpers
    "InputProvider", "DeclineAll", "ScriptedInput",
]
