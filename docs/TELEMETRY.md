# Telemetry — what this repo emits, and what upstream emitted

o11yag-oa is o11yag-otel with the OpenTelemetry code taken out, so that
Dynatrace OneAgent can be measured on what it finds by itself. That leaves two
sources of telemetry, and only one of them is this repo's code:

| Source | What it is | Set up in |
|---|---|---|
| **Audit records** | One JSON object per line on stdout, written by `o11y.audit(...)`. The only telemetry the services produce themselves. | `<service>/o11y.py` (`audit`) |
| **OneAgent** | Injected into every pod in the `o11yag-oa` namespace by the Dynatrace Operator. Whatever traces, LLM spans, service metrics and logs it captures. | `dynatrace/k8s/dynakube.yaml` |

Everything else on this page is the comparison: what upstream emits, and what has
become of each signal here. **What OneAgent actually captures has not yet been
measured.** Where a row says "OneAgent: not yet measured", that is the honest
state, not a placeholder for "probably yes".

## What this repo emits

### Audit records

`o11y.audit(event_type, **fields)` writes one line to stdout through its own
logging handler, so each line parses as a single JSON object:

```json
{"audit.event.type": "o11yag.ticket.handled", "audit.schema.version": "1", "audit.ticket_id": "TK-4059b4c0", "audit.intent": "question", ...}
```

- `audit.event.type` and `audit.schema.version` (currently `"1"`) are always present.
- Every other field is `audit.<name>`. Fields passed as `None` are left out.
- Strings, numbers and booleans are written as they are; anything else (lists,
  dicts — tool arguments, CRM results, doc ids) is JSON-encoded into a string.
- **There is no `audit.trace_id` or `audit.span_id`.** Upstream reads them off the
  active OTel span; with no OTel here there is no span to read.

They are meant to be collected by OneAgent log monitoring, which the DynaKube
scopes to this namespace. Customer text and tool arguments go here and nowhere
else.

| Event type | Emitted by | What it records |
|---|---|---|
| `o11yag.ticket.handled` | orchestrator | The per-ticket roll-up: intent and who decided it (`intent_source`), outcome, quality verdict, latency, LLM calls, tokens, cost, and the prompt/response unless `AUDIT_LOG_CONTENT=false` |
| `o11yag.feedback.received` | orchestrator | A human rating posted to `/feedback` |
| `o11yag.answer.generated` | knowledge-worker | The answer, the doc ids it was grounded on, top score, tokens, the judge's verdict and who decided it |
| `o11yag.answer.withheld` | knowledge-worker | An answer the judge marked unsupported and that was replaced with the refusal |
| `o11yag.retrieval.ungrounded` | knowledge-worker | A question refused because retrieval found nothing above `MIN_SCORE` |
| `o11yag.security.injection_detected` | knowledge-worker | A retrieved document that contained instructions |
| `o11yag.tool.called` | action-worker | An MCP tool call, its arguments, the CRM's result, and the approval state |
| `o11yag.tool.blocked` | action-worker | A consequential tool call the approval gate did not let through |
| `o11yag.security.tool_poisoned` | action-worker | A tool description that contained instructions |
| `o11yag.security.tool_catalogue_changed` | action-worker | The MCP tool catalogue no longer matching its pinned or first-seen digest |
| `o11yag.approval.requested` | approvals | A request arriving at the gate |
| `o11yag.approval.decided` | approvals | The decision, who made it (`human` or `auto-approver`) and how long it waited |
| `o11yag.crm.refund_issued` | mcp-crm | A refund written to the system of record |

The list comes from the `o11y.audit(` calls in `*/app.py` and `mcp-crm/server.py`.

### Nothing else

- The business-metric functions in `o11y.py` (`task_finished`, `tool_called`, ...)
  are **no-ops**. They keep upstream's names so the service code reads the same.
- `/chat` still returns `trace_id`, and it is always `""`.
- `opentelemetry-api` is still installed, as a hard dependency of `mcp` 2.x, and
  the MCP SDK calls it internally. With no OTel SDK configured those calls record
  nothing — unless OneAgent picks them up, which is one of the things to measure.

### Finding the audit records

```
fetch logs
| filter k8s.namespace.name == "o11yag-oa"
| filter contains(content, "audit.event.type")
```

This matches on the raw line on purpose. **Whether OneAgent log monitoring and
Grail parse the JSON into fields has not been verified**, so a query that filters
on `audit.event.type` as a field may return nothing even while the records arrive.

## What upstream emits, and its status here

Upstream (o11yag-otel) instruments its five app services with OpenLLMetry
(`traceloop-sdk`), the OpenTelemetry SDK, OTel HTTP instrumentors and the MCP
SDK's own OTel support, and exports all of it over OTLP through an OTel Collector.
None of that is in this repo. The tables below are upstream's inventory, kept as
the checklist for what OneAgent does and does not recover.

