# High-Performance MCP Applications

## *What every AI application developer should know about the Model Context Protocol*

**Revised outline v2 — pinned to protocol revision 2026-07-28**

---

## The reframe

The previous draft was a *Designing Data-Intensive Applications*-style architecture book. This revision is a *High Performance Browser Networking*-style book: teach the protocol mechanics deeply enough — down to the wire — that readers can reason about the performance, cost, and reliability of the applications they build on top. HPBN's engine is **physics → protocol mechanics → application consequences**, backed everywhere by real measurements. This outline transplants that engine.

Three principles drive the revision:

1. **Physics first.** HPBN opens with latency and bandwidth because every later chapter cashes out against them. This book opens with the three budgets of agentic applications — **latency** (time-to-first-token, tokens/sec, round trips), **context** (the token window as bandwidth), and **cost** — and every later chapter cashes out against those.
2. **Mechanics → performance → use cases, per primitive.** Each protocol feature gets the HPBN treatment: how it works on the wire, what it costs, and when to use it. Conveniently, the 2026-07-28 revision is *itself* performance-motivated — statelessness, cacheable list results with TTLs, deterministic ordering for prompt caches, header-based routing — so the protocol and the book's thesis reinforce each other.
3. **Measurement over narrative.** HPBN persuades with waterfall charts and telemetry, not stories. **Project Meridian survives but changes jobs**: instead of a fictional bank's narrative arc, it becomes the book's *instrumented reference application* — an open-source, runnable financial-analytics assistant spanning four MCP servers, with a load-test harness and dashboards in the companion repo. Every optimization in the book appears as a before/after trace from Meridian.

**Spec pinning.** The book is written against **2026-07-28**, the largest revision since MCP launched: a stateless core, the extensions framework, and a formal feature lifecycle (Active → Deprecated → Removed, twelve-month minimum window). Deprecated features (Roots, Sampling, Logging, HTTP+SSE, RFC 7591 Dynamic Client Registration) are covered in clearly marked **Legacy boxes** — enough to maintain existing systems, never presented as patterns to adopt. The lifecycle policy is also the book's shelf-life argument: a text pinned to this revision tracks a protocol that has promised not to break silently.

### The HPBN → MCP translation table

| HPBN concept | MCP equivalent in this book |
|---|---|
| Latency & bandwidth primer | Time-to-first-token, tokens/sec, round trips, context window, $/task |
| Building blocks of TCP/UDP | JSON-RPC message anatomy; Streamable HTTP and STDIO wire mechanics |
| TLS chapter (security *and* handshake cost) | OAuth 2.1 on the wire: flows, token caching, auth round-trip cost |
| HTTP/1.1 vs HTTP/2 semantics | The primitives: tools, resources, prompts — mechanics and cost |
| HTTP caching, keep-alive, pipelining | `CacheableResult` (ttlMs / cacheScope), prompt caching, connection reuse |
| Head-of-line blocking, multiplexing | Serialized agent loops vs parallel tool fan-out; MRTR round-trip costs |
| SSE / WebSocket / WebRTC chapters | Response streams, `subscriptions/listen`, Tasks, MCP Apps |
| Resource hints, prefetching | Predeclared `ui://` templates, cache-TTL-aware prefetch, warm STDIO pools |
| RUM and synthetic measurement | OpenTelemetry trace propagation via `_meta`; evals as regression tests |

---

## Part I — Protocol Fundamentals

> *Goal: the physics of agentic applications, then the protocol core and its transports — down to the bytes. (HPBN Part I: "Networking 101.")*

### Chapter 1: Primer on Latency, Tokens, and Context

