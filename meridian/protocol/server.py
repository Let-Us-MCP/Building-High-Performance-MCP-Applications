"""The server core: routing, discovery, and the primitive registries.

Note what this class does not have. No `initialize`. No session object. No
per-connection state of any kind. A `Server` is a pure function from request to
response, which is why you can run twenty of them behind a round-robin load
balancer and never think about stickiness again.

The only state here is the *catalogue*: which tools, resources, and prompts
exist. That is a property of the deployment, not of any conversation, and the
specification is explicit that it must not vary per connection. It may vary by
the authorization on the request, because credentials arrive per request.
"""

from __future__ import annotations

import inspect
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from . import errors, jsonrpc
from .meta import (
    KEY_SERVER_INFO,
    SUPPORTED_VERSIONS,
    Implementation,
    RequestContext,
    ServerCapabilities,
    parse_request_context,
)

DEFAULT_PAGE_SIZE = 50

TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


# ---------------------------------------------------------------------------
# Registry entries
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[RequestContext], Any]
    title: str | None = None
    output_schema: dict | None = None
    annotations: dict | None = None
    icons: list[dict] | None = None
    ui_resource_uri: str | None = None   # MCP Apps: _meta.ui.resourceUri

    def to_json(self) -> dict:
        out: dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }
        if self.title:
            out["title"] = self.title
        if self.output_schema:
            out["outputSchema"] = self.output_schema
        if self.annotations:
            out["annotations"] = self.annotations
        if self.icons:
            out["icons"] = self.icons
        if self.ui_resource_uri:
            out["_meta"] = {"ui": {"resourceUri": self.ui_resource_uri}}
        return out


@dataclass
class Resource:
    uri: str
    name: str
    reader: Callable[[RequestContext, str], Any]
    title: str | None = None
    description: str | None = None
    mime_type: str = "text/plain"
    ttl_ms: int = 60_000
    cache_scope: str = "private"
    annotations: dict | None = None
    size: int | None = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {"uri": self.uri, "name": self.name}
        for key, value in (
            ("title", self.title), ("description", self.description),
            ("mimeType", self.mime_type), ("size", self.size),
            ("annotations", self.annotations),
        ):
            if value is not None:
                out[key] = value
        return out


@dataclass
class ResourceTemplate:
    uri_template: str
    name: str
    reader: Callable[[RequestContext, str, dict], Any]
    title: str | None = None
    description: str | None = None
    mime_type: str | None = None
    ttl_ms: int = 60_000
    cache_scope: str = "private"
    _regex: re.Pattern | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # RFC 6570 level 1 is all the book needs: {var} inside a path.
        pattern = re.escape(self.uri_template)
        pattern = re.sub(r"\\\{(\w+)\\\}", r"(?P<\1>[^/]+)", pattern)
        self._regex = re.compile("^" + pattern + "$")

    def match(self, uri: str) -> dict | None:
        m = self._regex.match(uri) if self._regex else None
        return m.groupdict() if m else None

    def to_json(self) -> dict:
        out: dict[str, Any] = {"uriTemplate": self.uri_template, "name": self.name}
        for key, value in (("title", self.title), ("description", self.description),
                           ("mimeType", self.mime_type)):
            if value is not None:
                out[key] = value
        return out