Two differences in coverage to keep in mind when comparing:

- **OneAgent is injected into every pod in the namespace**, including loadgen,
  LiteLLM, Ollama, Qdrant and Redis. Upstream instruments only orchestrator,
  knowledge-worker, action-worker, mcp-crm and approvals. OneAgent may therefore
  show things upstream never had, such as the loadgen → orchestrator call.
- **The injected OneAgent code modules are 1.347.49.** Dynatrace documents 1.339
  as the minimum for capturing OpenAI-SDK LLM calls, so the version is not the
  obstacle; whether it captures them here is still to be measured.

### Metrics

Upstream's list was measured on the shared tenant on 2026-09-30.

| Metric | Upstream source | Emitted by (upstream) | What it answers | Status here |
|---|---|---|---|---|
| `o11yag.tasks` | OTel SDK (ours) | orchestrator | Tickets handled, by `intent`, `tenant`, `outcome` | **gone** (no-op stub). Outcome and intent are in `o11yag.ticket.handled` |
| `o11yag.task.latency` | OTel SDK (ours) | orchestrator | Wall clock per ticket | **gone** (no-op stub). `latency_ms` in `o11yag.ticket.handled` |
| `o11yag.task.llm_calls` | OTel SDK (ours) | orchestrator | LLM calls per ticket — the loop-depth signal | **gone** (no-op stub). `llm_calls` in `o11yag.ticket.handled` |
| `o11yag.task.tokens` | OTel SDK (ours) | orchestrator | Tokens per ticket | **gone** (no-op stub). `tokens` in `o11yag.ticket.handled` |
| `o11yag.task.cost.usd` | OTel SDK (ours) | orchestrator | Cost per ticket (priced, not measured) | **gone** (no-op stub). `cost_usd` in `o11yag.ticket.handled` |
| `o11yag.approval.wait` | OTel SDK (ours) | action-worker | Time waiting at the approval gate | **gone** (no-op stub). `waited_s` in `o11yag.approval.decided` |
| `o11yag.tool.calls` | OTel SDK (ours) | action-worker | MCP tool calls and whether the gate let them through | **gone** (no-op stub). One `o11yag.tool.called` / `.blocked` record per call |
| `o11yag.retrieval.top_score` | OTel SDK (ours) | knowledge-worker | Best similarity score | **gone** (no-op stub). `top_score` in `o11yag.answer.generated` / `.retrieval.ungrounded` |
| `o11yag.retrieval.hits` | OTel SDK (ours) | knowledge-worker | Chunks returned | **gone** (no-op stub). `hits` only in `o11yag.retrieval.ungrounded` |
| `o11yag.answer.quality` | OTel SDK (ours) | knowledge-worker | Groundedness verdicts, by `verdict` and `decided_by` | **gone** (no-op stub). `quality_verdict` / `quality_decided_by` in `o11yag.answer.generated` |
| `o11yag.security.events` | OTel SDK (ours) | knowledge-worker, action-worker | Adversarial content in agent input | **gone** (no-op stub). One `o11yag.security.*` record per detection |
| `o11yag.feedback` | OTel SDK (ours) | orchestrator | Human ratings of an answer | **gone** (no-op stub). `o11yag.feedback.received` |
| `gen_ai.client.token.usage` | OpenLLMetry | orchestrator, knowledge-worker, action-worker | Tokens per LLM call | OneAgent: not yet measured |
| `gen_ai.client.operation.duration` | OpenLLMetry | orchestrator, knowledge-worker, action-worker | Latency per LLM call | OneAgent: not yet measured |
| `gen_ai.client.generation.choices` | OpenLLMetry | orchestrator, knowledge-worker, action-worker | Completions per LLM call | OneAgent: not yet measured |
| `llm.openai.embeddings.vector_size` | OpenLLMetry | knowledge-worker | Embedding vector size | OneAgent: not yet measured |
| `http.server.duration` | OTel HTTP (Flask; ASGI for mcp-crm) | all five services | Inbound request latency | OneAgent: not yet measured (OneAgent has its own service metrics, under different keys) |
| `http.server.active_requests` | OTel HTTP (Flask; ASGI for mcp-crm) | all five services | Requests in flight | OneAgent: not yet measured |
| `http.server.request.size` | OTel HTTP (ASGI) | mcp-crm | Inbound request body size | OneAgent: not yet measured |
| `http.client.duration` | OTel HTTP (`requests` / `httpx`) | orchestrator, knowledge-worker, action-worker | Outbound request latency | OneAgent: not yet measured |

