# Telemetry — who emits what

o11yag's telemetry comes from four sources, and they are easy to confuse because
they all end up in the same traces and the same Dynatrace tenant. This page says
which source produces which signal, and where in the code it is set up.

| Source | What it is | Set up in |
|---|---|---|
| **OpenLLMetry** | `traceloop-sdk` — auto-instruments the OpenAI client (every LLM call through LiteLLM) and the Qdrant client, and provides the `@workflow` / `@agent` / `@task` / `@tool` decorators | `Traceloop.init()`, `<service>/o11y.py:89` |
| **OpenTelemetry (ours)** | The OTel SDK used directly: our business metrics, the audit log records, and hand-made spans and span attributes | `<service>/o11y.py:68-82`; spans in the service code |
| **HTTP instrumentors** | OTel contrib packages that patch an HTTP library: `requests` (outbound), `httpx` (Qdrant only), Flask (inbound), ASGI (inbound, MCP server only) | `<service>/o11y.py:118-133`; ASGI at `mcp-crm/server.py:150-152` |
| **MCP SDK** | The `mcp` Python SDK's built-in OTel support, on by default — nothing to configure | none in our code; it ships enabled |

All four write to **one** set of OTel providers, so they share trace ids and one
exporter: OTLP/HTTP to the collector (`collector/k8s/o11yag-collector.yaml`),
which forwards to Dynatrace. `o11y.py` is the same file copied into every
service.

## How they fit together

OpenLLMetry is not a separate telemetry system — it *is* OpenTelemetry.
`Traceloop.init()` installs an ordinary OTel tracer provider with an OTLP
exporter, and everything else attaches to it.

One ordering rule holds it together (`o11y.py:58-66`): **our meter provider is
installed before `Traceloop.init()`.** OTel refuses to replace a provider once
one is set, so whichever goes first wins. Ours goes first, which means
OpenLLMetry's `gen_ai.*` metrics are exported through our meter provider too.
The warning `Overriding of current MeterProvider is not allowed` in the logs is
this working as intended.

## Metrics sent to Dynatrace

Measured on tenant `nzu34348` on 2026-09-30: these are the metric keys the
tenant received from the `o11yag_*` services, not a list read off the code.
The **MCP SDK emits no metrics** — spans only — so no row has MCP as its
source.

Type and unit for our metrics come from `o11y.py`. For the library metrics
(OpenLLMetry, HTTP) they are what the OpenTelemetry semantic conventions
specify, and were **not** checked against the tenant.

| Metric | Type · unit | Source | Emitted by | Dimensions | What it answers |
|---|---|---|---|---|---|
| `o11yag.tasks` | counter · 1 | OpenTelemetry (ours) | orchestrator | `intent`, `tenant`, `outcome` | Tickets handled, and how each ended (`ok`, `blocked`, `escalated`, `incomplete`, `error`) |
| `o11yag.task.latency` | histogram · ms | OpenTelemetry (ours) | orchestrator | `intent`, `tenant` | Wall clock per ticket, including any human approval wait |
| `o11yag.task.llm_calls` | histogram · 1 | OpenTelemetry (ours) | orchestrator | `intent`, `tenant` | LLM calls per ticket — the loop-depth signal |
| `o11yag.task.tokens` | counter · 1 | OpenTelemetry (ours) | orchestrator | `intent`, `tenant` | Tokens per ticket, rolled up across every hop |
| `o11yag.task.cost.usd` | counter · usd | OpenTelemetry (ours) | orchestrator | `intent`, `tenant` | Cost per ticket. **Priced, not measured** — see the README's limits |
| `o11yag.approval.wait` | histogram · ms | OpenTelemetry (ours) | action-worker | `tool`, `decision` | Time spent waiting at the approval gate |
| `o11yag.tool.calls` | counter · 1 | OpenTelemetry (ours) | action-worker | `tool`, `outcome`, `approved` | MCP tool calls, and whether the gate let them through |
| `o11yag.retrieval.top_score` | histogram · 1 | OpenTelemetry (ours) | knowledge-worker | `collection` | Best similarity score — did retrieval find anything relevant |
| `o11yag.retrieval.hits` | histogram · 1 | OpenTelemetry (ours) | knowledge-worker | `collection` | Chunks returned |
| `o11yag.answer.quality` | counter · 1 | OpenTelemetry (ours) | knowledge-worker | `verdict`, `decided_by` | Answers graded for groundedness, and whether the judge model or the heuristic decided |
| `o11yag.security.events` | counter · 1 | OpenTelemetry (ours) | knowledge-worker, action-worker | `kind`, `action`, `source` | Adversarial content found in agent input (`prompt_injection`, `tool_poisoning`, `tool_catalogue_changed`) |
| `o11yag.feedback` | counter · 1 | OpenTelemetry (ours) | orchestrator | `rating`, `intent`, `tenant` | Human ratings of an answer. Only sent when someone posts feedback — **not present on the tenant when this was measured** |
| `gen_ai.client.token.usage` | histogram · tokens | OpenLLMetry | orchestrator, knowledge-worker, action-worker | set by the library, incl. `gen_ai.token.type` (`input` / `output`) | Tokens per LLM call |
| `gen_ai.client.operation.duration` | histogram · s | OpenLLMetry | orchestrator, knowledge-worker, action-worker | set by the library | Latency per LLM call |
| `gen_ai.client.generation.choices` | counter · 1 | OpenLLMetry | orchestrator, knowledge-worker, action-worker | set by the library | Completions returned per LLM call |
| `llm.openai.embeddings.vector_size` | counter · 1 | OpenLLMetry | knowledge-worker | set by the library | Embedding vector size (embedding calls for retrieval) |
| `http.server.duration` | histogram · ms | HTTP (Flask; ASGI for mcp-crm) | all five services | set by the library | Inbound request latency |
| `http.server.active_requests` | up-down counter · 1 | HTTP (Flask; ASGI for mcp-crm) | all five services | set by the library | Requests in flight |
| `http.server.request.size` | histogram · By | HTTP (ASGI) | mcp-crm | set by the library | Inbound request body size |
| `http.client.duration` | histogram · ms | HTTP (`requests` / `httpx`) | orchestrator, knowledge-worker, action-worker | set by the library | Outbound request latency: service-to-service calls, approval polling, Qdrant |