@dataclass
class Prompt:
    name: str
    description: str
    builder: Callable[[RequestContext, dict], Any]
    arguments: list[dict] = field(default_factory=list)
    title: str | None = None
    version: str | None = None

    def to_json(self) -> dict:
        out: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.arguments:
            out["arguments"] = self.arguments
        if self.title:
            out["title"] = self.title
        if self.version:
            out["_meta"] = {"com.meridian/promptVersion": self.version}
        return out


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class Server:
    """A stateless MCP server.

    Subclass it or, more usually, instantiate it and hang handlers off it with
    the decorators. Both styles appear in the book.
    """

    def __init__(
        self,
        name: str,
        version: str = "1.0.0",
        *,
        instructions: str | None = None,
        supported_versions: Iterable[str] | None = None,
        list_changed: bool = False,
        subscribe: bool = False,
        page_size: int = DEFAULT_PAGE_SIZE,
        tools_ttl_ms: int = 300_000,
        tools_cache_scope: str = "public",
        discover_ttl_ms: int = 3_600_000,
    ):
        self.info = Implementation(name=name, version=version)
        self.instructions = instructions
        self.supported_versions = list(supported_versions or SUPPORTED_VERSIONS)
        self.page_size = page_size
        self.tools_ttl_ms = tools_ttl_ms
        self.tools_cache_scope = tools_cache_scope
        self.discover_ttl_ms = discover_ttl_ms
        self.list_changed = list_changed
        self.subscribe = subscribe

        self._tools: dict[str, Tool] = {}
        self._resources: dict[str, Resource] = {}
        self._templates: list[ResourceTemplate] = []
        self._prompts: dict[str, Prompt] = {}
        self._extensions: dict[str, dict] = {}
        self._completers: dict[tuple[str, str, str], Callable] = {}
        self._methods: dict[str, Callable[[RequestContext], Any]] = {}
        self._subscribers: list[Any] = []
        self._lock = threading.RLock()

        # Cheap built-in counters. Chapter 16 replaces these with real spans.
        self.call_count: dict[str, int] = {}
        self.total_ms: dict[str, float] = {}

        self._register_core_methods()

    # -- registration -------------------------------------------------------

    def tool(self, name: str, description: str, input_schema: dict, **kw):
        """Decorator form: `@server.tool("name", "what it does", schema)`."""
        def deco(fn):
            self.add_tool(Tool(name=name, description=description,
                               input_schema=input_schema, handler=fn, **kw))
            return fn
        return deco

    def add_tool(self, tool: Tool) -> Tool:
        if not TOOL_NAME_RE.match(tool.name):
            raise ValueError(
                f"tool name {tool.name!r} should match [A-Za-z0-9_.-]{{1,128}}"
            )
        with self._lock:
            self._tools[tool.name] = tool
        return tool

    def resource(self, uri: str, name: str, **kw):
        def deco(fn):
            self.add_resource(Resource(uri=uri, name=name, reader=fn, **kw))
            return fn
        return deco

    def add_resource(self, resource: Resource) -> Resource:
        with self._lock:
            self._resources[resource.uri] = resource
        return resource

    def template(self, uri_template: str, name: str, **kw):
        def deco(fn):
            self.add_template(ResourceTemplate(
                uri_template=uri_template, name=name, reader=fn, **kw))
            return fn
        return deco

    def add_template(self, tmpl: ResourceTemplate) -> ResourceTemplate:
        with self._lock:
            self._templates.append(tmpl)
        return tmpl

    def prompt(self, name: str, description: str, **kw):
        def deco(fn):
            self.add_prompt(Prompt(name=name, description=description,
                                   builder=fn, **kw))
            return fn
        return deco

    def add_prompt(self, prompt: Prompt) -> Prompt:
        with self._lock:
            self._prompts[prompt.name] = prompt
        return prompt

    def completer(self, ref_type: str, ref_name: str, argument: str):
        """Register an argument completer.

        `ref_type` is "prompt" or "resource"; `ref_name` is the prompt name or
        the URI template. The callable receives the partial value the user has
        typed and the arguments already filled in, and returns candidates.
        """
        def deco(fn):
            self._completers[(ref_type, ref_name, argument)] = fn
            return fn
        return deco

    def declare_extension(self, ident: str, settings: dict | None = None) -> None:
        """Advertise an extension. Declaring one you do not implement is lying,
        and the client will believe you."""
        self._extensions[ident] = settings or {}

    def method(self, name: str):
        """Register a raw RPC method. Extensions use this."""
        def deco(fn):
            self._methods[name] = fn
            return fn
        return deco

    # -- capabilities -------------------------------------------------------

    def capabilities(self) -> ServerCapabilities:
        caps = ServerCapabilities(extensions=dict(self._extensions))
        if self._tools:
            caps.tools = {"listChanged": True} if self.list_changed else {}
        if self._resources or self._templates:
            res: dict[str, Any] = {}
            if self.list_changed:
                res["listChanged"] = True
            if self.subscribe:
                res["subscribe"] = True
            caps.resources = res
        if self._prompts:
            caps.prompts = {"listChanged": True} if self.list_changed else {}
        if self._completers:
            caps.completions = {}
        return caps

    # -- core methods -------------------------------------------------------

    def _register_core_methods(self) -> None:
        self._methods.update({
            "server/discover": self._discover,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "resources/list": self._resources_list,
            "resources/templates/list": self._templates_list,
            "resources/read": self._resources_read,
            "prompts/list": self._prompts_list,
            "prompts/get": self._prompts_get,
            "completion/complete": self._completion_complete,
        })

    def _discover(self, ctx: RequestContext) -> dict:
        out: dict[str, Any] = {
            "supportedVersions": list(self.supported_versions),
            "capabilities": self.capabilities().to_json(),
            "ttlMs": self.discover_ttl_ms,
            "cacheScope": "public",
        }
        if self.instructions:
            out["instructions"] = self.instructions
        return out

    # -- pagination ---------------------------------------------------------

    def _paginate(self, items: list, cursor: str | None) -> tuple[list, str | None]:
        """Opaque cursors over a stable ordering.

        The cursor is an offset here because the catalogue is small and stable.
        A server paginating over a mutable database should encode a sort key
        instead, because offsets skip and duplicate rows when the underlying
        set shifts between pages.
        """
        start = 0
        if cursor:
            try:
                start = int(cursor)
            except ValueError:
                raise errors.InvalidParams("Invalid cursor") from None
            if start < 0 or start > len(items):
                raise errors.InvalidParams("Invalid cursor")
        page = items[start:start + self.page_size]
        nxt = str(start + self.page_size) if start + self.page_size < len(items) else None
        return page, nxt

    # -- tools --------------------------------------------------------------

    def visible_tools(self, ctx: RequestContext) -> list[Tool]:
        """Override to filter by scope. The set may vary by authorization,
        never by connection."""
        return sorted(self._tools.values(), key=lambda t: t.name)

    def _tools_list(self, ctx: RequestContext) -> dict:
        tools = self.visible_tools(ctx)
        page, nxt = self._paginate(tools, ctx.params.get("cursor"))
        out: dict[str, Any] = {
            "tools": [t.to_json() for t in page],
            "ttlMs": self.tools_ttl_ms,
            "cacheScope": self.tools_cache_scope,
        }
        if nxt:
            out["nextCursor"] = nxt
        return out

    def _tools_call(self, ctx: RequestContext) -> dict:
        name = ctx.params.get("name")
        if not isinstance(name, str):
            raise errors.InvalidParams("tools/call requires a string `name`")
        tool = self._tools.get(name)
        if tool is None:
            raise errors.InvalidParams(f"Unknown tool: {name}")

        # An argument that fails the tool's own inputSchema is a *tool execution*
        # error, not a protocol error (SEP-1303). The distinction is the whole
        # point: protocol errors are caught by the client and never reach the
        # model, so a model that sent a bad date can never learn that it did.
        # Returning isError puts the message in the context window, where it can
        # be read and corrected on the next turn.
        #
        # Protocol errors stay protocol errors for the things a model cannot fix
        # by changing arguments: an unknown tool, or a malformed CallToolRequest.
        try:
            validate_against_schema(ctx.arguments, tool.input_schema,
                                    where=f"{name} arguments")
        except errors.McpError as exc:
            return normalise_tool_result(tool_error(exc.message), tool)

        result = tool.handler(ctx)
        if inspect.isawaitable(result):
            raise errors.InternalError("async tool handlers are not supported here")
        return normalise_tool_result(result, tool)

    # -- resources ----------------------------------------------------------

    def visible_resources(self, ctx: RequestContext) -> list[Resource]:
        return sorted(self._resources.values(), key=lambda r: r.uri)

    def _resources_list(self, ctx: RequestContext) -> dict:
        items = self.visible_resources(ctx)
        page, nxt = self._paginate(items, ctx.params.get("cursor"))
        out: dict[str, Any] = {
            "resources": [r.to_json() for r in page],
            "ttlMs": 300_000,
            "cacheScope": "private",
        }
        if nxt:
            out["nextCursor"] = nxt
        return out

    def _templates_list(self, ctx: RequestContext) -> dict:
        page, nxt = self._paginate(list(self._templates), ctx.params.get("cursor"))
        out: dict[str, Any] = {
            "resourceTemplates": [t.to_json() for t in page],
            "ttlMs": 600_000,
            "cacheScope": "public",
        }
        if nxt:
            out["nextCursor"] = nxt
        return out

    def _resources_read(self, ctx: RequestContext) -> dict:
        uri = ctx.params.get("uri")
        if not isinstance(uri, str):
            raise errors.InvalidParams("resources/read requires a string `uri`")

        resource = self._resources.get(uri)
        if resource is not None:
            payload = resource.reader(ctx, uri)
            return finish_read(payload, uri, resource.mime_type,
                               resource.ttl_ms, resource.cache_scope)

        for tmpl in self._templates:
            params = tmpl.match(uri)
            if params is not None:
                payload = tmpl.reader(ctx, uri, params)
                return finish_read(payload, uri, tmpl.mime_type or "text/plain",
                                   tmpl.ttl_ms, tmpl.cache_scope)

        raise errors.ResourceNotFound(uri)

    # -- prompts ------------------------------------------------------------

    def _prompts_list(self, ctx: RequestContext) -> dict:
        items = sorted(self._prompts.values(), key=lambda p: p.name)
        page, nxt = self._paginate(items, ctx.params.get("cursor"))
        out: dict[str, Any] = {
            "prompts": [p.to_json() for p in page],
            "ttlMs": 600_000,
            "cacheScope": "public",
        }
        if nxt:
            out["nextCursor"] = nxt
        return out

    def _prompts_get(self, ctx: RequestContext) -> dict:
        name = ctx.params.get("name")
        prompt = self._prompts.get(name) if isinstance(name, str) else None
        if prompt is None:
            raise errors.InvalidParams(f"Unknown prompt: {name}")
        args = ctx.params.get("arguments") or {}
        for spec in prompt.arguments:
            if spec.get("required") and spec["name"] not in args:
                raise errors.InvalidParams(f"Missing required argument: {spec['name']}")
        result = prompt.builder(ctx, args)
        if isinstance(result, dict) and "messages" in result:
            return result
        return {"description": prompt.description, "messages": result}

    # -- completion ---------------------------------------------------------

    MAX_COMPLETION_VALUES = 100

    def _completion_complete(self, ctx: RequestContext) -> dict:
        """Argument autocompletion for prompts and resource templates.

        The response caps `values` at 100 by protocol rule and reports `total`
        and `hasMore` separately, so a client can say "showing 100 of 4,312"
        rather than implying the list is complete.
        """
        ref = ctx.params.get("ref") or {}
        argument = ctx.params.get("argument") or {}
        ref_type = str(ref.get("type", ""))
        name = argument.get("name")
        if ref_type not in ("ref/prompt", "ref/resource") or not name:
            raise errors.InvalidParams(
                "completion/complete needs a ref/prompt or ref/resource and an "
                "argument name")

        key = (ref_type.split("/", 1)[1],
               ref.get("name") if ref_type == "ref/prompt" else ref.get("uri"),
               name)
        completer = self._completers.get(key)
        if completer is None:
            # An unknown argument is not an error. A client asks about
            # everything the user types, and a server that raises here turns
            # ordinary typing into a stream of error dialogs.
            return {"completion": {"values": [], "total": 0, "hasMore": False}}

        # Previously-filled arguments, so a completer can narrow on them.
        context = (ctx.params.get("context") or {}).get("arguments") or {}
        values = list(completer(str(argument.get("value", "")), context))
        page = values[:self.MAX_COMPLETION_VALUES]
        return {"completion": {
            "values": page,
            "total": len(values),
            "hasMore": len(values) > len(page),
        }}

    # -- dispatch -----------------------------------------------------------

    def handle(self, message: dict, *, auth: dict | None = None,
               emit_progress: Callable | None = None) -> dict | None:
        """Handle one parsed JSON-RPC message. Returns None for notifications.

        This is the whole server, and it is a pure function of its arguments.
        Everything that looks like state (the catalogue) is fixed at
        construction time, so two replicas given the same message produce the
        same bytes. That property is what makes round-robin load balancing
        sufficient and sticky sessions unnecessary.

        `emit_progress` is supplied by the transport, because only the transport
        knows where a notification should go: on Streamable HTTP it is the
        response stream for this request, on stdio it is the one shared pipe.
        """
        try:
            jsonrpc.validate_request(message)
        except errors.McpError as exc:
            return jsonrpc.error_response(message.get("id"), exc)

        method = message["method"]
        params = message.get("params") or {}
        req_id = message.get("id")

        if jsonrpc.is_notification(message):
            self.handle_notification(method, params)
            return None

        started = time.perf_counter()
        try:
            ctx = parse_request_context(
                method, params, request_id=req_id, auth=auth,
                supported_versions=self.supported_versions,
            )
            ctx.emit_progress = emit_progress
            handler = self._methods.get(method)
            if handler is None:
                raise errors.MethodNotFound(method)

            result = handler(ctx)
            if not isinstance(result, dict):
                raise errors.InternalError(
                    f"handler for {method} returned {type(result).__name__}, not a dict"
                )
            result.setdefault("resultType", jsonrpc.RESULT_COMPLETE)
            meta = result.setdefault("_meta", {})
            meta.setdefault(KEY_SERVER_INFO, self.info.to_json())
            return jsonrpc.result_response(req_id, result)

        except errors.McpError as exc:
            return jsonrpc.error_response(req_id, exc)
        except Exception as exc:  # a handler bug must not take the process down
            return jsonrpc.error_response(
                req_id, errors.InternalError(f"{type(exc).__name__}: {exc}")
            )
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            self.call_count[method] = self.call_count.get(method, 0) + 1
            self.total_ms[method] = self.total_ms.get(method, 0.0) + elapsed

    def handle_notification(self, method: str, params: dict) -> None:
        """Client-to-server notifications. The core protocol defines one:
        `notifications/cancelled`, and only on stdio."""
        return None

    # -- server-initiated change notifications ------------------------------

    def attach_subscriber(self, sink) -> None:
        with self._lock:
            self._subscribers.append(sink)

    def detach_subscriber(self, sink) -> None:
        with self._lock:
            if sink in self._subscribers:
                self._subscribers.remove(sink)

    def notify_list_changed(self, kind: str) -> int:
        """Fan a list-changed notification out to opted-in listeners.

        `kind` is one of tools, prompts, resources. Only subscribers that asked
        for this notification type receive it; the specification forbids sending
        types the client did not request.
        """
        method = f"notifications/{kind}/list_changed"
        filter_key = {
            "tools": "toolsListChanged",
            "prompts": "promptsListChanged",
            "resources": "resourcesListChanged",
        }[kind]
        sent = 0
        with self._lock:
            sinks = list(self._subscribers)
        for sink in sinks:
            if sink.wants(filter_key):
                sink.send(jsonrpc.Notification(method, {}).to_json())
                sent += 1
        return sent

    def notify_task(self, task_json: dict) -> int:
        """Push a task's state to subscribers who asked for task updates.

        The notification carries the whole task rather than just the id, which
        saves the `tasks/get` the client would otherwise send on being told
        something changed. That is the entire reason to prefer this over
        polling: one message instead of a message plus a round trip.
        """
        sent = 0
        with self._lock:
            sinks = list(self._subscribers)
        for sink in sinks:
            if sink.wants("tasks"):
                sink.send(jsonrpc.Notification(
                    "notifications/tasks", {"task": task_json}).to_json())
                sent += 1
        return sent

    def notify_resource_updated(self, uri: str) -> int:
        sent = 0
        with self._lock:
            sinks = list(self._subscribers)
        for sink in sinks:
            if sink.wants_uri(uri):
                sink.send(jsonrpc.Notification(
                    "notifications/resources/updated", {"uri": uri}).to_json())
                sent += 1
        return sent

    def stats(self) -> dict:
        return {
            "calls": dict(self.call_count),
            "meanMs": {
                m: round(self.total_ms[m] / self.call_count[m], 3)
                for m in self.call_count if self.call_count[m]
            },
        }


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def text_result(text: str, *, structured: Any = None, is_error: bool = False) -> dict:
    out: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }
    if structured is not None:
        out["structuredContent"] = structured
    return out


