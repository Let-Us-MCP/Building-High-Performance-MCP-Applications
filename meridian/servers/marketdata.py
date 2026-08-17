"""The market-data server: the one where caching actually pays.

Its results are identical for every caller, which makes them `cacheScope:
public`, which means a shared gateway can serve them to everyone. That is the
only server in Meridian where the gateway cache does real work, and Chapter 6
measures exactly how much.

It also serves the one genuinely large resource in the system, so it is where
pagination stops being a nicety.
"""

from __future__ import annotations

import json
import math

from ..protocol import (
    RequestContext,
    ResourceNotFound,
    Server,
    StdioServerTransport,
    StreamableHttpServer,
    text_result,
    tool_error,
)
from .data import INDUSTRIES, REGIONS, _rng

_rate_rng = _rng("rates")

# Fixed reference curve. Deterministic on purpose: a benchmark that moves
# because the data moved is not measuring the thing you think it is.
CURVE = {
    "1M": 4.42, "3M": 4.38, "6M": 4.21, "1Y": 3.96,
    "2Y": 3.71, "5Y": 3.68, "10Y": 3.89, "30Y": 4.12,
}

SECTOR_SPREADS = {
    industry: round(0.8 + i * 0.17, 2) for i, industry in enumerate(INDUSTRIES)
}


def build_server() -> Server:
    server = Server(
        "meridian-marketdata",
        "3.0.0",
        instructions=(
            "Reference rates and sector spreads. Everything here is public and "
            "identical for all callers, so results carry long TTLs and a public "
            "cache scope."
        ),
        tools_ttl_ms=600_000,
        tools_cache_scope="public",
        page_size=25,
    )

    @server.tool(
        "get_reference_curve",
        "Return the current reference rate curve.",
        {
            "type": "object",
            "properties": {
                "tenors": {
                    "type": "array",
                    "items": {"type": "string",
                              "enum": list(CURVE.keys())},
                    "description": "Subset of tenors. Omit for the whole curve.",
                },
            },
        },
        output_schema={
            "type": "object",
            "properties": {"curve": {"type": "object"}, "asOf": {"type": "string"}},
            "required": ["curve", "asOf"],
        },
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def get_reference_curve(ctx: RequestContext):
        tenors = ctx.arguments.get("tenors")
        curve = {k: v for k, v in CURVE.items() if not tenors or k in tenors}
        if not curve:
            return tool_error(
                "No tenors matched. Valid tenors: " + ", ".join(CURVE)
            )
        payload = {"curve": curve, "asOf": "2026-07-28T00:00:00Z"}
        return {"content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload, "isError": False}

    @server.tool(
        "price_facility",
        "Price a credit facility from a risk score and a tenor.",
        {
            "type": "object",
            "properties": {
                "riskScore": {"type": "number", "minimum": 1, "maximum": 99},
                "tenor": {"type": "string", "enum": list(CURVE.keys())},
                "industry": {"type": "string", "enum": INDUSTRIES},
                "amountUsd": {"type": "number", "minimum": 1000},
            },
            "required": ["riskScore", "tenor", "amountUsd"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "basePct": {"type": "number"},
                "creditSpreadPct": {"type": "number"},
                "sectorSpreadPct": {"type": "number"},
                "allInPct": {"type": "number"},
                "annualCostUsd": {"type": "number"},
            },
            "required": ["basePct", "creditSpreadPct", "allInPct", "annualCostUsd"],
        },
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def price_facility(ctx: RequestContext):
        args = ctx.arguments
        base = CURVE[args["tenor"]]
        credit = round(0.35 + math.exp(args["riskScore"] / 34.0) * 0.28, 3)
        sector = SECTOR_SPREADS.get(args.get("industry"), 0.0)
        all_in = round(base + credit + sector, 3)
        payload = {
            "basePct": base,
            "creditSpreadPct": credit,
            "sectorSpreadPct": sector,
            "allInPct": all_in,
            "annualCostUsd": round(args["amountUsd"] * all_in / 100.0, 2),
        }
        return {"content": [{"type": "text", "text": json.dumps(payload)}],
                "structuredContent": payload, "isError": False}

    @server.resource(
        "meridian://market/curve",
        "Reference curve",
        description="The full reference rate curve. Public, and stable for an hour.",
        mime_type="application/json",
        ttl_ms=3_600_000,
        cache_scope="public",
    )
    def curve_resource(ctx: RequestContext, uri: str):
        return json.dumps({"curve": CURVE, "asOf": "2026-07-28T00:00:00Z"},
                          separators=(",", ":"))

    @server.template(
        "meridian://market/sector/{industry}",
        "Sector spread",
        description="Indicative credit spread for one sector.",
        mime_type="application/json",
        ttl_ms=3_600_000,
        cache_scope="public",
    )
    def sector_resource(ctx: RequestContext, uri: str, params: dict):
        industry = params["industry"].replace("-", " ")
        if industry not in SECTOR_SPREADS:
            raise ResourceNotFound(uri)
        return json.dumps({"industry": industry,
                           "spreadPct": SECTOR_SPREADS[industry]},
                          separators=(",", ":"))

    # A deliberately long list, so pagination has something to page.
    for i, region in enumerate(REGIONS):
        for j, industry in enumerate(INDUSTRIES):
            slug = f"{region}-{industry.replace(' ', '-')}"
            server.add_resource(_benchmark_resource(slug, region, industry))

    return server


def _benchmark_resource(slug: str, region: str, industry: str):
    from ..protocol import Resource

    def reader(ctx: RequestContext, uri: str):
        return json.dumps({
            "region": region,
            "industry": industry,
            "medianLeverage": round(1.4 + len(industry) * 0.07, 2),
            "defaultRatePct": round(0.6 + len(region) * 0.11, 2),
        }, separators=(",", ":"))

    return Resource(
        uri=f"meridian://market/benchmark/{slug}",
        name=f"{region} {industry} benchmark",
        reader=reader,
        mime_type="application/json",
        ttl_ms=7_200_000,
        cache_scope="public",
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    import time

    ap = argparse.ArgumentParser(description="Meridian market data server")
    ap.add_argument("--http", type=int, metavar="PORT")
    args = ap.parse_args(argv)

    server = build_server()
    if args.http:
        http = StreamableHttpServer(server, port=args.http)
        print(f"meridian-marketdata on {http.url}", file=sys.stderr, flush=True)
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
