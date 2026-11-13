# o11yag-oa Dynatrace dashboard

A Dynatrace **platform dashboard** built for this repo's data: what OneAgent
captures, and the audit records the services write to stdout. Upstream's
dashboard (o11yag-otel) is built on its `o11yag.*` metrics and custom span
attributes, none of which exist here; this one was rebuilt from scratch and every
query was run against the live tenant before it went in (2026-10-01).

## Deploy

```
./deploy.sh                 # create, or update the one in .dashboard-id
./deploy.sh <document-id>   # update a specific dashboard
```

`deploy.sh` posts `o11yag.json` to the Dynatrace **Document API** using
`$DT_ENVIRONMENT` and `$DT_PLATFORM_TOKEN` — the same two variables the
Dynatrace MCP plugin reads. `dtctl` is not needed and neither is a manual
upload. A run without `.dashboard-id` creates a new dashboard and records its id;
later runs update that one and overwrite any edits made in the UI.

Worth knowing, because it is not obvious: **the tenant's MCP gateway exposes
only read tools** (DQL execution, document *search*, the Davis analyzers). There
is no MCP tool that creates a document. The REST API underneath it accepts
writes perfectly well with the same token — the gap is in the MCP surface, not
in the permissions.

Two API details that cost a round trip each:

- `GET /documents/{id}` returns a **multipart** body (metadata part + content
  part), not JSON. The version needed for an update comes from
  `GET /documents/{id}/metadata`.
- An update is `PATCH` with `?optimistic-locking-version=<current>`, and its
  response nests the metadata under `documentMetadata`, whereas `POST` returns
  it flat.

The file is also a valid **Dashboards → Upload** import if you would rather do it
by hand.

## Two data sources

**Audit records.** `o11y.audit()` writes one JSON object per line to stdout and
OneAgent log monitoring ingests it. Two things about how it arrives, both
measured:

- **Each audit line is merged into the log line printed just before it.** A log
  record's `content` reads `…HTTP Request: POST … "HTTP/1.1 200 OK"\n{"audit.event.type": …}`.
  Log monitoring treats the JSON line as a continuation, so the record is not
  JSON and nothing is parsed into fields.
- **Nothing is lost.** Over two hours the per-service count of audit lines in
  Grail matched `kubectl logs` (122 vs 123, the difference at the window edge).

So every audit tile starts with the same block, which splits `content` back into
lines, keeps the audit ones and parses them:

```
fetch logs
| filter k8s.namespace.name == "o11yag-oa" and contains(content, "o11yag.ticket.handled")
| fieldsAdd line = splitString(content, "\n")
| expand line
| filter startsWith(line, "{\"audit.event.type\"")
| parse line, "JSON:a"
| fieldsAdd ev = a[`audit.event.type`]
| filter in(ev, {"o11yag.ticket.handled"})
```

The `contains` on the event name is there to cut the scan before the split; the
`in(ev, …)` after the parse is the real filter. An OpenPipeline rule that parsed
the JSON at ingest would make all of this a plain `filter`, and is the obvious
next step if the dashboard is kept.

**OneAgent spans.** Server spans for every Flask endpoint, `openai.chat` and
`openai.embeddings` spans with `gen_ai.*` attributes (model alias, input and
output tokens, temperature, finish reason), and Redis client spans from the
approvals service. `dt.service.name` is `app` for every Flask service (it seems
to come from the Flask app object), so span tiles group by `k8s.workload.name`.
`k8s.deployment.name` arrives masked (`o*yag-…`) and is not used.

## Tiles

| Section | Tiles | Source |
|---|---|---|
| Tickets | Tickets handled · Error rate · Cost · LLM calls per ticket · Volume by outcome · Mix by intent · Who decided the intent · LLM calls per ticket by intent · Latency p90 by intent · Tokens by intent · Cost by intent | `o11yag.ticket.handled` |
| Tools, approvals, refunds | MCP tool calls · Approval wait · Refund decisions · Refunds issued by the CRM | `o11yag.tool.*`, `o11yag.approval.decided`, `o11yag.crm.refund_issued` |
| Answers and security | Retrieval top score · Answer quality · Verdicts by who decided · Human feedback · Security detections | `o11yag.answer.generated`, `o11yag.retrieval.ungrounded`, `o11yag.feedback.received`, `o11yag.security.*` |
| What OneAgent sees | LLM calls · LLM call latency p90 · LLM tokens by service and model alias · Endpoints · Which services share a trace · Services with no spans | spans |
| What OneAgent does not capture | markdown | measured |

The ticket tiles answer the same questions as upstream's KPI rows (per resolved
ticket, not per LLM call; loop depth as the baseline; the intent mix as a
routing check). The difference is that upstream emits the numbers as metrics and
this dashboard computes them from audit records at query time.

**Who decided the intent** is new here and worth a look: on 2026-10-01, 122 of
123 tickets were routed by the keyword rules because `qwen:0.5b` did not answer
with a usable label, while the LLM call was still made for every ticket. The LLM
spans alone would suggest the model routes the tickets.

## What OneAgent does not capture here

Measured 2026-10-01 over two hours of loadgen traffic; the last tile on the
dashboard says the same.

- **The MCP hop.** `o11yag-mcp-crm` (uvicorn/ASGI) produces no spans, and the
  action worker's MCP calls (httpx2) produce no client spans. Tool calls are only
  visible as audit records.
- **The model gateway.** `o11yag-litellm` produces no spans. Ollama's own spans
  land in traces of their own, not joined to the LLM call behind them.
- **loadgen, Qdrant, Redis server.** No spans.
- **Agent structure.** No step, tool, judge or approval-wait spans, and no
  record of who chose each tool call (upstream's `step.decided_by`).
- **A trace id on audit records.** None, so audit records cannot be joined to
  traces. Upstream's refunds tile joined spans to audit logs; this one cannot.

What does join: orchestrator → knowledge worker or action worker → approvals is
one trace.

## Known gaps

- **Empty in the healthy case:** Human feedback (nothing sends feedback
  automatically) and Security detections when no attack is switched on. An empty
  tile is indistinguishable from a broken one, so check **Tickets handled**
  first.
- **Timed-out approvals have no decided record**, so they are missing from
  Approval wait and Refund decisions. They show up in MCP tool calls as
  `blocked (timeout)`.
- **Cost is priced, not measured.** `COST_PER_1K_TOKENS_USD` prices tokens as if a
  hosted model were behind LiteLLM.
- **Query cost.** Every audit tile scans the namespace's logs and parses JSON at
  query time. Fine at this volume; an OpenPipeline parse rule is the fix if it
  is not.
