"""The risk-assessment server. Meridian's workhorse.

Everything the book teaches shows up somewhere in this file:

  * task-shaped tools rather than a REST mirror
  * output schemas, so the model never has to parse prose back into numbers
  * tool execution errors that a model can actually recover from
  * MRTR, for the approval a large exposure needs before it will compute
  * TTL'd resources with an honest `cacheScope`
  * a versioned prompt blueprint
  * the Tasks extension, for the four-step underwriting run
  * an MCP App template for the drill-down dashboard

Run it:

    python3 -m meridian.servers.risk                 # stdio
    python3 -m meridian.servers.risk --http 8931     # Streamable HTTP
"""

from __future__ import annotations

import json
import time

from ..protocol import (
    RequestContext,
    Server,
    StdioServerTransport,
    StreamableHttpServer,
    elicit_form,
    input_required,
    read_elicit,
    text_result,
    tool_error,
)
from ..protocol import tasks as tasks_ext
from ..protocol.mrtr import StateSealer
from .data import ACCOUNTS, FILINGS, TRANSACTIONS, risk_score

# One secret per deployment, not per process, or a retry that lands on another
# replica cannot open the state its sibling sealed.
SEALER = StateSealer(secret=b"meridian-risk-demo-key-not-for-production", ttl_seconds=900)

# Exposures above this need a named approver before the model gets a number.
APPROVAL_THRESHOLD_USD = 5_000_000


