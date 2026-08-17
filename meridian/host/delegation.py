"""Delegation: budgets, depth limits, and circuit breakers.

An agent that calls another agent is a host on one side and a server on the
other, so nothing here is new protocol. What is new is bookkeeping: four
counters have to travel with a delegated request, or a three-level system
becomes a fork bomb with a token meter attached.

    depth       refuse past a limit
    tokens      remaining, not allocated
    dollars     remaining, not allocated
    traceparent so one trace covers the whole tree

The first three ride in `_meta` under a vendor prefix, because they are ours
rather than the protocol's. `traceparent` is already reserved by the spec.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ..protocol import errors
from ..protocol.meta import RequestContext

# Vendor-prefixed, because the second label is `meridian`, not `mcp`.
DEPTH_KEY = "com.meridian/delegationDepth"
TOKENS_KEY = "com.meridian/tokenBudgetRemaining"
DOLLARS_KEY = "com.meridian/costBudgetRemaining"

MAX_DELEGATION_DEPTH = 3
MAX_STEPS = 8
DEFAULT_TOKEN_BUDGET = 20_000
DEFAULT_COST_BUDGET = 0.10


@dataclass
class Budget:
    """What a delegated call is allowed to spend."""
    depth: int
    tokens: int
    dollars: float
    steps: int

    def to_meta(self) -> dict:
        """The `_meta` fields to attach when delegating further down."""
        return {
            DEPTH_KEY: self.depth,
            TOKENS_KEY: self.tokens,
            DOLLARS_KEY: self.dollars,
        }

    def spend(self, tokens: int, dollars: float) -> "Budget":
        """Return what is left after a child reports back.

        Budgets decrement. Handing each of three children the full remaining
        budget authorises three times the money, which is the mistake this
        method exists to make hard.
        """
        return Budget(
            depth=self.depth,
            tokens=max(0, self.tokens - tokens),
            dollars=max(0.0, self.dollars - dollars),
            steps=self.steps,
        )


def inherit_budget(ctx: RequestContext) -> Budget:
    """Read the caller's remaining budget, or assume the smallest.

    An agent that receives no budget must not assume an unlimited one. That
    default is how a misconfigured caller turns into a four-figure invoice.
    """
    meta = ctx.raw_meta
    depth = int(meta.get(DEPTH_KEY, 0))
    if depth >= MAX_DELEGATION_DEPTH:
        raise errors.InvalidParams(
            f"Delegation depth {depth} exceeds the limit of "
            f"{MAX_DELEGATION_DEPTH}")
    return Budget(
        depth=depth + 1,
        tokens=int(meta.get(TOKENS_KEY, DEFAULT_TOKEN_BUDGET)),
        dollars=float(meta.get(DOLLARS_KEY, DEFAULT_COST_BUDGET)),
        steps=max(1, MAX_STEPS - depth * 2),
    )


class CircuitBreaker:
    """Stop calling something that is clearly broken.

    Three states. Closed: normal. Open: fail fast without calling. Half-open:
    let exactly one probe through and decide from its outcome. The half-open
    state is what stops a recovering service being flattened by every client
    reconnecting at once.

    Failing fast matters more in an agent system than in ordinary RPC. A
    timeout consumes the caller's step budget and returns no information; a
    fast failure leaves the model steps to try something else.
    """

    def __init__(self, *, threshold: int = 5, cooldown: float = 30.0):
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def allows(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if self.opened_at is None:
                return True
            if now - self.opened_at >= self.cooldown:
                # Half-open: one probe gets through. If it fails, the next
                # `record` re-opens immediately, because failures is already
                # one below the threshold.
                self.opened_at = None
                self.failures = self.threshold - 1
                return True
            return False

    def record(self, ok: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if ok:
                self.failures = 0
                self.opened_at = None
            else:
                self.failures += 1
                if self.failures >= self.threshold:
                    self.opened_at = now

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed" if self.failures == 0 else "degraded"
        return "open"


def build_delegating_agent(inner_host, model, *, name: str = "meridian-risk-agent"):
    """An agent exposed as an MCP server.

    The tool below is implemented by running a whole agent loop. From the
    caller's side it is an ordinary tool call, which is the point: the protocol
    has no idea one of its servers is itself a host.
    """
    from ..protocol import Server, text_result
    from .loop import AgentLoop

    agent = Server(name, "1.0.0", instructions=(
        "Delegated credit analysis. Give it an account and a question; it will "
        "consult the risk, fraud, and market-data servers and return a synthesis."
    ))

    @agent.tool(
        "analyse_account",
        "Full credit analysis of one account: risk score, fraud signals, and "
        "indicative pricing, synthesised into a recommendation.",
        {
            "type": "object",
            "properties": {
                "accountId": {"type": "string", "pattern": "^ACC-[0-9]{4}$"},
                "question": {"type": "string",
                             "description": "What the caller wants to know"},
            },
            "required": ["accountId"],
        },
    )
    def analyse_account(ctx: RequestContext):
        budget = inherit_budget(ctx)
        result = AgentLoop(inner_host, model,
                           max_steps=budget.steps,
                           token_budget=budget.tokens,
                           cost_budget_usd=budget.dollars).run(
            f"Analyse {ctx.arguments['accountId']}. "
            f"{ctx.arguments.get('question', '')}")
        return text_result(result.answer,
                           structured={"iterations": len(result.iterations),
                                       "costUsd": round(result.cost_usd, 6),
                                       "depth": budget.depth})

    return agent


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------

PATH_KEY = "com.meridian/delegationPath"


def call_path(ctx: RequestContext) -> list[str]:
    """The agents this request has already passed through, oldest first."""
    raw = ctx.raw_meta.get(PATH_KEY)
    return [str(x) for x in raw] if isinstance(raw, list) else []


def extend_path(ctx: RequestContext, name: str) -> list[str]:
    """Add this agent to the path, refusing if it is already on it.

    A depth limit bounds how bad a cycle gets; it does not detect one. A mesh
    where A calls B calls C calls A stops at depth 3 having done three useless
    delegations and returned a budget error that names the wrong problem. The
    path makes the actual cycle visible in the error message, which is the
    difference between a five-minute diagnosis and an afternoon.
    """
    path = call_path(ctx)
    if name in path:
        raise errors.InvalidParams(
            "Delegation cycle: " + " -> ".join([*path, name]))
    return [*path, name]
