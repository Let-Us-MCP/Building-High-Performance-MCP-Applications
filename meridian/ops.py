"""Operational checks: health, drain, and capacity arithmetic.

Most MCP health checks are wrong in an interesting way. They check that the
process is listening, which tells you almost nothing: a process can accept
connections while its catalogue is empty, its dependencies are down, and every
one of its handlers raises.

A useful check exercises the protocol.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .protocol.meta import (
    KEY_CLIENT_CAPABILITIES,
    KEY_PROTOCOL_VERSION,
    PROTOCOL_VERSION,
)


def healthy(server) -> tuple[bool, dict]:
    """Health means `server/discover` answers correctly, not that a port is open.

    Answering discovery proves routing, capability derivation, and
    serialisation all work, which is three subsystems for one request.
    """
    probe = {
        "jsonrpc": "2.0", "id": "health", "method": "server/discover",
        "params": {"_meta": {
            KEY_PROTOCOL_VERSION: PROTOCOL_VERSION,
            KEY_CLIENT_CAPABILITIES: {},
        }},
    }
    response = server.handle(probe)
    if response is None or "error" in response:
        reason = (response or {}).get("error", {}).get("message", "no response")
        return False, {"reason": reason}

    result = response["result"]
    ok = (PROTOCOL_VERSION in result.get("supportedVersions", [])
          and bool(result.get("capabilities")))
    return ok, {
        "capabilities": sorted(result.get("capabilities", {})),
        "supportedVersions": result.get("supportedVersions", []),
    }


def deep_check(server, *, tool: str, arguments: dict) -> tuple[bool, dict]:
    """Call one real tool against a known fixture.

    This is the check that catches a server whose database went away, and it is
    the one most people skip because it costs a real call.
    """
    from .protocol.meta import build_request_meta, ClientCapabilities

    params = dict(arguments)
    started = time.perf_counter()
    response = server.handle({
        "jsonrpc": "2.0", "id": "deep", "method": "tools/call",
        "params": {"name": tool, "arguments": params,
                   "_meta": build_request_meta(ClientCapabilities())},
    })
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    if response is None or "error" in response:
        return False, {"reason": "protocol error", "ms": round(elapsed_ms, 2)}
    result = response["result"]
    return not result.get("isError", False), {
        "ms": round(elapsed_ms, 2),
        "isError": result.get("isError", False),
    }


@dataclass
class Capacity:
    """Capacity planning for traffic whose shape is driven by loop iterations.

    One user request becomes several model turns becomes several tool calls,
    sometimes in a parallel burst. Provisioning for the mean gives you a
    service that falls over during every fan-out, which is to say during every
    task.
    """
    tasks_per_second: float
    round_trips_per_task: float
    fan_out_width: int

    @property
    def mean_rps(self) -> float:
        return self.tasks_per_second * self.round_trips_per_task

    @property
    def burst_concurrency(self) -> float:
        return self.mean_rps * self.fan_out_width

    def to_json(self) -> dict:
        return {
            "tasksPerSecond": self.tasks_per_second,
            "meanRequestsPerSecond": round(self.mean_rps, 1),
            "burstConcurrency": round(self.burst_concurrency, 1),
        }


def drain(server, subscriptions, *, timeout: float = 5.0) -> dict:
    """Shut down in the order that does not look like an outage to clients.

    Step three is the MCP-specific one. A subscription stream that just dies
    tells the client nothing, so every client treats a rolling deploy as an
    incident and reconnects at once, to the replicas still up.
    """
    closed = 0
    deadline = time.time() + timeout
    for sink in list(subscriptions):
        try:
            sink.close_gracefully()
            closed += 1
        except Exception:
            pass
        if time.time() > deadline:
            break
    return {"subscriptionsClosed": closed}