def build_server(*, fat_catalogue: bool = False) -> Server:
    server = Server(
        "meridian-risk",
        "1.4.0",
        instructions=(
            "Credit risk scoring for commercial accounts. Start with "
            "`assess_account_risk`; it returns a score, a band, and the factors "
            "that drove it. For a portfolio view use `rank_portfolio_risk` "
            "rather than calling the per-account tool in a loop."
        ),
        list_changed=True,
        subscribe=True,
        tools_ttl_ms=300_000,
        tools_cache_scope="public",
    )

    # ---------------------------------------------------------------- tools

    @server.tool(
        "assess_account_risk",
        "Score one commercial account for credit risk. Returns a 1-99 score, a "
        "band, and the weighted factors behind it.",
        {
            "type": "object",
            "properties": {
                "accountId": {
                    "type": "string",
                    "description": "Account identifier, e.g. ACC-1042",
                    "pattern": "^ACC-[0-9]{4}$",
                },
                "exposureUsd": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Proposed exposure. Above 5,000,000 an "
                                   "approver is required before scoring.",
                },
                "region": {
                    "type": "string",
                    "enum": ["us-east", "us-west", "eu-central", "eu-west", "apac-south"],
                    "description": "Routing hint. Mirrored into an HTTP header "
                                   "so gateways can route without reading the body.",
                    "x-mcp-header": "Region",
                },
            },
            "required": ["accountId"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "accountId": {"type": "string"},
                "score": {"type": "number"},
                "band": {"type": "string",
                         "enum": ["low", "moderate", "elevated", "high"]},
                "drivers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "factor": {"type": "string"},
                            "contribution": {"type": "number"},
                        },
                        "required": ["factor", "contribution"],
                    },
                },
            },
            "required": ["accountId", "score", "band", "drivers"],
        },
        annotations={"readOnlyHint": True, "idempotentHint": True},
        ui_resource_uri="ui://meridian/risk-dashboard",
    )
    def assess_account_risk(ctx: RequestContext):
        account_id = ctx.arguments.get("accountId")
        account = ACCOUNTS.get(account_id)
        if account is None:
            # A tool execution error, not a protocol error. The model can read
            # this, notice it used a stale id, and try a different one.
            return tool_error(
                f"No account {account_id}. Account ids look like ACC-1000 "
                f"through ACC-{1000 + len(ACCOUNTS) - 1}."
            )

        exposure = ctx.arguments.get("exposureUsd") or 0
        if exposure > APPROVAL_THRESHOLD_USD:
            interrupted = _require_approval(ctx, account_id, exposure)
            if interrupted is not None:
                return interrupted

        result = risk_score(account)
        return {
            "content": [{"type": "text", "text": json.dumps(result, separators=(",", ":"))}],
            "structuredContent": result,
            "isError": False,
        }

    def _require_approval(ctx: RequestContext, account_id: str, exposure: float):
        """The MRTR half of `assess_account_risk`.

        First pass: no approval on the request, so return an
        `InputRequiredResult` carrying the question and a sealed blob holding
        what we already worked out. Second pass: the answer is in
        `inputResponses`, the blob is verified, and we continue.
        """
        answer = read_elicit(ctx.input_responses, "approval")
        state = ctx.request_state

        if answer is not None and state is not None:
            principal = (ctx.auth or {}).get("sub")
            sealed = SEALER.open(state, principal=principal,
                                 method=ctx.method, params=ctx.params)
            if sealed.get("accountId") != account_id:
                return tool_error("Approval state does not match this account.")
            if answer.declined:
                return tool_error(
                    f"Exposure of ${exposure:,.0f} was declined by the approver."
                )
            if answer.cancelled:
                return tool_error("Approval dialog was dismissed. Nothing was scored.")
            approver = (answer.content or {}).get("approver", "").strip()
            if not approver:
                # Missing a value we need is not an error. Ask again.
                return _ask_for_approval(ctx, account_id, exposure)
            return None  # approved; fall through and score

        if answer is not None and state is None:
            return tool_error("Approval response arrived without its request state.")

        return _ask_for_approval(ctx, account_id, exposure)

    def _ask_for_approval(ctx: RequestContext, account_id: str, exposure: float):
        if not ctx.capabilities.supports_elicitation("form"):
            # Never send a request shape the client did not declare. Degrade
            # to a plain error the model can relay to the user instead.
            return tool_error(
                f"Exposure of ${exposure:,.0f} exceeds the "
                f"${APPROVAL_THRESHOLD_USD:,.0f} threshold and this client "
                "cannot collect an approval."
            )
        return input_required(
            input_requests={
                "approval": elicit_form(
                    f"Exposure of ${exposure:,.0f} on {account_id} exceeds the "
                    f"${APPROVAL_THRESHOLD_USD:,.0f} threshold. Who is approving?",
                    {
                        "type": "object",
                        "properties": {
                            "approver": {
                                "type": "string",
                                "title": "Approver",
                                "description": "Name or employee id of the approver",
                                "minLength": 2,
                            },
                            "rationale": {
                                "type": "string",
                                "title": "Rationale",
                                "maxLength": 400,
                            },
                        },
                        "required": ["approver"],
                    },
                )
            },
            request_state=SEALER.seal(
                {"accountId": account_id, "exposureUsd": exposure},
                principal=(ctx.auth or {}).get("sub"),
                method=ctx.method,
                params=ctx.params,
            ),
        )

    @server.tool(
        "rank_portfolio_risk",
        "Rank a set of accounts by risk score in one call. Prefer this over "
        "calling assess_account_risk repeatedly.",
        {
            "type": "object",
            "properties": {
                "accountIds": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 200,
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 10},
            },
            "required": ["accountIds"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "ranked": {"type": "array", "items": {"type": "object"}},
                "unknown": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ranked", "unknown"],
        },
        annotations={"readOnlyHint": True, "idempotentHint": True},
    )
    def rank_portfolio_risk(ctx: RequestContext):
        ids = ctx.arguments.get("accountIds") or []
        limit = int(ctx.arguments.get("limit") or 10)
        scored, unknown = [], []
        total = len(ids)
        for i, account_id in enumerate(ids):
            account = ACCOUNTS.get(account_id)
            if account is None:
                unknown.append(account_id)
                continue
            scored.append(risk_score(account))
            if total > 20 and i % 10 == 0:
                ctx.progress(i, total, f"scored {i} of {total}")
        scored.sort(key=lambda r: r["score"], reverse=True)
        payload = {"ranked": scored[:limit], "unknown": unknown}
        return {
            "content": [{"type": "text",
                         "text": f"Ranked {len(scored)} accounts, "
                                 f"{len(unknown)} unknown."}],
            "structuredContent": payload,
            "isError": False,
        }

    @server.tool(
        "underwrite_loan",
        "Run the four-stage underwriting pipeline for an account. Long-running; "
        "returns a task handle.",
        {
            "type": "object",
            "properties": {
                "accountId": {"type": "string", "pattern": "^ACC-[0-9]{4}$"},
                "amountUsd": {"type": "number", "minimum": 1000},
                "termMonths": {"type": "integer", "minimum": 6, "maximum": 120},
            },
            "required": ["accountId", "amountUsd"],
        },
    )
    def underwrite_loan(ctx: RequestContext):
        account_id = ctx.arguments["accountId"]
        account = ACCOUNTS.get(account_id)
        if account is None:
            return tool_error(f"No account {account_id}.")

        # Only return a task to a client that asked for the extension. To one
        # that did not, the fast path is the only correct answer.
        if not ctx.capabilities.supports_extension(tasks_ext.EXTENSION_ID):
            return _underwrite_now(account, ctx.arguments)

        task = server.tasks.create(status=tasks_ext.WORKING,
                                   status_message="queued", poll_interval_ms=200)

        def work(t):
            stages = ["pulling filings", "scoring", "checking covenants", "pricing"]
            for i, stage in enumerate(stages, start=1):
                server.tasks.update(t.task_id, status_message=stage,
                                    progress=i / len(stages))
                time.sleep(0.05)
            return _underwrite_now(account, ctx.arguments)

        tasks_ext.run_in_background(server.tasks, task, work)
        return tasks_ext.create_task_result(task)

    def _underwrite_now(account, args: dict) -> dict:
        score = risk_score(account)
        amount = args["amountUsd"]
        term = int(args.get("termMonths") or 36)
        spread = 1.4 + score["score"] * 0.045
        decision = "decline" if score["score"] > 82 else (
            "refer" if score["score"] > 68 else "approve")
        payload = {
            "accountId": account.account_id,
            "decision": decision,
            "score": score["score"],
            "band": score["band"],
            "amountUsd": amount,
            "termMonths": term,
            "indicativeSpreadPct": round(spread, 2),
        }
        return {
            "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
            "structuredContent": payload,
            "isError": False,
        }

    if fat_catalogue:
        _add_legacy_catalogue(server)

    # ------------------------------------------------------------ resources

    @server.template(
        "meridian://accounts/{account_id}/summary",
        "Account summary",
        description="Static profile for one account. Cheap, stable, private.",
        mime_type="application/json",
        ttl_ms=900_000,
        cache_scope="private",
    )
    def account_summary(ctx: RequestContext, uri: str, params: dict):
        account = ACCOUNTS.get(params["account_id"])
        if account is None:
            from ..protocol import ResourceNotFound
            raise ResourceNotFound(uri)
        return json.dumps(account.to_json(), separators=(",", ":"))

    @server.template(
        "meridian://filings/{filing_id}",
        "Regulatory filing",
        description="Public regulatory filing. Identical for every caller, so "
                    "a shared gateway may cache it.",
        mime_type="application/json",
        ttl_ms=86_400_000,
        cache_scope="public",
    )
    def filing(ctx: RequestContext, uri: str, params: dict):
        record = FILINGS.get(params["filing_id"])
        if record is None:
            from ..protocol import ResourceNotFound
            raise ResourceNotFound(uri)
        return json.dumps(record, separators=(",", ":"))

    @server.resource(
        "meridian://risk/model-card",
        "Risk model card",
        description="How the score is computed, and what it must not be used for.",
        mime_type="text/markdown",
        ttl_ms=604_800_000,
        cache_scope="public",
    )
    def model_card(ctx: RequestContext, uri: str):
        return MODEL_CARD

    @server.resource(
        "ui://meridian/risk-dashboard",
        "Risk dashboard",
        description="MCP App template for interactive drill-down.",
        mime_type="text/html;profile=mcp-app",
        ttl_ms=86_400_000,
        cache_scope="public",
    )
    def dashboard(ctx: RequestContext, uri: str):
        return DASHBOARD_HTML

    # -------------------------------------------------------------- prompts

    @server.prompt(
        "credit-review",
        "Standard credit review narrative for one account.",
        arguments=[
            {"name": "accountId", "description": "Account to review", "required": True},
            {"name": "audience", "description": "committee | relationship-manager",
             "required": False},
        ],
        version="3.2.0",
    )
    def credit_review(ctx: RequestContext, args: dict):
        audience = args.get("audience", "committee")
        account_id = args["accountId"]
        return [
            {
                "role": "user",
                "content": {
                    "type": "text",
                    "text": (
                        f"Produce a credit review for {account_id} addressed to the "
                        f"{audience}. Call assess_account_risk first and quote the "
                        "score and band verbatim. Name the two largest drivers. "
                        "State the recommendation in the first sentence. Do not "
                        "speculate about data you were not given."
                    ),
                },
            }
        ]

    # ---------------------------------------------------------- completions

    @server.completer("prompt", "credit-review", "accountId")
    def complete_account_id(value: str, filled: dict) -> list[str]:
        """Autocomplete account ids as the user types the slash command.

        Prefix-matched and case-insensitive, because a user typing `acc-10`
        means the same thing as `ACC-10` and being pedantic about it is how
        completion menus end up empty.
        """
        prefix = value.strip().upper()
        return sorted(a for a in ACCOUNTS if a.startswith(prefix))

    @server.completer("prompt", "credit-review", "audience")
    def complete_audience(value: str, filled: dict) -> list[str]:
        return [a for a in ("committee", "relationship-manager")
                if a.startswith(value.lower())]

    return server


