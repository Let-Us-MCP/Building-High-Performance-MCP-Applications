"""The agent loop, instrumented.

Every iteration is timed and decomposed the same way, because that decomposition
is the book's central diagram: think, route, execute, feed back. If you cannot
attribute a millisecond to one of those four bands, you cannot optimise it.

The loop also carries the guardrails, which is where they belong. Step budgets,
token budgets, and dollar budgets are the host's job. A model cannot be trusted
to stop, and a server cannot see enough to make it.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .host import Host
from .model import StubModel, Turn, estimate_tokens


@dataclass
class Iteration:
    """One trip round the loop, with the milliseconds attributed."""
    index: int
    model_ms: float = 0.0
    transport_ms: float = 0.0
    consent_ms: float = 0.0
    tool_calls: int = 0
    parallel: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.model_ms + self.transport_ms + self.consent_ms

    def to_json(self) -> dict:
        return {
            "i": self.index,
            "modelMs": round(self.model_ms, 2),
            "transportMs": round(self.transport_ms, 2),
            "consentMs": round(self.consent_ms, 2),
            "totalMs": round(self.total_ms, 2),
            "toolCalls": self.tool_calls,
            "parallel": self.parallel,
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "costUsd": round(self.cost_usd, 6),
        }


@dataclass
class LoopResult:
    answer: str
    iterations: list[Iteration] = field(default_factory=list)
    stopped_because: str = "completed"
    context_tokens: int = 0

    @property
    def wall_ms(self) -> float:
        return sum(i.total_ms for i in self.iterations)

    @property
    def model_ms(self) -> float:
        return sum(i.model_ms for i in self.iterations)

    @property
    def transport_ms(self) -> float:
        return sum(i.transport_ms for i in self.iterations)

    @property
    def total_tokens(self) -> int:
        return sum(i.input_tokens + i.output_tokens for i in self.iterations)

    @property
    def cost_usd(self) -> float:
        return sum(i.cost_usd for i in self.iterations)

    @property
    def round_trips(self) -> int:
        return sum(i.tool_calls for i in self.iterations)

    def to_json(self) -> dict:
        return {
            "stoppedBecause": self.stopped_because,
            "iterations": len(self.iterations),
            "roundTrips": self.round_trips,
            "wallMs": round(self.wall_ms, 1),
            "modelMs": round(self.model_ms, 1),
            "transportMs": round(self.transport_ms, 1),
            "modelSharePct": round(100.0 * self.model_ms / self.wall_ms, 1) if self.wall_ms else 0,
            "totalTokens": self.total_tokens,
            "costUsd": round(self.cost_usd, 6),
            "steps": [i.to_json() for i in self.iterations],
        }


class AgentLoop:
    """Think, act, observe, repeat, stop.

    The stopping conditions are the interesting part. There are four, and a
    production loop needs all of them:

      max_steps      the model is looping and has stopped making progress
      token_budget   the context is filling with tool output nobody will read
      cost_budget    somebody wired this to a cron job and went on holiday
      no tool calls  the model is finished, which is the one you hope for
    """

    def __init__(self, host: Host, model: StubModel, *,
                 max_steps: int = 8,
                 token_budget: int | None = None,
                 cost_budget_usd: float | None = None,
                 parallel_fanout: bool = True,
                 on_step: Callable[[Iteration], None] | None = None):
        self.host = host
        self.model = model
        self.max_steps = max_steps
        self.token_budget = token_budget
        self.cost_budget_usd = cost_budget_usd
        self.parallel_fanout = parallel_fanout
        self.on_step = on_step

    def run(self, goal: str) -> LoopResult:
        catalogue = self.host.catalogue()
        context = [goal]
        result = LoopResult(answer="")

        for step in range(self.max_steps):
            iteration = Iteration(index=step)

            # --- think
            started = time.perf_counter()
            turn = self.model.think("\n".join(context), catalogue)
            iteration.model_ms = turn.latency_ms
            iteration.input_tokens = turn.input_tokens
            iteration.output_tokens = turn.output_tokens
            iteration.cost_usd = turn.cost_usd

            if not turn.tool_calls:
                result.answer = turn.text
                result.iterations.append(iteration)
                self._emit(iteration)
                result.stopped_because = "completed"
                break

            # --- act
            iteration.tool_calls = len(turn.tool_calls)
            iteration.parallel = self.parallel_fanout and len(turn.tool_calls) > 1

            started = time.perf_counter()
            if iteration.parallel:
                results = self.host.call_tools_parallel(turn.tool_calls)
            else:
                results = [
                    self.host.call_tool(c["name"], c.get("arguments"))
                    for c in turn.tool_calls
                ]
            iteration.transport_ms = (time.perf_counter() - started) * 1000.0

            # --- observe
            for call, payload in zip(turn.tool_calls, results):
                context.append(f"[{call['name']}] {_summarise(payload)}")

            result.iterations.append(iteration)
            self._emit(iteration)

            if self.token_budget is not None and result.total_tokens > self.token_budget:
                result.stopped_because = "token budget exhausted"
                result.answer = "Stopped: the context budget for this task ran out."
                break
            if self.cost_budget_usd is not None and result.cost_usd > self.cost_budget_usd:
                result.stopped_because = "cost budget exhausted"
                result.answer = "Stopped: the dollar budget for this task ran out."
                break
        else:
            result.stopped_because = "step budget exhausted"
            result.answer = "Stopped: too many steps without reaching an answer."

        result.context_tokens = estimate_tokens("\n".join(context))
        return result

    def _emit(self, iteration: Iteration) -> None:
        if self.on_step:
            self.on_step(iteration)


def _summarise(payload: dict, limit: int = 600) -> str:
    """Put the tool result into the context without putting *all* of it there.

    Structured content is preferred over the text mirror, because it is smaller
    and the model does not have to parse prose to find a number. Truncation is
    blunt, and Chapter 13 replaces it with something better, but blunt and
    bounded beats unbounded.
    """
    if payload.get("structuredContent") is not None:
        text = json.dumps(payload["structuredContent"], separators=(",", ":"))
    else:
        text = " ".join(
            block.get("text", "") for block in payload.get("content", [])
            if block.get("type") == "text"
        )
    if payload.get("isError"):
        text = "ERROR: " + text
    return text if len(text) <= limit else text[:limit] + "...[truncated]"
