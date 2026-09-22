# o11yag

A minimal, runnable reference architecture for the way enterprises actually build
LLM agents today — instrumented with [OpenLLMetry](https://github.com/traceloop/openllmetry)
and exported to Dynatrace, with the telemetry aimed specifically at the questions
agent stacks cannot currently answer.

Built to run on [Kubernetes on Docker Desktop](https://www.docker.com/blog/how-to-set-up-a-kubernetes-cluster-on-docker-desktop/)
and [WSL](https://learn.microsoft.com/en-us/windows/wsl/install), but nothing in
it is local-only.

> **New to agentic architecture?** Start with
> [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md) — a guided read of this codebase
> for someone who knows observability but not agents.

## Why

The interesting half of agent observability is not "instrument the LLM call".
OpenLLMetry does that for you in one line: model, tokens, cost, prompts, vector
queries, all of it, for free. Start there and you are done with the easy part on
day one.

What nothing gives you is the part that makes agents different from every service
APM was designed for:

- **Failure is silent and semantic.** The agent returns `200 OK` with a wrong
  answer. No span has `error=true`. Every RED metric is green.
- **There is no baseline.** The same ticket legitimately takes 3 LLM calls today
  and 11 tomorrow. Classical APM assumes a stable call graph; there isn't one.
- **Nobody budgets per LLM call.** They budget per resolved ticket — and that
  roll-up doesn't exist until you emit it.
- **The tool side is a blind spot.** The model half is thoroughly instrumented.
  "What did the agent actually do to my systems of record" is not.

o11yag builds the standard architecture, then closes those four gaps and shows
what it costs to close them.

## Architecture

The shape is the one the 2026 vendor guides converge on: an orchestrator that
triages and delegates, specialised workers, RAG for private knowledge, MCP for
tools, a model gateway in front of every LLM call, and a human approval gate in
front of anything consequential.

```
                            ┌─────────┐
                            │ loadgen │  a ticket every 20s
                            └────┬────┘
                                 │ POST /chat
                                 ▼
                      ┌──────────────────────┐
             ┌────────│     orchestrator     │────────┐
             │        └──────────┬───────────┘        │
    delegate │                   │ chat               │ delegate
             ▼                   │                    ▼
 ┌──────────────────────┐        │     ┌──────────────────────┐
 │   knowledge-worker   │        │     │     action-worker    │
 │         (RAG)        │        │     │     (agent loop)     │
 └───┬──────────────┬───┘        │     └──┬─────────┬──────┬──┘
     │ retrieve     │ chat       │   chat │   MCP   │      │ approve?
     ▼              │  + embed   │        │         ▼      │
┌───────────┐       │            │        │  ┌───────────┐ │
│   qdrant  │       └──────┐     │   ┌────┘  │  mcp-crm  │ │
│ (vectors) │              ▼     ▼   ▼       │  orders,  │ │
└───────────┘         ┌──────────────────┐   │  refunds, │ │
                      │     litellm      │   │  tickets  │ │
                      │    (gateway)     │   └───────────┘ │
                      └─────────┬────────┘                 ▼
                                │              ┌──────────────────┐
                                ▼              │    approvals     │──▶ human
                      ┌──────────────────┐     │      (gate)      │
                      │      ollama      │     └─────────┬────────┘
                      │  qwen:0.5b       │               │ pending
                      │  nomic-embed-text│               ▼
                      └──────────────────┘     ┌──────────────────┐
                                               │      redis       │
  OTLP from the five                           └──────────────────┘
  instrumented services
  (loadgen emits none)
          │
          ▼
 ┌──────────────────┐
 │  otel-collector  │──▶ Dynatrace · traces · metrics · logs
 └──────────────────┘
```

Eleven pods, every edge. Two of them are shared infrastructure that everything
leans on, and that is the point rather than an artefact of the drawing: **every
chat and embedding call goes through `litellm`**, so no service holds a provider
name or ever reaches Ollama directly, and **every instrumented service exports
through one Collector**, so swapping the backend is a Collector change.

MCP wraps the **systems of record only**. The knowledge worker talks to Qdrant
directly, because in practice you don't MCP-wrap your own vector store — you
MCP-wrap the CRM, the ticketing system, the vendor API you didn't write.

## Components

| Component | Port | Stack | Role |
|-----------|------|-------|------|
| **o11yag-orchestrator** | 8000 | Flask | Supervisor. Classifies intent, delegates to one worker, owns the per-ticket roll-up and the conversation audit record. |
| **o11yag-knowledge-worker** | 8001 | Flask + Qdrant | RAG over the support knowledge base. Refuses to answer off a weak retrieval. |
| **o11yag-action-worker** | 8002 | Flask + MCP client | The agent loop. Picks tools, calls them over MCP, routes consequential ones through the gate. |
| **o11yag-mcp-crm** | 8003 | MCP Python SDK | MCP server over Streamable HTTP exposing the fake system of record. |
| **o11yag-approvals** | 8004 | Flask + Redis | Human-in-the-loop gate, with a reviewer page at `/`. |
| **LiteLLM** | 4000 | LiteLLM proxy | Model gateway. Every LLM and embedding call goes through it. |
| **Qdrant** | 6333 | Qdrant | Vector store, seeded by the knowledge worker on first boot. |
| **Ollama** | 11434 | Ollama | `qwen:0.5b` for chat, `nomic-embed-text` for embeddings. |
| **Redis** | 6379 | Redis | Approval state. |
| **OTel Collector** | 4317/4318 | contrib | OTLP in, Dynatrace out. Traces, metrics **and logs**. |
| **loadgen** | — | Python | Support tickets on a timer. Emits no telemetry of its own. |

> **LiteLLM is not LightLLM.** LiteLLM is a pure-Python proxy that forwards to a
> backend — no GPU, no model weights. LightLLM is a GPU inference engine and is
> not used here.

## Verified against

The SDK surface this code targets was checked against real installs, not from
memory: `traceloop-sdk` **0.62.3** (`Traceloop.init(app_name, api_endpoint,
disable_batch)`, `set_association_properties`, the `workflow`/`task`/`agent`/`tool`
decorators), `mcp` **2.2.0** (`MCPServer`, `streamable_http_client`,
`Tool.input_schema`, `CallToolResult.structured_content`) and `openai` **3.14.1**.

**Run, and working:**

- All five instrumented services boot from a clean virtualenv built only from
  their own `requirements.txt`, with telemetry on, and answer `/health`.
- The MCP leg end to end — server up, client calling it. Every one of the 17 HTTP
  requests in a `list_tools` + two `call_tool` exchange carried the client's
  `traceparent`, matching the calling span's trace id.
- The agent loop against that live MCP server: `lookup_order` → `issue_refund`
  → done, refunding the order's own total rather than an amount asserted in the
  customer's message.

**Found only by running it in a cluster:** the MCP server's Host-header check.
Every tool call returned 421 because the SDK auto-allows localhost and nothing
else, which the loopback test above could not have caught — it was loopback.

**Not run:** anything needing a cluster. The Kubernetes deployment itself, the
Dynatrace export, Ollama, LiteLLM and the Qdrant seed path are all unexercised —
the manifests parse and the images build, but nothing has been deployed.

The boot test is the one that matters most, and it is why `smoke.sh` exists: the
services passed every static check — manifests parsed, Python compiled, imports
read correctly — while the orchestrator still died on its first line of real work
because a transitive dependency had quietly gone away. For this stack the
dependency graph is the fragile part, not the code, because the ecosystem is
mid-migration from `httpx` to `httpx2` and the SDKs disagree about where they are
in it.

## Conventions

- **Config** is environment-driven through a per-service `config.py`.
- **Containers** are multi-stage and non-root, with a read-only root filesystem
  and a `/tmp` `emptyDir` for scratch.
- **Deployment** is one self-contained Kubernetes manifest per component.
- **Image naming** is `o11yag-<service>`, tagged with a build timestamp.
- **Instrumentation** is `o11y.py`, copied verbatim into every instrumented
  service (each Docker build context is its own directory).
- **Namespaces**: span attributes follow `gen_ai.*` where OpenLLMetry's semantic
  conventions cover them, with `mcp.*`, `agent.*` and `rag.*` only for what has
  no equivalent. Metric *keys* are product-scoped `o11yag.*`. Audit record fields
  are `audit.*`. The split is deliberate: renaming a metric key orphans its
  history, renaming a span attribute only affects queries from that point on.

## Getting started

> Prerequisites: a Kubernetes cluster, `kubectl`, and Docker.

1. **Start Kubernetes on Docker Desktop** with headroom for Ollama:
   memory ≥ 8192, cpus ≥ 4.

2. **Set the Dynatrace variables**:
   ```
   export DT_API_TOKEN='your_token'
   export DT_TENANT='abc12345'      # first part of your Dynatrace URL
   ```
   The token needs the **Ingest metrics**, **Ingest OpenTelemetry traces** and
   **Ingest logs** scopes. `Ingest logs` is what carries the audit records —
   without it the Collector accepts them and Dynatrace rejects them, which is
   silent unless you read the Collector's own log.

3. **Build and deploy**:
   ```
   bash build_deploy.sh
   ```
   First start is slow: Ollama downloads two models (~700MB) before it is ready,
   and the knowledge worker seeds Qdrant on its first boot.

4. **Allow-list span attributes on the tenant.** Dynatrace does **not** persist
   custom span attributes by default — it accepts them and silently drops them,
   listing what it discarded in `supportability.non_persisted_attribute_keys`.
   Until this is configured you lose `agent.loop.*`, `rag.*`, `mcp.*`, `step.*`
   **and** everything OpenLLMetry emits: `gen_ai.usage.*`, `gen_ai.request.model`,
   and the `traceloop.association.properties.*` that carry tenant / customer /
   ticket. Metrics are a separate pipeline and are unaffected, so the dashboard
   looks healthy while the traces are hollow.

   Check with:
   ```
   fetch spans, from: now() - 10m
   | filter matchesValue(dt.service.name, "o11yag_*")
   | summarize dropped = countIf(isNotNull(`supportability.non_persisted_attribute_keys`)),
               total = count()
   ```
   `dropped` must be 0. It applies to newly ingested spans only, so judge it on
   fresh data.

5. **Watch it work**:
   ```
   kubectl get pods -n o11yag -w
   kubectl logs -l app.kubernetes.io/name=o11yag-loadgen -n o11yag -f
   ```

Redeploy without rebuilding with `bash build_deploy.sh --no-build`. `stop.sh`
removes the workloads but keeps the namespace, the secret and the volumes.

**Before deploying, smoke-test the images**:
```
bash smoke.sh
```
It boots each built image with telemetry off and every backing service pointed at
a dead port, and checks it answers `/health`. That catches the class of bug where
everything static passes — manifests parse, Python compiles — and the container
still dies on boot because a dependency is missing from `requirements.txt`. It is
much faster than finding out from a CrashLoopBackOff.

## The four gaps, and what closing each one cost

### 1. Silent semantic failure — *partially closed*

The cheapest honest guard is refusing to answer off a bad retrieval. The
knowledge worker checks the best similarity score against `MIN_SCORE` and says
"I don't have a policy that covers that" rather than letting the model invent
one, emitting an `o11yag.retrieval.ungrounded` audit record when it does.

That catches wrong answers *caused by bad retrieval*. It does not catch a
confidently wrong answer grounded in a correct document, which is the larger half
of the problem and needs a quality signal — an LLM judge, a thumbs-down from the
channel — that this version does not have. **This gap is not closed.** It is the
single most valuable thing to build next.

### 2. No baseline — closed, in the sense that the series now exists

`agent.loop.steps`, `o11yag.task.llm_calls` and `agent.loop.repeated` give you
the shape of the work per ticket, dimensioned by intent. Because there is no
fixed call graph, these series *are* the baseline: point Davis anomaly detection
at `o11yag.task.llm_calls` by intent and a task that suddenly needs three times
as many model calls becomes an alert instead of a bill.

`agent.loop.terminated = max_steps` deserves its own attention. It means the
agent ran out of budget and returned a partial answer. Nothing errors.

### 3. Per-ticket economics — closed

`o11yag.task.cost.usd`, `.tokens`, `.llm_calls` and `.latency` are recorded once
per resolved ticket, dimensioned by `intent` and `tenant`, from
`o11y.task_finished()`. Attribution rides Traceloop association properties
(`tenant`, `customer_id`, `ticket_id`) set once on the orchestrator and inherited
by every downstream span.

### 4. The tool blind spot — closed

Every MCP call produces a span (`gen_ai.tool.name`, `mcp.server`,
`mcp.transport`, arguments), a metric (`o11yag.tool.calls` by tool / outcome /
approval status) and an audit record. The MCP server is instrumented too, and
the SDK carries trace context in the JSON-RPC `_meta` field, so the call is one
trace end to end. The HTTP hop underneath it is the part that needs help — see
*Trace context* below for which half is free and which is not.

### Bonus: the approval gate is not invisible

`approval_wait` is its own span and `o11yag.approval.wait` its own metric. Left
inside the tool call, a reviewer who goes to lunch would swamp every latency
percentile in the stack. Subtract it from `o11yag.task.latency` to get machine
time.

## Signals reference

### Metrics (`o11yag.*`)

| Metric | Type | Dimensions | What it is |
|--------|------|------------|------------|
| `o11yag.tasks` | counter | intent, tenant, outcome | Tickets handled |
| `o11yag.task.latency` | histogram (ms) | intent, tenant | Wall clock per ticket, human wait included |
| `o11yag.task.llm_calls` | histogram | intent, tenant | Loop depth — the non-determinism signal |
| `o11yag.task.tokens` | counter | intent, tenant | Tokens per ticket |
| `o11yag.task.cost.usd` | counter | intent, tenant | Cost per ticket |
| `o11yag.approval.wait` | histogram (ms) | tool, decision | Human decision time |
| `o11yag.tool.calls` | counter | tool, outcome, approved | MCP tool calls |
| `o11yag.retrieval.top_score` | histogram | collection | Best similarity score |
| `o11yag.retrieval.hits` | histogram | collection | Chunks returned |

### Span attributes beyond what OpenLLMetry emits

| Attribute | Where | What it is |
|-----------|-------|------------|
| `agent.loop.steps` / `.max_steps` | action worker | Iterations taken, and the budget |
| `agent.loop.repeated` | action worker | Identical tool + identical args, called twice. No error, just waste |
| `agent.loop.terminated` | action worker | `done` or `max_steps` — the silent partial answer |
| `step.index` / `step.decided_by` | action worker | Which iteration, and whether the model or the fallback chose |
| `step.fallback_reason` | action worker | *Why* the model's answer was rejected — `unparseable`, `unknown_tool`, `missing:order_id`, `unknown:id`, `mode:rules` |
| `gen_ai.tool.name` / `.call.arguments` | action worker | The tool call |
| `mcp.server` / `mcp.transport` / `mcp.tools.available` | action worker | The MCP hop |
| `approval.required` / `.decision` / `.waited_ms` / `.decided_by` | action worker | The gate |
| `rag.hits` / `.top_score` / `.doc_ids` / `.scores` / `.grounded` | knowledge worker | Retrieval quality, not just latency |

The MCP SDK contributes spans of its own on top of these, from its
`mcp-python-sdk` tracer and with no opt-in: `MCP send <method>` on the client
side (`mcp.method.name`, `jsonrpc.request.id`) and a matching server span
carrying `gen_ai.operation.name` and `gen_ai.tool.name`. They are the SDK's
rather than ours, which matters when you allow-list attribute keys on the tenant.

### Audit records (OTLP logs, `audit.*`)

| Event type | Written by | Carries |
|------------|-----------|---------|
| `o11yag.ticket.handled` | orchestrator | The conversation, intent, outcome, cost, tokens |
| `o11yag.answer.generated` | knowledge worker | Question, answer, the documents it was grounded on |
| `o11yag.retrieval.ungrounded` | knowledge worker | A question the KB could not answer |
| `o11yag.tool.called` | action worker | Tool, arguments, result, approval status |
| `o11yag.tool.blocked` | action worker | A tool call the gate refused |
| `o11yag.approval.requested` / `.decided` | approvals | Who decided, how long they took |
| `o11yag.crm.refund_issued` | mcp-crm | The effect on the system of record |

Every record carries `audit.trace_id` and `audit.span_id`, so an auditor pivots
from a record to the trace and back.

```
fetch logs
| filter audit.event.type == "o11yag.tool.called"
| fields timestamp, audit.tool, audit.args, audit.approved,
         audit.ticket_id, audit.customer_id, audit.trace_id
| sort timestamp desc
```

## Dashboard

[`dashboards/o11yag.json`](dashboards/o11yag.json) — deploy with
`cd dashboards && ./deploy.sh`, which posts it to the Dynatrace Document API
using the same `$DT_ENVIRONMENT` / `$DT_PLATFORM_TOKEN` the Dynatrace MCP plugin
uses. (Manual **Dashboards → Upload** also works.) Twelve tiles over the `o11yag.*` metrics: per-ticket KPIs,
volume and intent mix, loop depth, latency, token and cost breakdown, MCP tool
calls, and retrieval quality. Every query was validated against a live tenant
before the file was written. See [`dashboards/README.md`](dashboards/README.md)
for what each tile is for and which metrics deliberately have no tile yet.

## Trace context

The stack is one trace from ticket to system of record. Three things make that
work, and it is worth being exact about which of them you get for free:

- **OpenLLMetry does not instrument HTTP.** `o11y.py` adds the Flask, requests
  and httpx instrumentations separately. Without them every service hop starts a
  new trace. Be precise about what each buys, because this stack is mostly *not*
  on httpx: `requests` covers the service-to-service hops, `httpx` covers
  `qdrant-client` and nothing else, and Flask covers the inbound span.
- **The MCP hop propagates itself, inside the JSON-RPC envelope.** This is free,
  and it is the part most write-ups (including an earlier version of this one)
  get wrong. The SDK's client dispatcher opens a CLIENT span per outbound
  request — the `MCP send <method>` spans in the waterfall, named after the
  method plus the tool where the params carry one, in
  `mcp/shared/jsonrpc_dispatcher.py` — and writes the W3C context into that
  request's `_meta` field on the way out (SEP-414). The server end reads it back
  in `OpenTelemetryMiddleware`, which `mcp.server.lowlevel.server` installs by
  default and which also sets `gen_ai.operation.name` and `gen_ai.tool.name` on
  `tools/call`. Nothing here configures any of it; it arrives with `mcp` 2.x.
  Because the carrier is `_meta` rather than a header, **it holds over stdio
  too** — trace continuity across MCP is no longer a property of the transport.
- **The HTTP layer underneath it does not propagate itself.** The obvious fix —
  add `opentelemetry-instrumentation-httpx` and let it propagate — silently does
  nothing here, because **MCP 2.x makes its HTTP calls through `httpx2`, a
  different package from `httpx`**. The same is true of the OpenAI SDK from 3.x
  on, so the httpx instrumentation does not cover the LLM calls either — those
  become spans because Traceloop wraps the OpenAI *client*, a level above the
  transport. So `action-worker/mcp_client.py` hands the transport its own
  `httpx2` client with an event hook that injects the W3C context on every
  request, and the MCP server adds ASGI middleware to read that header back.
  Be honest about what that second route buys: the tool call itself lands in the
  right trace either way, over `_meta`. The hand-injection is what keeps the
  transport spans — `POST /mcp`, and the session's `DELETE /mcp` — inside the
  ticket's trace instead of each rooting one of its own. The failure mode is not
  an error: every call still succeeds and nothing reports a problem.

## What this deliberately does not do

- **No evaluation loop.** The reference architectures all draw one. This has
  none. Drawing a box you didn't build is how a demo becomes a claim you can't
  support — see gap 1 above.
- **No security act.** Tool poisoning, indirect prompt injection through the RAG
  corpus, rug-pulled tool definitions: all real, none built. The injection
  document belongs in `knowledge-worker/kb.py` and is deliberately not there yet.
- **The approval gate auto-approves by default**, so loadgen can run unattended.
  With `AUTO_APPROVE=true` the gate proves nothing about governance — it is a
  timer wearing a reviewer's hat. Set it to `false`, port-forward the approvals
  page, and decide by hand for the honest version. Refunds over
  `AUTO_APPROVE_MAX_EUR` always wait for a person regardless, so there is always
  one path that genuinely stops.
- **Cost is priced, not measured.** A local model is free, which makes cost-per-
  ticket a column of zeroes and the attribution unprovable. `COST_PER_1K_TOKENS_USD`
  prices the tokens as if a hosted model were behind the gateway. It shows the
  attribution works; it is not a measurement. Replace it with the gateway's own
  reported cost the moment a real provider is behind LiteLLM.
- **`qwen:0.5b` is a poor tool-caller, and here is the measurement.** Over three
  hours of live traffic it produced usable arguments for `lookup_order` **2 times
  out of 51** — mostly passing a customer id as `{"id": "C-7"}` when the tool
  takes `order_id` — and it declared itself finished after a single step in **39
  of 45 runs**. `issue_refund` was never once attempted, so the approval gate was
  never exercised. Six runs reached a second step and spent it re-calling the
  same tool with identical arguments (`agent.loop.repeated`).

  Two things in the code respond to that, and it matters which does what:

  - **Argument validation** (`planner.validate_args`) holds the model to the
    JSON schema the MCP server advertises. Wrong or missing parameters are
    rejected and the deterministic rules take over, with `step.fallback_reason`
    recording what was wrong. This fixes bad *arguments*.
  - **`PLANNER_MODE=rules`** skips the model for tool selection entirely. This is
    what makes the refund → approval → gate path reliably demonstrable.

  Validation alone does **not** make refunds happen: a model that answers
  `{"done": true}` is well-formed and in-contract, so it is still accepted and
  the loop still ends early. Well-formed is not the same as sensible, and only
  the second switch addresses that. Never present a `rules` run as model
  reasoning — `step.decided_by` is in the trace precisely so you don't have to
  take anyone's word for it.
- **LiteLLM is unauthenticated** inside the namespace. The real shape is a virtual
  key per agent with its own budget.
- **Audit records land in the default log bucket.** Until an OpenPipeline rule
  routes `audit.event.type` to a bucket with its own retention, the
  record-keeping claim is not fully true. That is tenant configuration, not code.
- **`traceloop-sdk` imports `httpx` without declaring it.** Every service that
  installs Traceloop therefore lists `httpx` in its `requirements.txt` even
  though none of them use it directly. Drop that line and the pod dies on boot
  with `ModuleNotFoundError: No module named 'httpx'`, because nothing else
  pulls it in any more — `openai` 3.x and `mcp` 2.x are both on `httpx2`.
- **The MCP server validates the `Host` header**, and only auto-allows
  localhost. Reached by any other name — a Kubernetes Service, an Ingress host —
  it answers `421 Misdirected Request` and logs `Invalid Host header`, which
  looks like a routing fault rather than a policy decision. `MCP_ALLOWED_HOSTS`
  on its ConfigMap lists the names it may be called by. Extend that list when
  you expose it a new way; resist `MCP_DNS_REBINDING_PROTECTION=false`, which
  removes a real control for everyone to fix one address.
- **The MCP session is rebuilt per tool call**, which adds an initialize round
  trip to every call and inflates the tool latency you see. A real agent holds
  one session per conversation.

## Debug / dev

**Send a ticket** to the orchestrator:
```
kubectl port-forward service/o11yag-orchestrator 8000:8000 -n o11yag

curl -X POST http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"ticket_id":"TK-1","customer_id":"C-7","tenant":"acme",
       "text":"I want a refund for order ORD-1001, the headphones stopped working."}'
```

**The approvals page**:
```
kubectl port-forward service/o11yag-approvals 8004:8004 -n o11yag
# then open http://localhost:8004/
```

**Ask the knowledge worker directly**:
```
kubectl port-forward service/o11yag-knowledge-worker 8001:8001 -n o11yag
curl -X POST http://localhost:8001/answer -H 'Content-Type: application/json' \
  -d '{"ticket_id":"TK-2","text":"How long do refunds take?"}'
```

**Check the model gateway**:
```
kubectl port-forward service/litellm 4000:4000 -n o11yag
curl http://localhost:4000/v1/models
```

**If every MCP tool call fails with 421**, the action worker is reaching the
server by a name the allowlist doesn't cover. The server says so:
```
kubectl logs -l app.kubernetes.io/name=o11yag-mcp-crm -n o11yag | grep -i "host"
#   WARNING mcp.server.transport_security Invalid Host header: <the name>
#   INFO    ... "POST /mcp HTTP/1.1" 421 Misdirected Request
```
Add that name to `MCP_ALLOWED_HOSTS` in `mcp-crm/k8s/o11yag-mcp-crm.yaml` and
roll the pod. The server logs its allowlist on boot, so you can check what it
believes it accepts.

**Watch what the Collector is forwarding**: `kubectl logs -l app.kubernetes.io/name=o11yag-otel-collector -n o11yag -f`

### Turning the interesting cases on

| To see | Do |
|--------|-----|
| A real human approval | `AUTO_APPROVE=false` on the approvals ConfigMap, then use the page |
| A gate that stops regardless | Send a refund for `ORD-1004` (430 EUR, over the ceiling) |
| A refund actually reaching the gate | `PLANNER_MODE: "rules"` on the action worker ConfigMap — the local model rarely gets there on its own |
| A loop that runs out of budget | `MAX_STEPS: "1"` on the action worker ConfigMap |
| An ungrounded answer | Ask something the KB has no policy for |
| A blocked tool call | Deny an approval on the page |
