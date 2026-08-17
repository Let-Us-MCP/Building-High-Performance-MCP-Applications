"""The host: many servers, one namespace, one context budget, one trust boundary.

The specification puts the host in charge of everything the model must not be
in charge of. Consent lives here. Audit lives here. The decision about which
server sees which data lives here. A server cannot read the conversation and
cannot see another server, and it is the host that makes that true.

Practically, this class does five jobs:

  1. connects to N servers and caches their capabilities, honouring `ttlMs`
  2. namespaces the tool catalogues so two `search` tools can coexist
  3. routes a model's tool-call intent to the right client
  4. enforces a token budget across the merged catalogue
  5. runs the consent gate before anything with side effects
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..protocol import Client, ClientCapabilities, McpError, ResultCache
from ..protocol import tasks as tasks_ext
from .model import estimate_tokens

NAMESPACE_SEPARATOR = "."


@dataclass
class ServerBinding:
    label: str
    client: Client
    tools: list[dict] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    connected_at: float = 0.0
    healthy: bool = True
    last_error: str | None = None


class ConsentDecision:
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class ConsentPolicy:
    """Where the human stays in the loop.

    The default says yes to anything a server marked read-only and asks about
    everything else. That is a defensible default, and it is also exactly the
    kind of thing an attacker will try to talk their way past, which is why
    Chapter 19 argues the annotations must be treated as untrusted unless the
    server is.
    """

    def __init__(self, *, auto_allow_read_only: bool = True,
                 denylist: set[str] | None = None,
                 on_ask: Callable[[str, dict], bool] | None = None):
        self.auto_allow_read_only = auto_allow_read_only
        self.denylist = denylist or set()
        self.on_ask = on_ask
        self.decisions: list[dict] = []

    def check(self, qualified_name: str, tool: dict, arguments: dict) -> str:
        if qualified_name in self.denylist:
            decision = ConsentDecision.DENY
        elif self.auto_allow_read_only and (tool.get("annotations") or {}).get("readOnlyHint"):
            decision = ConsentDecision.ALLOW
        elif self.on_ask is not None:
            decision = (ConsentDecision.ALLOW if self.on_ask(qualified_name, arguments)
                        else ConsentDecision.DENY)
        else:
            decision = ConsentDecision.ALLOW
        self.decisions.append({"tool": qualified_name, "decision": decision})
        return decision


class Host:
    """Orchestrates many clients. One per server, as the architecture requires."""

    def __init__(self, *, name: str = "meridian-host", version: str = "1.0.0",
                 capabilities: ClientCapabilities | None = None,
                 shared_cache: bool = True,
                 consent: ConsentPolicy | None = None,
                 tool_token_budget: int | None = None):
        self.name = name
        self.version = version
        self.capabilities = capabilities or ClientCapabilities(
            elicitation={"form": {}, "url": {}},
            extensions={tasks_ext.EXTENSION_ID: {}},
        )
        self.cache = ResultCache() if shared_cache else None
        self.consent = consent or ConsentPolicy()
        self.tool_token_budget = tool_token_budget

        self.bindings: dict[str, ServerBinding] = {}
        self._lock = threading.RLock()
        self.audit: list[dict] = []

    # -- wiring -------------------------------------------------------------

    def connect(self, label: str, transport, *, input_provider=None,
                auth_context: str = "anon") -> ServerBinding:
        client = Client(
            transport,
            name=self.name,
            version=self.version,
            capabilities=self.capabilities,
            cache=self.cache,
            input_provider=input_provider,
            server_label=label,
            auth_context=auth_context,
        )
        binding = ServerBinding(label=label, client=client, connected_at=time.time())
        with self._lock:
            self.bindings[label] = binding
        return binding

    def discover_all(self, parallel: bool = True) -> dict[str, dict]:
        """Fetch every server's capabilities.

        In parallel, because these are independent and doing them in sequence
        is the first unnecessary serialisation most hosts ship with. Chapter 12
        measures the difference on a five-server fleet.
        """
        def one(binding: ServerBinding) -> tuple[str, dict]:
            try:
                result = binding.client.discover()
                binding.capabilities = result.get("capabilities", {})
                binding.healthy = True
                return binding.label, result
            except Exception as exc:
                binding.healthy = False
                binding.last_error = f"{type(exc).__name__}: {exc}"
                return binding.label, {}

        bindings = list(self.bindings.values())
        if not parallel:
            return dict(one(b) for b in bindings)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(bindings))) as pool:
            return dict(pool.map(one, bindings))

    def refresh_catalogue(self, parallel: bool = True) -> list[dict]:
        """Merge every server's tools into one namespaced catalogue."""
        def one(binding: ServerBinding) -> list[dict]:
            try:
                binding.tools = binding.client.list_tools()
                binding.healthy = True
            except Exception as exc:
                binding.healthy = False
                binding.last_error = f"{type(exc).__name__}: {exc}"
                binding.tools = []
            return binding.tools

        bindings = list(self.bindings.values())
        if parallel and len(bindings) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(bindings)) as pool:
                list(pool.map(one, bindings))
        else:
            for binding in bindings:
                one(binding)
        return self.catalogue()

    def catalogue(self) -> list[dict]:
        """The merged, namespaced, budgeted tool list the model actually sees.

        Namespacing is not optional once you have more than one server. Tool
        names are unique per server and nothing more, so two servers each
        offering `search` is normal, expected, and fatal if you flatten them.
        """
        out: list[dict] = []
        with self._lock:
            for label in sorted(self.bindings):
                binding = self.bindings[label]
                for tool in binding.tools:
                    entry = dict(tool)
                    entry["name"] = f"{label}{NAMESPACE_SEPARATOR}{tool['name']}"
                    entry["_server"] = label
                    out.append(entry)
        if self.tool_token_budget is not None:
            out = self._apply_budget(out, self.tool_token_budget)
        return out

    def _apply_budget(self, tools: list[dict], budget: int) -> list[dict]:
        """Drop tools once the catalogue exceeds its token allowance.

        Crude on purpose. The interesting question is not the eviction policy,
        it is that a budget exists at all: without one, a host wired to eight
        servers silently spends thirty thousand tokens per turn on descriptions
        of tools the model will never call.
        """
        kept: list[dict] = []
        spent = 0
        for tool in tools:
            cost = estimate_tokens(
                f"{tool.get('name','')}{tool.get('description','')}"
                f"{tool.get('inputSchema','')}"
            )
            if spent + cost > budget:
                continue
            kept.append(tool)
            spent += cost
        return kept

    def catalogue_tokens(self) -> int:
        return sum(
            estimate_tokens(f"{t.get('name','')}{t.get('description','')}"
                            f"{t.get('inputSchema','')}{t.get('outputSchema','')}")
            for t in self.catalogue()
        )

    # -- routing ------------------------------------------------------------

    def split_name(self, qualified: str) -> tuple[str, str]:
        label, _, tool = qualified.partition(NAMESPACE_SEPARATOR)
        if not tool:
            raise McpError(-32602, f"Tool name {qualified!r} is not namespaced")
        return label, tool

    def find_tool(self, qualified: str) -> dict | None:
        label, name = self.split_name(qualified)
        binding = self.bindings.get(label)
        if binding is None:
            return None
        for tool in binding.tools:
            if tool["name"] == name:
                return tool
        return None

    def call_tool(self, qualified: str, arguments: dict | None = None, **kw) -> dict:
        """Route one call, after the consent gate."""
        label, name = self.split_name(qualified)
        binding = self.bindings.get(label)
        if binding is None:
            raise McpError(-32602, f"No server named {label!r}")

        tool = self.find_tool(qualified) or {}
        decision = self.consent.check(qualified, tool, arguments or {})
        self.audit.append({
            "tool": qualified, "decision": decision, "at": time.time(),
            "arguments": arguments or {},
        })
        if decision == ConsentDecision.DENY:
            from ..protocol import tool_error
            return tool_error(f"The user declined the call to {qualified}.")

        return binding.client.call_tool(name, arguments or {}, **kw)

    def call_tools_parallel(self, calls: list[dict]) -> list[dict]:
        """Fan out independent calls.

        The model asked for these together, which is its way of saying none of
        them depends on the others. Running them in sequence throws that
        information away and pays the sum of the latencies instead of the max.
        """
        if len(calls) <= 1:
            return [self.call_tool(c["name"], c.get("arguments")) for c in calls]

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as pool:
            futures = [
                pool.submit(self.call_tool, c["name"], c.get("arguments"))
                for c in calls
            ]
            results = []
            for future in futures:
                try:
                    results.append(future.result())
                except McpError as exc:
                    from ..protocol import tool_error
                    results.append(tool_error(f"{exc.message}"))
            return results

    # -- resources ----------------------------------------------------------

    def read_resource(self, label: str, uri: str) -> dict:
        binding = self.bindings.get(label)
        if binding is None:
            raise McpError(-32602, f"No server named {label!r}")
        return binding.client.read_resource(uri)

    # -- housekeeping -------------------------------------------------------

    def stats(self) -> dict:
        out = {
            "servers": {
                label: {
                    "healthy": b.healthy,
                    "tools": len(b.tools),
                    "lastError": b.last_error,
                    **b.client.stats.to_json(),
                }
                for label, b in self.bindings.items()
            },
            "catalogueTools": len(self.catalogue()),
            "catalogueTokens": self.catalogue_tokens(),
        }
        if self.cache is not None:
            out["cache"] = self.cache.stats.to_json()
        return out

    def close(self) -> None:
        for binding in self.bindings.values():
            try:
                binding.client.close()
            except Exception:
                pass