Two things the table implies:

- **Per call vs per ticket.** OpenLLMetry's `gen_ai.*` metrics are per LLM call.
  Nobody budgets per call, so `o11yag.task.*` rolls the same work up per
  resolved ticket — that roll-up is the reason our metrics exist
  (`o11y.py`, `task_finished`).
- **No `http.client.*` from the MCP hop.** The MCP SDK sends its HTTP through
  `httpx2`, which the `httpx` instrumentor does not patch. The MCP hop shows up
  as spans (below), not as HTTP client metrics.

To re-check the list against the tenant:

```
metrics
| filter startsWith(metric.key, "o11yag") or startsWith(metric.key, "gen_ai")
      or startsWith(metric.key, "llm") or startsWith(metric.key, "http.")
| summarize series = count(), by: {metric.key}
```

## Spans

| Source | Example span | Where |
|---|---|---|
| OpenLLMetry — decorators | `mcp_tool_call.tool` (with `gen_ai.tool.name`, `gen_ai.tool.call.arguments`) | `@tool_span(name="mcp_tool_call")`, `action-worker/app.py:60` |
| OpenLLMetry — auto-instrumentation | the LLM call spans (model, tokens), the Qdrant query spans | nothing in our code; `Traceloop.init()` |
| OpenTelemetry (ours) | `step_N` (`step.decided_by`, `step.fallback_reason`), `approval_wait` | `tracer.start_as_current_span`, `action-worker/app.py:215`, `action-worker/approvals.py:20` |
| HTTP instrumentors | a CLIENT span per `requests.post` (e.g. orchestrator → worker, `orchestrator/app.py:110`), a SERVER span per inbound Flask request, `POST /mcp` on the MCP server | `o11y.py:118-133`, `mcp-crm/server.py:150-152` |
| MCP SDK | `MCP send tools/call` (client), `tools/call issue_refund` (server, scope `mcp-python-sdk`) | nothing in our code; ships enabled |

**Trace context crosses the MCP hop two ways.** The MCP SDK puts it in the
JSON-RPC `_meta` field, which joins the MCP spans to the trace. It does not
join the HTTP request that carries them, so the repo adds that by hand: an
`httpx2` event hook injects `traceparent` on the client
(`action-worker/mcp_client.py:48-57`), and the ASGI middleware reads it on the
server. `mcp-crm/server.py` and `action-worker/mcp_client.py` explain this at
the top of each file.

**Prompts and completions are not on spans.** OpenLLMetry records them on
spans by default; the ConfigMaps set `TRACELOOP_TRACE_CONTENT=false` because
in a support flow that is customer data in the trace store.

## Logs

The logs sent to Dynatrace are our own **audit records** (OpenTelemetry, ours).
They are written with `o11y.audit(...)` and carry `audit.event.type`,
`audit.trace_id` / `audit.span_id` and the event's fields:

| Area | Event types |
|---|---|
| Tool calls | `o11yag.tool.called` (with the CRM's `audit.result`), `o11yag.tool.blocked` |
| Approval gate | `o11yag.approval.requested`, `o11yag.approval.decided` |
| System of record | `o11yag.crm.refund_issued` |
| Answers | `o11yag.answer.generated`, `o11yag.answer.withheld`, `o11yag.retrieval.ungrounded` |
| Feedback | `o11yag.feedback.received` |
| Security | `o11yag.security.injection_detected`, `o11yag.security.tool_poisoned`, `o11yag.security.tool_catalogue_changed` |

They exist because spans are the wrong home for "did we refund this customer":
spans are sampled and expire with trace retention. Customer text and tool
arguments go here instead of onto spans. The dashboard's refunds tile joins
these records to the spans by trace and span id to show what the CRM actually
did.

Container stdout/stderr is **not** shipped: the collector only has an `otlp`
receiver, so anything a process prints to the console is gone with its pod.
