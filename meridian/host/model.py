"""A deterministic stand-in for the model.

A book cannot ship a reproducible LLM. It can ship something with an LLM's
*shape*: it takes a catalogue and a goal, it emits tool-call intents, it reads
results, and it eventually stops. That is enough to measure everything the
protocol is responsible for.

What is simulated, and honestly labelled as such:

  * inference latency, drawn from a fixed distribution
  * token accounting, using a character-based estimator

What is real, and measured rather than modelled:

  * serialisation cost
  * transport time
  * server execution time
  * cache hit rates
  * round-trip counts

Chapter 1 states the distribution up front so no reader mistakes the simulated
part for a measurement.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

# Latency model, in milliseconds. Calibrated against published time-to-first-token
# figures for mid-size hosted models in mid-2026. Stated so you can disagree with it.
TTFT_MEAN_MS = 340.0
TTFT_STDEV_MS = 90.0
MS_PER_OUTPUT_TOKEN = 7.5

# Roughly four characters per token for English prose and JSON alike. Wrong in
# the third decimal place, right enough to compare a before against an after.
CHARS_PER_TOKEN = 4.0

# Representative mid-2026 pricing, US dollars per million tokens.
INPUT_USD_PER_MTOK = 3.00
OUTPUT_USD_PER_MTOK = 15.00


def estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * INPUT_USD_PER_MTOK / 1_000_000.0
            + output_tokens * OUTPUT_USD_PER_MTOK / 1_000_000.0)


@dataclass
class Turn:
    """One model turn: what it cost and what it decided."""
    input_tokens: int
    output_tokens: int
    latency_ms: float
    tool_calls: list[dict] = field(default_factory=list)
    text: str = ""

    @property
    def cost_usd(self) -> float:
        return estimate_cost_usd(self.input_tokens, self.output_tokens)


class StubModel:
    """A planner that follows a script, with an LLM's cost profile.

    Deterministic given a seed, which is the only way the book's before/after
    numbers can mean anything.
    """

    def __init__(self, plan: list[list[dict]] | None = None, *,
                 seed: int = 7, simulate_latency: bool = True,
                 selection_accuracy: float = 1.0):
        self.plan = plan or []
        self.rng = random.Random(seed)
        self.simulate_latency = simulate_latency
        self.selection_accuracy = selection_accuracy
        self.turns: list[Turn] = []
        self._step = 0

    # -- accounting
    @property
    def total_input_tokens(self) -> int:
        return sum(t.input_tokens for t in self.turns)

    @property
    def total_output_tokens(self) -> int:
        return sum(t.output_tokens for t in self.turns)

    @property
    def total_cost_usd(self) -> float:
        return sum(t.cost_usd for t in self.turns)

    @property
    def total_latency_ms(self) -> float:
        return sum(t.latency_ms for t in self.turns)

    def reset(self) -> None:
        self.turns.clear()
        self._step = 0

    # -- the turn
    def think(self, context: str, catalogue: list[dict]) -> Turn:
        """Consume a context, emit the next step of the plan.

        The catalogue is serialised into the input token count whether or not
        the plan uses it, because that is exactly what happens with a real
        model: every tool description is paid for on every single turn.
        """
        catalogue_text = "".join(
            f"{t.get('name','')}{t.get('description','')}"
            f"{t.get('inputSchema','')}{t.get('outputSchema','')}"
            for t in catalogue
        )
        input_tokens = estimate_tokens(context) + estimate_tokens(catalogue_text)

        calls = self.plan[self._step] if self._step < len(self.plan) else []
        self._step += 1

        # A bigger catalogue does not just cost tokens, it costs accuracy. The
        # host models that here so Chapter 5 can measure the retry it causes.
        if calls and self.selection_accuracy < 1.0:
            if self.rng.random() > self.selection_accuracy:
                calls = [{**calls[0], "name": "__wrong_tool__"}]

        output_text = "".join(
            f"{c.get('name','')}{c.get('arguments','')}" for c in calls
        ) or "Here is the summary you asked for."
        output_tokens = estimate_tokens(output_text)

        latency = TTFT_MEAN_MS + output_tokens * MS_PER_OUTPUT_TOKEN
        if self.simulate_latency:
            latency = max(40.0, self.rng.gauss(TTFT_MEAN_MS, TTFT_STDEV_MS)
                          + output_tokens * MS_PER_OUTPUT_TOKEN)
            time.sleep(latency / 1000.0 * 0.02)  # 2% wall clock, so tests stay fast

        turn = Turn(input_tokens=input_tokens, output_tokens=output_tokens,
                    latency_ms=latency, tool_calls=calls,
                    text="" if calls else output_text)
        self.turns.append(turn)
        return turn

    def to_json(self) -> dict:
        return {
            "turns": len(self.turns),
            "inputTokens": self.total_input_tokens,
            "outputTokens": self.total_output_tokens,
            "costUsd": round(self.total_cost_usd, 6),
            "modelLatencyMs": round(self.total_latency_ms, 1),
        }