- The three budgets every AI application spends: **latency** (time-to-first-token, tokens/sec, and the round-trip time of one agent-loop iteration), **context** (the token window as the application's bandwidth and memory), and **cost** ($/task as a first-class metric)
- Anatomy of a single loop iteration, with a real waterfall: model inference → tool-call serialization → transport → server execution → response → re-inference; where the milliseconds and the tokens actually go
- Round trips are the enemy: why every extra model turn or client round trip dominates end-to-end latency, and how this one idea shapes every design decision in the book
- The N × M integration problem and why a protocol (not a framework) is the answer
- Positioning MCP against the alternatives: provider-native function calling, OpenAPI-driven tool use, framework tool abstractions (LangChain, Semantic Kernel), and agent-to-agent protocols (A2A) — what each optimizes for
- Meet **Meridian**, the book's instrumented reference application, and its baseline measurements — the numbers every subsequent chapter improves

### Chapter 2: The MCP Architectural Model

- The three roles — **Host**, **Client**, **Server** — and why the *host* is the trust boundary, not the model
- The stateless core: no initialization handshake, no protocol-level session; every request carries its protocol version and client capabilities in `_meta` (`io.modelcontextprotocol/protocolVersion`, `clientCapabilities`, `clientInfo`), and servers identify themselves in each result
- `server/discover`: up-front capability and version discovery, and its role as a backward-compatibility probe
- `resultType` on every result (`"complete"` vs `"input_required"`) as the hinge for the interaction patterns in Chapter 8
- The protocol as a contract: date-stamped versioning, `UnsupportedProtocolVersionError`, and the **feature lifecycle policy** (Active/Deprecated/Removed, twelve-month window) as the concrete answer to "will this book's code still run?"
- Threat modeling as a design input: every server response, resource payload, and LLM output is **untrusted input** — the security lens that recurs through Part V
- **Legacy box:** the 2024–2025 stateful protocol — `initialize`, `Mcp-Session-Id`, sticky sessions — what it was, why it lost, and how to recognize it in older servers

### Chapter 3: Building Blocks of the Transport

- JSON-RPC message anatomy: requests, results, errors, notifications; the MCP error-code allocation (`-32020` to `-32099` reserved) and what each code tells a client to do
- **Streamable HTTP** on the wire: one endpoint, one POST per request, required `Mcp-Method` / `Mcp-Name` headers (and what they buy: gateway routing, WAF policy, per-method rate limits), SSE-framed response streams, and request-scoped notifications (`notifications/progress`, `notifications/message`) riding the request's own stream
- **`subscriptions/listen`**: the single long-lived stream for opted-in server push (list-changed events, resource subscriptions) — MCP's SSE-channel analog, and how it replaces the old GET endpoint and `resources/subscribe`
- The failure model: no stream resumability — a broken response stream means re-issuing the request with a new ID; designing idempotent servers so retries are safe
- **STDIO** on the wire: framing, process lifecycle, cold-start cost, sandboxing; when a local pipe beats a network hop
- Topology and scale: sidecar (1:1), gateway (N:1), mesh (N:M) — and how the stateless core turns horizontal scaling into plain round-robin load balancing with no sticky sessions
- Meridian: measuring STDIO cold starts vs warm HTTP connections for the risk-model server; the transport decision, with numbers
- **Legacy box:** HTTP+SSE (deprecated since 2025-03-26, now formally Deprecated) — recognizing and migrating old servers

### Chapter 4: Authorization on the Wire

- OAuth 2.1 mechanics as MCP uses them: PKCE, protected-resource metadata, audience binding (RFC 8707), token exchange
- Client identity in 2026: **Client ID Metadata Documents** as the registration mechanism; issuer-bound credentials; validating the `iss` parameter (RFC 9207) before redeeming authorization codes
- Integrating with enterprise IdPs (Entra ID, Okta, Keycloak) without forking your server
- The performance chapter hiding inside the security chapter: what each auth round trip costs, token caching and refresh strategy, and amortizing consent across a session's worth of requests
- Failure and revocation: expired tokens mid-loop, re-auth without losing agent state
- **Legacy box:** RFC 7591 Dynamic Client Registration — deprecated, but still what many 2025-era authorization servers speak

---

## Part II — The Primitives

> *Goal: the complete 2026-07-28 capability surface. Every chapter follows the HPBN pattern: wire mechanics → performance characteristics → design guidelines and anti-patterns. (HPBN Part III: "HTTP.")*

### Chapter 5: Tools — The Execution Layer

- From REST mirroring to **semantic, task-shaped tools**: designing for a model's planning process, not a developer's API habits
- Schemas in depth: full JSON Schema 2020-12 input/output schemas, `$ref` resolution and composition bounds, structured outputs, idempotency markers
- The interception loop: model tool-call intent → host → client → server → result → re-inference; where validation, consent, and audit hooks belong
- Error taxonomy: **recoverable** errors that feed back to the model for retry vs **fatal** errors that halt the loop — and how mislabeling one as the other burns tokens or kills tasks
- Performance: tool descriptions spend context on *every* request — measuring catalog token cost, the tool-count vs selection-accuracy curve, and why the spec now asks for **deterministic `tools/list` ordering** (prompt-cache hit rates)
- Parallel tool fan-out vs serialized calls: when the model can batch, and what the host must do to let it
- Meridian: the loan-origination system wrapped as guarded tools; before/after traces of catalog slimming and parallelization

### Chapter 6: Resources — The Context Layer

- URI and resource-template design for AI consumption: naming, MIME types, semantic discoverability, pagination as a requirement rather than an afterthought
- The 2026 caching model: `CacheableResult` — `ttlMs` freshness hints and `cacheScope` (`public` / `private`) on list and read results; letting gateways and shared intermediaries cache what they're allowed to
- Invalidation: `subscriptions/listen` for list-changed and resource-change events; TTL-aware refresh loops in the host
- Who chooses context: user-driven selection vs model-driven inclusion vs host policy — and the token cost of each strategy
- Anti-patterns: dumping databases as resources, ignoring pagination, leaking cross-tenant data through shared URIs, setting `cacheScope: public` on anything personalized
- Meridian: account summaries and regulatory filings as scoped, TTL'd resources; measuring cache hit rates at the gateway
- **Legacy box:** Roots — deprecated; migrate to passing directories and files via tool parameters, resource URIs, or server configuration

### Chapter 7: Prompts — Workflow Blueprints

- Prompts as **server-owned, versioned assets**, not client-side string concatenation
- Argument passing, message structuring, multi-turn templates, and composing server prompts with host-side context
- Versioning and compatibility for prompt blueprints; testing prompts like code
- Performance: templates spend the context budget too — measuring prompt token cost and its interaction with provider prompt caching (stable prefixes win)
- Anti-patterns: prompt sprawl, unversioned templates, business logic buried in prose
- Meridian: one compliance-review blueprint standardized across three business units, with regression evals

### Chapter 8: Multi Round-Trip Requests — Interaction and Elicitation

- The **MRTR pattern**, the 2026 replacement for server-initiated requests: a server returns `resultType: "input_required"` with `inputRequests`; the client retries the original request carrying `inputResponses`; `requestState` correlates the exchange
- Elicitation on top of MRTR: structured forms, confirmations, free-text fallbacks, and URL-mode handoffs for out-of-band interactions (payment pages, IdP screens)
- Elicitation vs prompting: when to ask the *user* vs when to ask the *model* — and when to ask neither because the tool schema should have captured it
- The cost model: **every MRTR is at least one extra client round trip**, often plus a model turn — designing servers to gather inputs up front, and hosts to render interruptions without breaking flow
- Consent gates: pairing elicitation with authorization for sensitive operations (deep treatment in Chapter 20)
- Design post-mortem: why **Sampling** and **Roots** were deprecated — what the control-flow inversion promised, why direct provider-API integration won, and what that teaches about protocol design
- Meridian: a compliance officer confirms a flagged transaction mid-task; measuring the human-in-the-loop latency budget
- **Legacy box:** Sampling — maintaining 2025-era servers that still call back for completions; migration to provider APIs

### Chapter 9: Extensions and Tasks — Long-Running Work

- The **extensions framework**: `extensions` in client/server capabilities, negotiation, official vs vendor extensions — how MCP now evolves without breaking its core
- The **Tasks extension** (`io.modelcontextprotocol/tasks`): task handles (including unsolicited ones on ordinary calls), polling with `tasks/get`, mid-flight input via `tasks/update`, cancellation
- Task lifecycles: submitted → working → input-required → completed/failed; combining Tasks with MRTR for long jobs that need a human mid-flight
- Performance: polling cadence vs latency vs cost; progress streaming on the original response stream; timeout and retry strategy across network partitions
- When *not* to use Tasks: the synchronous fast path is cheaper — thresholds and heuristics
- Meridian: the loan-underwriting agent as a four-step task with progress streamed to the host; latency distribution before/after moving to Tasks

### Chapter 10: MCP Apps — Interactive Interfaces

- The MCP Apps extension (`io.modelcontextprotocol/ui`): tools declare a UI via `_meta.ui.resourceUri`; templates live at `ui://` URIs; hosts render `text/html;profile=mcp-app` content in **sandboxed iframes**
- The security contract: CSP constructed from server-declared domains (default: no network), all UI↔host communication as JSON-RPC over `postMessage`, every UI-initiated tool call traversing the host's normal audit and consent path
- Performance: templates are predeclared, so hosts can **prefetch, cache, and security-review** them before any tool runs; `structuredContent` delivers UI-grade data without bloating the model's context
- When an app beats text: dashboards, forms, pickers, visual diffs — and when it's just a slower paragraph
- UX patterns: apps inside a conversation, state handoff between the app and the loop, teardown and state saving
- Meridian: the risk dashboard as an MCP App — interactive drill-down without leaving the conversation, with template prefetch measured against cold render

---

## Part III — Building MCP Applications

> *Goal: the hands-on build arc, in Python and TypeScript, with the stateless-core discipline baked in from the first line of code. Every chapter extends the Meridian companion repo. (HPBN Part IV: the "building with the APIs" chapters.)*

### Chapter 11: Building Servers

- Anatomy of a 2026 server: transport binding, `server/discover` implementation, request routing — no handshake, no session object
- SDK walkthrough: Python (`mcp`) and TypeScript (`@modelcontextprotocol/sdk`), plus what "Tier 1 SDK" support means for upgrade cadence
- Implementing the full surface in one server: tools, resources, prompts, MRTR responders, Tasks, and an App template
- The stateless discipline: **server-minted handles passed as ordinary tool arguments** for cross-call state, so any replica can answer any request
- Configuration: environment-based secrets, capability toggles, feature flags; declaring extensions honestly
- Meridian build: the risk-assessment server from empty directory to passing contract tests

### Chapter 12: Building Hosts and Clients

- The host as orchestrator: managing many server connections, discovery, capability caching (honoring `ttlMs`), and reconnection
- Routing model intents to the right server; namespacing and collision handling across catalogs
- Context assembly: merging resources, injecting prompts, and enforcing a token budget across servers — the host as the context-window's memory allocator
- Handling `input_required` results, task updates, and `subscriptions/listen` events without blocking the loop
- Rendering MCP Apps and elicitation forms; consent UX that users don't rage-click through
- Meridian build: a minimal host connecting two servers and running a multi-step loop, fully traced

### Chapter 13: The Agent Loop, State, and Memory

- Loop design: termination conditions, step budgets, parallel fan-out, and streaming partial results to the user (time-to-first-token as a UX contract)
- Where state actually lives now: server-minted handles, external stores (databases, vector stores, KV caches) exposed as resources, and host-side conversation state
- Context-window management: summarization, retrieval-augmented re-injection, sliding windows — measured against task success, not vibes
- Cache staleness for autonomous agents: TTL-aware refresh, detecting drift, and the cost of being wrong
- Cost guardrails: per-task token and dollar budgets, model routing (small models for glue steps, large for reasoning)
- Meridian: the risk agent maintains a 30-day interaction history across fully stateless sessions

### Chapter 14: Multi-Agent Topologies

- Agents as **both hosts and servers**: consuming tools from one side while exposing capabilities on the other
- Discovery at scale: the official MCP Registry, private registries, capability advertisement, runtime binding
- Delegation patterns: supervisor trees, peer meshes, hierarchies — and the failure isolation each provides
- Preventing runaway systems: depth limits, budget enforcement, circuit breakers across agent boundaries
- Meridian: a supervisor coordinates the risk, compliance, and fraud agents; tracing one request across all three

---

## Part IV — Performance Engineering

> *Goal: the book's HPBN heart — measure, then optimize, then operate. New in this revision.*

### Chapter 15: Testing and Debugging

- Unit testing servers: mocked transports, asserting on JSON-RPC message sequences, golden-file tests for schemas
- Contract testing against 2026-07-28: schema validation, `server/discover` correctness, `resultType` handling, error-code discipline
- Integration testing: host → client → server flows with stubbed models (deterministic) and real models (evals)
- **MCP Inspector** workflows: tracing calls, inspecting payloads, replaying flows
- CI gates: every Meridian server contract-tested and eval-gated before deploy

### Chapter 16: Measuring MCP Applications

- The metrics that matter: time-to-first-token, tokens/sec, loop-iteration latency decomposed (inference / transport / server execution / serialization), tool-selection accuracy, call success rate, context utilization, cache hit rate, $/task
- Distributed tracing done right: **OpenTelemetry context propagation through `_meta`** (`traceparent`, `tracestate`, `baggage`) for true host → server spans
- Building the waterfall: instrumenting one agent-loop iteration end to end, and reading the result
- Evals as regression tests: task suites, success criteria, and catching capability drift when models or servers change
- Dashboards and alerting foundations (operational alerting continues in Chapter 18)
- Meridian: the live dashboard that every optimization in Chapter 17 is judged against

### Chapter 17: Optimizing for Latency, Tokens, and Cost

- **Cut round trips:** front-load MRTR inputs into tool schemas, batch arguments, avoid needless elicitation, prefetch App templates during idle time
- **Cache at every layer:** honor and tune `ttlMs`; use `cacheScope: public` to let gateways serve list results; stable tool ordering and stable descriptions for provider prompt-cache hits; resource caching with subscription-driven invalidation
- **Stream everything:** progress notifications, partial results, token streaming — perceived latency is the latency
- **Transport tuning:** connection reuse and keep-alive for Streamable HTTP; warm pools vs spawn-per-call for STDIO; gateway routing on `Mcp-Method` / `Mcp-Name`
- **Spend tokens deliberately:** right-size tool catalogs, compress descriptions, use structured outputs to eliminate parse-retry loops, tune task-polling cadence
- **Route models:** cheap models for glue steps, expensive models for reasoning; measuring the quality/cost frontier
- Meridian: the full optimization pass — baseline vs final waterfall, with each change attributed

### Chapter 18: Deployment and Operations

- Topologies: STDIO sidecar, containerized Streamable HTTP, and **serverless** — now a first-class option because the core is stateless
- Scaling: plain round-robin load balancing, autoscaling signals, capacity planning from Chapter 16's metrics
- Lifecycle: health checks, graceful drain, zero-downtime capability rollout, honoring the deprecation window in your own servers
- Multi-tenant hosting: isolating tenants without protocol sessions to lean on
- Operational alerting: tool-call loops, subscription storms, timeout spikes, cache stampedes
- Meridian: the compliance server behind an API gateway, load-tested to failure and back

---

## Part V — Security and Trust

> *Goal: harden everything built in Parts I–IV for adversarial environments. The threat lens from Chapter 2, cashed out.*

### Chapter 19: Threat Modeling Agentic Applications

- The adversary catalog: prompt injection, tool poisoning and rug-pulls, confused-deputy attacks, forged tool-call intents, data exfiltration through resource URIs
- The lethal combination: private data + untrusted content + an exfiltration channel — and how to make sure your application never has all three at once
- The Apps attack surface: server-supplied HTML, CSP declaration review, iframe escape attempts, `postMessage` abuse — why UI-initiated calls must traverse the same consent path as everything else
- Fault isolation: sandboxing servers, limiting blast radius, graceful degradation when a server is unavailable, compromised, or lying
- A red-team checklist for MCP deployments
- Meridian: the pen test — a compromised compliance server attempts exfiltration through resource URIs; what the audit log caught and what it missed

### Chapter 20: Access Control, Audit, and MCP in the Enterprise

- Scoped authorization in practice: per-tool, per-resource, per-request permission models on top of Chapter 4's wire mechanics
- Context isolation: preventing leakage between clients sharing a server; row-level security as the worked example
- Human-in-the-loop as a control: pairing MRTR elicitation with approval gates for high-risk operations
- Audit: immutable, structured records of every call, resource access, and app interaction — designed for the regulator, not just the debugger
- Adopting MCP in the large: wrapping legacy systems (SOAP, mainframe) behind anti-corruption layers; the strangler-fig migration from bespoke function calling; registries, capability approval, change management
- Measuring success: reliability, latency, context coherence, and integration-cost reduction as KPIs
- Meridian, final state: four servers, one host, three delegated agents — the complete measured system

---

## Epilogue — The Evolving Protocol

- Reading the trajectory: the stateless core as infrastructure bet, the extensions ecosystem (Tasks and Apps as the template for what ships next), the registry as distribution
- Cross-protocol convergence: MCP alongside A2A and provider-native stacks; where the boundaries are settling
- Open problems: standardized evals, formal safety arguments for agent loops, capability attestation
- To the reader: build on the principles and the lifecycle contract, not on today's wire format

---

## Appendices

- **A.** 2026-07-28 method and message reference, with a map of the deprecated-features registry
- **B.** Transport decision matrix (STDIO vs Streamable HTTP; HTTP+SSE migration notes)
- **C.** Performance checklist — the one-page version of Chapter 17
- **D.** Security hardening checklist — the one-page version of Chapters 19–20
- **E.** The Meridian companion repository: four servers, one host, load-test harness, dashboards, and every trace reproduced in the book

---

## What changed from the previous draft, and why

| Change | Rationale |
|---|---|
| Reframed the whole book from DDIA-style architecture text to **HPBN-style protocol/performance text** | Per the new brief: readers should understand the protocol deeply enough to build *good* applications on it |
| New **Chapter 1 physics primer** (latency, tokens, cost) | HPBN's opening move; gives every later chapter a budget to spend against |
| Rewrote Chapter 2 around the **stateless core** (`_meta` envelope, `server/discover`, no handshake/sessions) | The old "stateless messages over stateful sessions" framing was inverted by 2026-07-28 |
| Merged old Ch 7 (Sampling) + Ch 8 (Elicitation) into new **Ch 8: MRTR** | Server-initiated requests were replaced by the MRTR pattern; Sampling and Roots are deprecated and now live in Legacy boxes |
| Old Ch 13 (async) rebuilt as **Ch 9: Extensions + Tasks** | Tasks is now an official extension with defined polling/update semantics — no need to hand-roll lifecycles |
| Added **Ch 10: MCP Apps** | First official extension, finalized 2026-01-26, adopted by major hosts; essential and previously absent |
| Added **Part IV: Performance Engineering** (measure → optimize → operate) | The HPBN heart; absorbs and extends old Ch 11–12 |
| New **Ch 4: Authorization on the Wire** early, mirroring HPBN's TLS chapter | Auth is unavoidable mechanics with real latency costs; deep policy/audit stays in Part V |
| Updated auth stack: **CIMD**, issuer binding, RFC 9207; DCR to a Legacy box | 2026-07-28 deprecated RFC 7591 registration |
| Transport chapter rewritten: required routing headers, `subscriptions/listen`, no resumability, stateless scaling | Matches the 2026-07-28 wire reality; the old session-resumption material is gone from the spec |
| Caching threaded throughout (Ch 3, 6, 17): `ttlMs`, `cacheScope`, deterministic ordering, prompt caches | New spec features that are pure HPBN material |
| **Meridian redefined**: from fictional narrative to instrumented, runnable reference app | HPBN persuades with measurements; the companion repo makes every claim reproducible |
| Added A2A to the Chapter 1 positioning discussion | The comparison readers will actually ask about in 2026 |
| Added **Legacy boxes** + pinned the book to 2026-07-28 with the feature-lifecycle policy | Fast-moving spec; the deprecation window is the book's shelf-life story |

**Scale:** 20 chapters + epilogue, roughly 400–480 pages — comparable to HPBN's 19 chapters.
