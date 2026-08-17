"""The fraud server: fast, read-only, and deliberately boring.

Every Meridian server has a job in the book. This one's job is to be the
cheap parallel call. Chapter 5 fans out risk, fraud, and market data at the
same time and measures what that buys over calling them in sequence, and this
server exists so that fan-out has something honest to fan out to.

It is also the one that changes its tool list at runtime, which makes it the
worked example for `subscriptions/listen` and cache invalidation.
"""

from __future__ import annotations

import json
import time

from ..protocol import (
    RequestContext,
    Server,
    StdioServerTransport,
    StreamableHttpServer,
    Tool,
    text_result,
    tool_error,
)
from .data import ACCOUNTS, TRANSACTIONS

VELOCITY_WINDOW_DAYS = 90


def build_server() -> Server:
    server = Server(
        "meridian-fraud",
        "0.9.3",
        instructions=(
            "Behavioural fraud signals. Read-only and fast; safe to call in "
            "parallel with risk scoring."
        ),
        list_changed=True,
        subscribe=True,
        tools_ttl_ms=60_000,
        tools_cache_scope="public",
    )

    @server.tool(
        "screen_account",
        "Screen one account for fraud signals across its recent transactions.",
        {
            "type": "object",
            "properties": {
                "accountId": {"type": "string", "pattern": "^ACC-[0-9]{4}$"},
            },
            "required": ["accountId"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "accountId": {"type": "string"},
                "signalCount": {"type": "integer"},
                "signals": {"type": "array", "items": {"type": "object"}},
                "verdict": {"type": "string",
                            "enum": ["clean", "watch", "investigate"]},
            },
            "required": ["accountId", "signalCount", "signals", "verdict"],
        },
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def screen_account(ctx: RequestContext):
        account_id = ctx.arguments["accountId"]
        if account_id not in ACCOUNTS:
            return tool_error(f"No account {account_id}.")

        txns = TRANSACTIONS.get(account_id, [])
        signals = []
        near_threshold = [t for t in txns if 9_000 <= t.amount_usd < 10_000]
        if len(near_threshold) >= 2:
            signals.append({
                "type": "structuring",
                "severity": "high",
                "detail": f"{len(near_threshold)} transfers just below $10,000",
            })
        round_numbers = [t for t in txns if t.amount_usd >= 100_000
                         and t.amount_usd % 50_000 == 0]
        if round_numbers:
            signals.append({
                "type": "round-amount",
                "severity": "medium",
                "detail": f"{len(round_numbers)} round-number transfers",
            })
        corridors = {t.corridor for t in txns}
        if len(corridors) >= 5:
            signals.append({
                "type": "corridor-spread",
                "severity": "low",
                "detail": f"activity across {len(corridors)} corridors",
            })

        verdict = ("investigate" if any(s["severity"] == "high" for s in signals)
                   else "watch" if signals else "clean")
        payload = {"accountId": account_id, "signalCount": len(signals),
                   "signals": signals, "verdict": verdict}
        return {"content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload, "isError": False}

    @server.tool(
        "explain_signal",
        "Explain what one fraud signal type means and how it is detected.",
        {
            "type": "object",
            "properties": {
                "signalType": {
                    "type": "string",
                    "enum": ["structuring", "round-amount", "corridor-spread"],
                },
            },
            "required": ["signalType"],
        },
        annotations={"readOnlyHint": True},
    )
    def explain_signal(ctx: RequestContext):
        return text_result(EXPLANATIONS[ctx.arguments["signalType"]])

    return server


EXPLANATIONS = {
    "structuring": (
        "Two or more transfers landing just under a reporting threshold in a "
        "short window. Individually unremarkable, collectively a pattern. The "
        "detector looks at the gap to the threshold, not the absolute amount."
    ),
    "round-amount": (
        "Large transfers at suspiciously round values. Legitimate commercial "
        "payments are usually invoice-shaped and rarely land on a multiple of "
        "fifty thousand."
    ),
    "corridor-spread": (
        "Activity spread across an unusual number of jurisdiction pairs for the "
        "account's stated business. Low severity on its own; it earns its keep "
        "in combination with the other two."
    ),
}


def add_runtime_tool(server: Server) -> int:
    """Add a tool after startup and tell every listener.

    This is the whole `listChanged` story in six lines. The notification is an
    immediate invalidation signal, so a client sitting on a `tools/list` result
    with four minutes of TTL left throws it away and refetches. Without the
    notification it would keep planning against a catalogue that no longer
    exists.
    """
    server.add_tool(Tool(
        name="screen_counterparty",
        description="Screen a counterparty name against the watchlist.",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "minLength": 2}},
            "required": ["name"],
        },
        handler=lambda ctx: text_result(
            f"{ctx.arguments['name']}: no watchlist match."
        ),
    ))
    return server.notify_list_changed("tools")


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Meridian fraud server")
    ap.add_argument("--http", type=int, metavar="PORT")
    args = ap.parse_args(argv)

    server = build_server()
    if args.http:
        http = StreamableHttpServer(server, port=args.http)
        print(f"meridian-fraud on {http.url}", file=sys.stderr, flush=True)
        http.start()
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            http.stop()
        return 0

    StdioServerTransport(server).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