def tool_error(message: str) -> dict:
    """A *tool execution* error: the model sees it and can try again.

    This is not an McpError. Raising one of those means "the request was
    malformed", which the model usually cannot fix. Returning one of these
    means "your arguments were wrong in a way I can describe", which it often
    can. Getting this distinction backwards is the single most common way to
    turn a recoverable situation into a dead task.
    """
    return text_result(message, is_error=True)


def normalise_tool_result(result: Any, tool: Tool) -> dict:
    """Let handlers return a string, a dict, or a full result envelope."""
    if isinstance(result, str):
        return text_result(result)
    if not isinstance(result, dict):
        raise errors.InternalError(
            f"tool {tool.name} returned {type(result).__name__}"
        )
    # Already an InputRequiredResult or a full envelope: pass it through.
    if "resultType" in result or "content" in result:
        return result
    # A bare dict is structured content. Mirror it into text for the model,
    # per the backwards-compatibility guidance in the spec.
    import json as _json
    return text_result(_json.dumps(result, separators=(",", ":")), structured=result)


def finish_read(payload: Any, uri: str, mime: str, ttl_ms: int, scope: str) -> dict:
    """Shape a resource read, honouring an MRTR interruption if the reader raised one."""
    if isinstance(payload, dict) and payload.get("resultType") == jsonrpc.RESULT_INPUT_REQUIRED:
        return payload
    if isinstance(payload, dict) and "contents" in payload:
        payload.setdefault("ttlMs", ttl_ms)
        payload.setdefault("cacheScope", scope)
        return payload
    if isinstance(payload, bytes):
        import base64
        entry = {"uri": uri, "mimeType": mime,
                 "blob": base64.b64encode(payload).decode("ascii")}
    else:
        entry = {"uri": uri, "mimeType": mime, "text": str(payload)}
    return {"contents": [entry], "ttlMs": ttl_ms, "cacheScope": scope}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_TYPES = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "object": dict, "array": list, "null": type(None),
}