The twelve `o11yag.*` series are the loss that is certain: no agent can recover a
business metric that the code never computes. Their values survive only as fields
on the audit records, so any chart of them here has to be built from logs.

### Spans

| Upstream span | Upstream source | Status here |
|---|---|---|
| `support_ticket` workflow, `classify_intent`, `delegate_to_worker` tasks (orchestrator) | OpenLLMetry decorators | **gone** (decorators removed) |
| `knowledge_worker` agent; `retrieve`, `screen_retrieval`, `generate_answer`, `judge_answer` tasks | OpenLLMetry decorators | **gone** (decorators removed) |
| `action_worker` agent; `mcp_tool_call.tool` | OpenLLMetry decorators | **gone** (decorators removed) |
| `step_N` (`step.decided_by`, `step.fallback_reason`), `approval_wait` | OTel SDK (ours) | **gone** (span code removed) |
| LLM call spans (model, tokens) through the OpenAI client | OpenLLMetry auto-instrumentation | OneAgent: not yet measured |
| Qdrant query spans | OpenLLMetry auto-instrumentation | OneAgent: not yet measured |
| CLIENT span per `requests` call, SERVER span per inbound Flask request, `POST /mcp` on the MCP server | OTel HTTP instrumentors | OneAgent: not yet measured |
| `MCP send tools/call` (client), `tools/call <tool>` (server) | MCP SDK's built-in OTel | Still called by the SDK through `opentelemetry-api`; records nothing without an OTel SDK. Whether OneAgent captures them: not yet measured |

**Trace context across the MCP hop.** Upstream joins the action-worker and the
MCP server into one trace two ways: the MCP SDK writes the context into the
JSON-RPC `_meta` field, and an `httpx2` event hook in `action-worker/mcp_client.py`
writes a `traceparent` header that an ASGI middleware on the server reads. The
hook and the middleware are gone here. Whether the hop is one trace now depends
on OneAgent seeing `httpx2` on the client and uvicorn on the server — not yet
measured.

### Span attributes

| Upstream attribute | Set on | Status here |
|---|---|---|
| `traceloop.association.properties.*` (tenant, customer_id, ticket_id) | every span of a ticket, workers included | **gone**. Tenant and customer reach telemetry only through `o11yag.ticket.handled` |
| `gen_ai.tool.name`, `gen_ai.tool.call.arguments`, `mcp.server`, `mcp.transport` | `mcp_tool_call` | **gone** (tool and args are in `o11yag.tool.called`) |
| `approval.required`, `approval.decision`, `approval.id`, `approval.decided_by`, `approval.waited_ms` | `mcp_tool_call`, `approval_wait` | **gone** (in `o11yag.approval.decided`) |
| `step.index`, `step.decided_by`, `step.fallback_reason` | `step_N` | **gone**, with no audit replacement. Who decided each agent step is no longer recorded anywhere |
| `agent.loop.steps`, `agent.loop.max_steps`, `agent.loop.repeated`, `agent.loop.terminated` | `action_worker` | **gone**. `steps`, `repeated_calls`, `terminated` are only in the `/act` response, which nothing records |
| `mcp.tools.available`, `mcp.tools.digest`, `mcp.tools.baseline`, `mcp.tools.changed`, `mcp.tools.digest.expected` | `action_worker` | **gone**. When unpinned, the first digest a pod sees is logged once; a change is in `o11yag.security.tool_catalogue_changed` |
| `security.tool_poisoning.*`, `security.injection.*` | `action_worker`, `screen_retrieval` | **gone** (detections are in the `o11yag.security.*` records) |
| `rag.hits`, `rag.top_score`, `rag.doc_ids`, `rag.scores`, `rag.grounded` | `retrieve` | **gone** (top score and doc ids are in `o11yag.answer.generated`) |
| `quality.verdict`, `quality.decided_by`, `quality.reason`, `quality.judge.*` | `judge_answer` | **gone** (verdict, decider and reason are in `o11yag.answer.generated`) |
| `error.kind` (`mcp_unavailable`) | `action_worker`, `mcp_tool_call` | **gone** |
| `o11yag.audit.sink_error` | the active span, when an audit write failed | **gone**; the failure is a warning in the pod log |

### Logs

Upstream sends the audit records as OTLP log records with the fields above as
attributes, plus `audit.trace_id` / `audit.span_id`, and ships nothing from
container stdout. Here it is the reverse: the records are stdout lines, with no
trace or span id. That breaks upstream's join between a refund's span and its
`o11yag.tool.called` record, which the dashboard's refunds tile depends on.

**Prompts and completions.** Upstream keeps them off spans
(`TRACELOOP_TRACE_CONTENT=false`) and puts them in the audit records. Here they
are still in the audit records. Whether OneAgent's LLM capture records prompt
text on spans, and whether that is off by default, is not yet measured.