def _add_legacy_catalogue(server: Server) -> None:
    """The tools Meridian started with, before Chapter 5 happened.

    A one-to-one mirror of the internal REST API. It works, it is discoverable,
    and it costs thousands of tokens of context on every single request while
    measurably lowering tool-selection accuracy. Chapter 5 deletes it and
    measures the difference. The default build is the after picture; pass
    `fat_catalogue=True` to reproduce the before.
    """
    verbs = ["get", "list", "create", "update", "delete"]
    nouns = ["account", "exposure", "covenant", "collateral", "guarantor", "rating"]
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "Resource identifier"},
            "body": {"type": "object", "description": "Request body"},
            "page": {"type": "integer", "description": "Page number"},
            "pageSize": {"type": "integer", "description": "Items per page"},
        },
    }
    for verb in verbs:
        for noun in nouns:
            if verb == "delete" and noun in ("rating", "account"):
                continue
            server.add_tool(_legacy_tool(f"{verb}_{noun}", verb, noun, schema))


def _legacy_tool(name: str, verb: str, noun: str, schema: dict):
    from ..protocol import Tool

    description = (
        f"{verb.capitalize()} a {noun} record via the core banking API. "
        f"Wraps {verb.upper()} /v2/{noun}s. Accepts an optional id, an "
        f"optional body, and standard pagination parameters. Returns the "
        f"raw {noun} representation as stored by the system of record, "
        f"including audit fields, soft-delete markers, and denormalised "
        f"references to related entities."
    )

    def handler(ctx: RequestContext):
        return text_result(f"{verb} {noun}: not implemented in the reference build")

    return Tool(name=name, description=description, input_schema=schema,
                handler=handler)