def validate_against_schema(value: Any, schema: dict, *, where: str = "value",
                            depth: int = 0) -> None:
    """A small JSON Schema 2020-12 subset: enough to catch real mistakes.

    Deliberately bounded. The specification warns that composition keywords and
    `$defs` let a hostile schema act as a denial-of-service vector against the
    validator, and asks implementations to cap depth or subschema count. Here
    the cap is a depth of 12, which no honest tool schema comes close to.

    `$ref` to a network URI is not dereferenced. Ever. That is a specification
    requirement, and it exists because a tool catalogue that fetches remote
    schemas is a server-side request forgery primitive wearing a bow tie.
    """
    if depth > 12:
        raise errors.InvalidParams(f"{where}: schema nesting exceeds the depth limit")
    if not isinstance(schema, dict):
        return

    if "$ref" in schema and str(schema["$ref"]).startswith(("http://", "https://")):
        raise errors.InvalidParams(f"{where}: remote $ref is not dereferenced")

    expected = schema.get("type")
    if isinstance(expected, str) and expected in _TYPES:
        py = _TYPES[expected]
        # JSON has no separate integer type, and bool is an int in Python.
        if expected == "integer" and isinstance(value, bool):
            raise errors.InvalidParams(f"{where}: expected integer, got boolean")
        if expected == "number" and isinstance(value, bool):
            raise errors.InvalidParams(f"{where}: expected number, got boolean")
        if not isinstance(value, py):
            raise errors.InvalidParams(
                f"{where}: expected {expected}, got {type(value).__name__}"
            )

    if isinstance(value, dict) and schema.get("type") == "object":
        for name in schema.get("required", []):
            if name not in value:
                raise errors.InvalidParams(f"{where}: missing required field {name!r}")
        props = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                raise errors.InvalidParams(
                    f"{where}: unexpected field(s) {', '.join(sorted(extra))}"
                )
        for name, sub in props.items():
            if name in value:
                validate_against_schema(value[name], sub,
                                        where=f"{where}.{name}", depth=depth + 1)

    if isinstance(value, list) and schema.get("items"):
        for i, item in enumerate(value):
            validate_against_schema(item, schema["items"],
                                    where=f"{where}[{i}]", depth=depth + 1)

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        raise errors.InvalidParams(f"{where}: must be one of {allowed}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise errors.InvalidParams(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            raise errors.InvalidParams(f"{where}: must be <= {schema['maximum']}")

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise errors.InvalidParams(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise errors.InvalidParams(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise errors.InvalidParams(f"{where}: does not match {schema['pattern']}")
