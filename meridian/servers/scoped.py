"""Access control and cross-call state, as working code.

Two patterns the book describes and that deserve to exist rather than be
sketched:

  ScopedRiskServer   tool visibility filtered by the caller's scopes, plus the
                     handler-side check that actually enforces it
  build_basket_server  the handle pattern: an identifier minted by the server,
                     carried by the model, authorized on every use

The distinction the first one exists to make: filtering the catalogue is a
menu, and the check in the handler is the lock. A caller can invoke a tool that
was never listed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from ..protocol import RequestContext, Server, Tool, text_result, tool_error
from ..protocol import errors
from .data import ACCOUNTS
from .risk import build_server as build_risk_server

# Which scope each tool requires. Tools absent from this map need none.
REQUIRED_SCOPE = {
    "assess_account_risk": "risk:read",
    "rank_portfolio_risk": "risk:read",
    "underwrite_loan": "risk:write",
}


class ScopedRiskServer(Server):
    """The risk server, with per-tool scopes and row-level account access."""

    def visible_tools(self, ctx: RequestContext) -> list[Tool]:
        """Override to filter by scope. The set may vary by authorization,
        never by connection."""
        scopes = set((ctx.auth or {}).get("scopes", []))
        return [t for t in super().visible_tools(ctx)
                if not REQUIRED_SCOPE.get(t.name)
                or REQUIRED_SCOPE[t.name] in scopes]

    def authorized(self, ctx: RequestContext, tool_name: str) -> bool:
        """The lock, as opposed to the menu.

        `visible_tools` hides a tool the caller may not use. Nothing stops that
        caller sending `tools/call` for it anyway, so the handler checks too.
        """
        required = REQUIRED_SCOPE.get(tool_name)
        if required is None:
            return True
        return required in set((ctx.auth or {}).get("scopes", []))


def accounts_visible_to(auth: dict | None) -> set[str]:
    """Row-level security: which accounts this principal may see at all.

    Derived from the credential on the request. Never from an argument, because
    an argument is a suggestion from a model that has read attacker-influenced
    text.
    """
    auth = auth or {}
    if "risk:all-accounts" in set(auth.get("scopes", [])):
        return set(ACCOUNTS)
    return set(auth.get("accounts", []))


def can_read_account(auth: dict | None, account_id: str) -> bool:
    return account_id in accounts_visible_to(auth)


def build_scoped_server(**kw) -> ScopedRiskServer:
    """A risk server that enforces scopes and row-level access.

    Built by copying the ordinary risk server's registrations onto a subclass,
    so the two cannot drift apart as tools are added.
    """
    base = build_risk_server(**kw)
    scoped = ScopedRiskServer(
        base.info.name, base.info.version,
        instructions=base.instructions,
        list_changed=base.list_changed,
        subscribe=base.subscribe,
    )
    for tool in base._tools.values():
        scoped.add_tool(_guard(tool))
    for resource in base._resources.values():
        scoped.add_resource(resource)
    for template in base._templates:
        scoped.add_template(template)
    for prompt in base._prompts.values():
        scoped.add_prompt(prompt)
    return scoped


def _guard(tool: Tool) -> Tool:
    """Wrap a handler so authorization is checked before it runs."""
    inner = tool.handler

    def handler(ctx: RequestContext):
        required = REQUIRED_SCOPE.get(tool.name)
        if required and required not in set((ctx.auth or {}).get("scopes", [])):
            # Deliberately the same shape as "unknown tool" would produce, so
            # the error does not confirm which tools exist.
            raise errors.InvalidParams(f"Unknown tool: {tool.name}")

        account_id = (ctx.arguments or {}).get("accountId")
        if account_id and not can_read_account(ctx.auth, account_id):
            # Same error as "does not exist". Distinguishing them tells an
            # attacker which account ids are real.
            return tool_error(f"No account {account_id}.")
        return inner(ctx)

    return Tool(name=tool.name, description=tool.description,
                input_schema=tool.input_schema, handler=handler,
                title=tool.title, output_schema=tool.output_schema,
                annotations=tool.annotations, icons=tool.icons,
                ui_resource_uri=tool.ui_resource_uri)


# ---------------------------------------------------------------------------
# Cross-call state, without sessions
# ---------------------------------------------------------------------------

BASKET_TTL_SECONDS = 24 * 3600


@dataclass
class Basket:
    basket_id: str
    owner: str | None
    items: list[str] = field(default_factory=list)
    touched_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.touched_at > BASKET_TTL_SECONDS


BASKETS: dict[str, Basket] = {}


def build_basket_server() -> Server:
    """The handle pattern from Chapter 2, as running code.

    The protocol has no concept of a handle. `basket_id` is an ordinary string
    argument, which is exactly why this works on every client and transport
    with no negotiation.
    """
    server = Server("meridian-basket", "1.0.0", instructions=(
        "A worked example of cross-call state without sessions. Create a "
        "basket, then pass its id to every later call."
    ))

    @server.tool(
        "create_basket",
        "Create a shopping basket and return its id. Baskets expire after 24 "
        "hours of inactivity; pass the id to add_item and checkout.",
        {"type": "object", "additionalProperties": False},
    )
    def create_basket(ctx: RequestContext):
        # Opaque and high-entropy: a handle that encodes structure invites
        # somebody to guess the neighbouring one.
        basket = Basket(basket_id="bsk_" + uuid.uuid4().hex[:12],
                        owner=(ctx.auth or {}).get("sub"))
        BASKETS[basket.basket_id] = basket
        return text_result(f"Created basket {basket.basket_id}.",
                           structured={"basket_id": basket.basket_id})

    @server.tool(
        "add_item",
        "Add an item to an open basket.",
        {
            "type": "object",
            "properties": {
                "basket_id": {"type": "string", "pattern": "^bsk_[a-f0-9]{12}$"},
                "sku": {"type": "string", "minLength": 1},
            },
            "required": ["basket_id", "sku"],
        },
    )
    def add_item(ctx: RequestContext):
        basket_id = ctx.arguments["basket_id"]
        basket = BASKETS.get(basket_id)

        # A handle names an object. It does not grant access to one, so the
        # caller's authorization is re-checked here on every single call.
        if basket is None or basket.expired or not _owns(ctx, basket):
            return tool_error(
                f"Basket {basket_id} does not exist or has expired. "
                "Create a new one with create_basket.")

        basket.items.append(ctx.arguments["sku"])
        basket.touched_at = time.time()
        return text_result(
            f"Basket {basket_id} now holds {len(basket.items)} item(s).",
            structured={"basket_id": basket_id, "itemCount": len(basket.items)})

    return server


def _owns(ctx: RequestContext, basket: Basket) -> bool:
    """For an authenticated server a handle is a name, not a capability.

    When there is no authentication the handle is necessarily a bearer token,
    which is why `create_basket` gives it real entropy.
    """
    if basket.owner is None:
        return True
    return basket.owner == (ctx.auth or {}).get("sub")