MODEL_CARD = """# Meridian risk model card

**Version** 1.4.0  **Scope** commercial credit, revolving and term.

## What the score means

A number from 1 to 99. Higher is worse. Bands: low (<35), moderate (35-54),
elevated (55-74), high (>=75).

## Inputs

Leverage, years trading, prior defaults, and revenue scale. Nothing else. In
particular the model does not see region, industry, or any protected
characteristic, and it is not permitted to.

## What it must not be used for

Consumer lending. Pricing without a human in the loop above the approval
threshold. Any decision that has to be explained to a regulator using factors
this model does not consume.

## Known weaknesses

Thin-file accounts (under two years trading) score conservatively because
tenure carries a fixed negative weight that they cannot earn.
"""


DASHBOARD_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Meridian risk</title>
<style>
  :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
  body { margin: 0; padding: 16px; }
  .band { display: inline-block; padding: 2px 10px; border-radius: 999px;
          font-size: 12px; font-weight: 600; }
  .low { background:#E9F2EC; color:#2C6E49 }
  .moderate { background:#E7F0F6; color:#0F5C8C }
  .elevated { background:#FBF0E7; color:#B4531A }
  .high { background:#FAECEC; color:#9B2226 }
  table { border-collapse: collapse; width: 100%; font-size: 14px; }
  td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid #C9CED6; }
  button { font: inherit; cursor: pointer; }
</style>
<h2 id="title">Risk drill-down</h2>
<table><thead><tr><th>Factor</th><th>Contribution</th></tr></thead>
<tbody id="rows"></tbody></table>
<p><button id="refresh">Re-score</button></p>
<script>
  // Everything the app knows arrives through postMessage. It has no network of
  // its own: the host builds a CSP from the server's declared domains, and the
  // default is no network at all.
  let accountId = null;
  const rpc = (method, params) => new Promise(resolve => {
    const id = Math.random().toString(36).slice(2);
    const onMessage = (e) => {
      if (e.data && e.data.id === id) {
        window.removeEventListener('message', onMessage);
        resolve(e.data.result);
      }
    };
    window.addEventListener('message', onMessage);
    parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
  });

  function render(data) {
    accountId = data.accountId;
    document.getElementById('title').innerHTML =
      `Risk drill-down: ${data.accountId} <span class="band ${data.band}">` +
      `${data.score} ${data.band}</span>`;
    document.getElementById('rows').innerHTML = (data.drivers || [])
      .map(d => `<tr><td>${d.factor}</td><td>${d.contribution}</td></tr>`).join('');
  }

  window.addEventListener('message', (e) => {
    if (e.data && e.data.method === 'ui/render') render(e.data.params.structuredContent);
  });

  document.getElementById('refresh').addEventListener('click', async () => {
    // A tool call from the app travels the host's normal consent and audit
    // path. The iframe gets no shortcut just because it is inside the host.
    const r = await rpc('tools/call',
      { name: 'assess_account_risk', arguments: { accountId } });
    if (r && r.structuredContent) render(r.structuredContent);
  });

  parent.postMessage({ jsonrpc: '2.0', id: 'init', method: 'ui/initialize',
                       params: { protocolVersion: '2026-01-26' } }, '*');
</script>
"""


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Meridian risk server")
    ap.add_argument("--http", type=int, metavar="PORT",
                    help="serve Streamable HTTP on this port instead of stdio")
    ap.add_argument("--fat", action="store_true",
                    help="serve the pre-Chapter-5 REST-mirror catalogue")
    args = ap.parse_args(argv)

    server = build_server(fat_catalogue=args.fat)
    tasks_ext.install(server)

    if args.http:
        http = StreamableHttpServer(server, port=args.http)
        print(f"meridian-risk on {http.url}", file=sys.stderr, flush=True)
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
