# o11yag

> **This is o11yag-oa, the OneAgent variant.** The same system as
> [o11yag-otel](https://github.com/zerogvt/o11yag-otel), with every line of
> OpenLLMetry/OpenTelemetry code removed, so that Dynatrace OneAgent can be
> measured on what it finds by itself. Same services, same service names, same
> behaviour; only the observability differs.
>
> - **No in-process telemetry.** No tracing SDK, no instrumentation packages, no
>   Collector. Traces are whatever OneAgent captures from the pods it injects.
> - **`*/o11y.py` is a stub** that keeps upstream's function names. The twelve
>   `o11yag.*` business metrics are no-ops. Audit records go to stdout as one
>   JSON object per line, for OneAgent log monitoring, and carry no trace id.
> - **`/chat` still returns `trace_id`**, and it is always empty.
> - **Still present:** `opentelemetry-api`, as a hard dependency of `mcp` 2.x.
>   The MCP SDK calls it internally. With no SDK configured those calls record
>   nothing, unless OneAgent picks them up — not yet measured.
>
> [`docs/TELEMETRY.md`](docs/TELEMETRY.md) sets what upstream emitted against
> what this repo has, which is the checklist for the comparison.

A minimal, runnable reference architecture for the way enterprises actually build
LLM agents today, observed by Dynatrace OneAgent alone. Upstream asks what
purpose-built LLM telemetry can answer about an agent stack; this repo asks how
much of that the agent you inject without touching the code recovers on its own.

Built to run on [Kubernetes on Docker Desktop](https://www.docker.com/blog/how-to-set-up-a-kubernetes-cluster-on-docker-desktop/)
and [WSL](https://learn.microsoft.com/en-us/windows/wsl/install), but nothing in
it is local-only.

> **New to agentic architecture?** Start with
> [`docs/WALKTHROUGH.md`](docs/WALKTHROUGH.md) — a guided read of this codebase
> for someone who knows observability but not agents. Then
> [`docs/UPDATE-GAPS-1-2.md`](docs/UPDATE-GAPS-1-2.md) for the answer judge, the
> feedback loop and the security act.

## Why

The interesting half of agent observability is not "instrument the LLM call".
Upstream gets that from OpenLLMetry in one line: model, tokens, cost, prompts,
vector queries. Here the same question is put to OneAgent, which captures
OpenAI-SDK calls from version 1.339 on; the injected code modules are 1.347.49.
What it actually records for this stack is not yet measured.

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

Upstream builds the standard architecture, then closes those four gaps with code
of its own and shows what that costs. This repo keeps the architecture and the
app logic, removes that code, and measures which of the four gaps an injected
agent closes without it.

## Architecture

The shape is the one the 2026 vendor guides converge on: an orchestrator that
triages and delegates, specialised workers, RAG for private knowledge, MCP for
tools, a model gateway in front of every LLM call, and a human approval gate in
front of anything consequential.

```
                            ┌─────────┐
                            │ loadgen │  a ticket every 20s
                            └────┬────┘
                                 │ POST /chat       human ──┐
                                 ▼                          │ POST /feedback
                      ┌──────────────────────┐◀─────────────┘
             ┌────────│     orchestrator     │────────┐
             │        └──────────┬───────────┘        │
    delegate │                   │ chat               │ delegate
             ▼                   │                    ▼
 ┌──────────────────────┐        │     ┌──────────────────────┐
 │   knowledge-worker   │        │     │     action-worker    │
 │  retrieve → screen   │        │     │   screen catalogue   │
 │  → answer → judge    │        │     │   → agent loop       │
 └───┬──────────────┬───┘        │     └──┬─────────┬──────┬──┘
     │ retrieve     │ chat ×2    │   chat │   MCP   │      │ approve?
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
                                               └──────────────────┘

  OneAgent in every pod above, injected by the Dynatrace Operator
  (namespace dynatrace) ──▶ Dynatrace · traces · logs
```

Ten pods, every edge. One of them is shared infrastructure that everything leans
on, and that is the point rather than an artefact of the drawing: **every chat
and embedding call goes through `litellm`**, so no service holds a provider name
or ever reaches Ollama directly.

The observability sits outside the drawing. The Dynatrace Operator's webhook
injects OneAgent into every pod in `o11yag-oa` when the pod is created —
including loadgen, LiteLLM, Ollama, Qdrant and Redis, which upstream does not
instrument at all. Keep that in mind when comparing coverage: a span OneAgent
shows inside LiteLLM has no upstream counterpart.

The stages written inside the two worker boxes are **not pods**, and that is
worth reading twice, because it is where half of this project's signal now comes
from. `screen` and `judge` are code sitting between an input and a prompt, or
between a prompt and the reply — nothing to deploy, nothing on the network to
point at, and no box of their own to draw. The judge is a second call through the
same gateway under its own alias (`support-judge`), which is why a question
ticket now costs two model calls rather than one. `POST /feedback` is likewise
just another endpoint on the orchestrator; it earns an arrow because the *human*
on the end of it is the only input in this diagram the stack cannot generate
about itself.

MCP wraps the **systems of record only**. The knowledge worker talks to Qdrant
directly, because in practice you don't MCP-wrap your own vector store — you
MCP-wrap the CRM, the ticketing system, the vendor API you didn't write.

## Components

| Component | Port | Stack | Role |
|-----------|------|-------|------|
| **o11yag-orchestrator** | 8000 | Flask | Supervisor. Classifies intent, delegates to one worker, owns the per-ticket roll-up, the conversation audit record and the `/feedback` endpoint. |
| **o11yag-knowledge-worker** | 8001 | Flask + Qdrant | RAG over the support knowledge base. Refuses to answer off a weak retrieval, screens what it retrieved for injected instructions, and grades the answer it gave. |
| **o11yag-action-worker** | 8002 | Flask + MCP client | The agent loop. Picks tools, calls them over MCP, routes consequential ones through the gate. |
| **o11yag-mcp-crm** | 8003 | MCP Python SDK | MCP server over Streamable HTTP exposing the fake system of record. |
| **o11yag-approvals** | 8004 | Flask + Redis | Human-in-the-loop gate, with a reviewer page at `/`. |
| **LiteLLM** | 4000 | LiteLLM proxy | Model gateway. Every LLM and embedding call goes through it. |
| **Qdrant** | 6333 | Qdrant | Vector store, seeded by the knowledge worker on first boot. |
| **Ollama** | 11434 | Ollama | `qwen:0.5b` for chat, `nomic-embed-text` for embeddings. |
| **Redis** | 6379 | Redis | Approval state. |
| **loadgen** | — | Python | Support tickets on a timer. Emits no telemetry of its own (OneAgent is injected into it anyway). |
| **Dynatrace Operator** | — | namespace `dynatrace` | Injects OneAgent into the pods in `o11yag-oa`, runs the ActiveGate and log monitoring. Configured by [`dynatrace/k8s/dynakube.yaml`](dynatrace/k8s/dynakube.yaml). |

> **LiteLLM is not LightLLM.** LiteLLM is a pure-Python proxy that forwards to a
> backend — no GPU, no model weights. LightLLM is a GPU inference engine and is
> not used here.

## Verified against

The SDK surface this code targets was checked against real installs, not from
memory: `mcp` **2.2.0** (`MCPServer`, `streamable_http_client`,
`Tool.input_schema`, `CallToolResult.structured_content`) and `openai` **3.14.1**.

**Run, and working, in this repo:**

- All five app services boot from a clean virtualenv built only from their own
  `requirements.txt` — no OTel packages — and answer `/health`.
- End to end, orchestrator → action worker → a live MCP server, with the model
  and the approval service down: the order lookup goes over MCP, the refund is
  refused because the gate could not be reached (fail closed), and every step
  leaves a JSON audit line on stdout.
- The DynaKube passes the operator's validation (v1.10.2) and reaches
  `Running`; every pod in `o11yag-oa` carries
  `oneagent.dynatrace.com/injected: "true"`.

**Run upstream, and unchanged here:**

- The agent loop against a live MCP server: `lookup_order` → `issue_refund`
  → done, refunding the order's own total rather than an amount asserted in the
  customer's message.
- The gap 1 and gap 2 logic, as pure functions, against the real corpus and a
  real tool catalogue: zero false positives from the injection patterns across
  all eight benign documents, the poison document caught on `override`, the
  catalogue digest stable under reordering and moving on a changed description,
  and the judge's heuristic separating an invented figure from grounded
  paraphrase and from an honest refusal. Logic only — no model, no cluster.

**Not yet measured:** what OneAgent actually records. Whether the LLM calls
become spans, whether the Qdrant client, `httpx2`, gunicorn `gthread` and
uvicorn are covered, whether the MCP hop stays one trace, and whether the audit
JSON lines arrive with their fields parsed. That is the comparison this repo
exists for, and none of it is claimed until it is measured.

**Found only by running it in a cluster:** the MCP server's Host-header check.
Every tool call returned 421 because the SDK auto-allows localhost and nothing
else, which the loopback test above could not have caught — it was loopback.

The boot test is the one that matters most, and it is why `smoke.sh` exists: the
services passed every static check — manifests parsed, Python compiled, imports
read correctly — while the orchestrator still died on its first line of real work
because a transitive dependency had quietly gone away (upstream, a package its
tracing SDK imported without declaring). For this stack the dependency graph is
the fragile part, not the code, because the ecosystem is mid-migration from
`httpx` to `httpx2` and the SDKs disagree about where they are in it.

## Conventions

- **Config** is environment-driven through a per-service `config.py`.
- **Containers** are multi-stage and non-root, with a read-only root filesystem
  and a `/tmp` `emptyDir` for scratch.
- **Deployment** is one self-contained Kubernetes manifest per component.
- **Image naming** is `o11yag-<service>`, tagged with a build timestamp.
- **Observability hooks** are `o11y.py`, copied verbatim into every app service
  (each Docker build context is its own directory). Here it is a stub: logging
  setup, no-op metric functions, and `audit()` writing JSON lines.
- **Audit record fields** are `audit.*`, with the event type in
  `audit.event.type`, the same keys upstream sends as log attributes.

## Getting started

> Prerequisites: a Kubernetes cluster, `kubectl`, and Docker.

1. **Start Kubernetes on Docker Desktop** with headroom for Ollama:
   memory ≥ 8192, cpus ≥ 4.

2. **Install the Dynatrace Operator** on the cluster. It creates the `dynatrace`
   namespace and the DynaKube CRD; `build_deploy.sh` stops with a message if
   they are missing (or pass `--no-oneagent` to deploy uninstrumented).

3. **Set the Dynatrace variables**:
   ```
   export DT_TENANT='abc12345'      # first part of your Dynatrace URL
   export DT_API_TOKEN='...'
   export DT_DATA_INGEST_TOKEN='...'
   ```
   `build_deploy.sh` turns these into two Secrets in the `dynatrace` namespace,
   `o11yag-oa` (the tokens) and `o11yag-oa-tenant`, and fills the tenant into the
   DynaKube's `apiUrl` at apply time, so neither is ever written to a tracked
   file. On a redeploy it keeps existing Secrets unless the variables are set.
   The comments in [`dynatrace/k8s/dynakube.yaml`](dynatrace/k8s/dynakube.yaml)
   say what was trimmed from the UI-generated DynaKube and why.

4. **Build and deploy**:
   ```
   bash build_deploy.sh
   ```
   It has no shebang, so run it with `bash`. OneAgent is set up before any
   workload, because the webhook injects at pod creation. First start is slow:
   Ollama downloads two models (~700MB) before it is ready, and the knowledge
   worker seeds Qdrant on its first boot.

   **Pods that existed before the DynaKube are not instrumented** and nothing
   says so. Recreate them:
   ```
   kubectl rollout restart deployment -n o11yag-oa
   ```
   Check with
   `kubectl get pods -n o11yag-oa -o custom-columns='POD:.metadata.name,INJECTED:.metadata.annotations.oneagent\.dynatrace\.com/injected'`.

5. **Watch it work**:
   ```
   kubectl get pods -n o11yag-oa -w
   kubectl logs -l app.kubernetes.io/name=o11yag-loadgen -n o11yag-oa -f
   ```

Redeploy without rebuilding with `bash build_deploy.sh --no-build`. `stop.sh`
removes the workloads but keeps the namespace and the volumes; the DynaKube and
its Secrets in `dynatrace` are untouched.

**Before deploying, smoke-test the images**:
```
bash smoke.sh
```
It boots each built image without OneAgent and with every backing service pointed
at a dead port, and checks it answers `/health`. That catches the class of bug where
everything static passes — manifests parse, Python compiles — and the container
still dies on boot because a dependency is missing from `requirements.txt`. It is
much faster than finding out from a CrashLoopBackOff.

## The four gaps: what the app does, and what is observable here

The app logic that closes each gap — the retrieval floor, the judge, the loop
bounds, the per-ticket roll-up, the approval gate — is unchanged from upstream.
What is gone is the telemetry upstream built on top of it: the `o11yag.*`
metrics, the custom span attributes, the agent/tool/step spans. In this repo the
logic still runs and its decisions still reach the **audit records**; whether
OneAgent recovers any of the rest is not yet measured. Upstream's figures quoted
below were measured on upstream, with its telemetry.

### 1. Silent semantic failure

Three guards, at three different distances from the truth.

**The retrieval floor** is the cheapest and runs first: the knowledge worker
checks the best similarity score against `MIN_SCORE` and says "I don't have a
policy that covers that" rather than letting the model invent one, emitting an
`o11yag.retrieval.ungrounded` audit record when it does. It catches wrong answers
*caused by* bad retrieval — and nothing else. The larger half of the problem is
the answer that is grounded in exactly the right document and wrong anyway, and
for that ticket every signal upstream had was green: a high retrieval score, no
span in error, a confident paragraph quoting a policy that does not exist.

**The judge** (`knowledge-worker/judge.py`) grades the answer it actually gave
against the extracts it was given. Upstream records the verdict on its own
`judge_answer` span and in `o11yag.answer.quality`; here it reaches the
`o11yag.answer.generated` audit record (`quality_verdict`, `quality_decided_by`,
`quality_reason`) and the ticket's `quality`. Two graders, and the deterministic
one is not a fallback:

- *heuristic* — free, runs on every answer. An amount, deadline or duration in
  the answer that appears nowhere in the extracts (`unsupported_number:60`), or
  an answer whose content words are largely absent from them
  (`low_overlap:0.12`). Crude, and aimed squarely at what actually goes wrong in
  a policy KB: the model keeps the shape of the policy and invents the figure,
  which is the version a customer acts on.
- *model* — a second LLM call, asked for a yes/no. The one every vendor diagram
  draws, and the one to be most careful about here, because the judge is the
  same `qwen:0.5b` that could not reliably emit a tool call. `JUDGE_MODEL` is a
  separate gateway alias so a capable model can be put behind it from
  `litellm`'s ConfigMap alone.

`decided_by` records which grader produced the verdict, so "the judge model never
once disagreed with the cheap check" stays a fact you can query rather than an
assumption you inherit. A judge that returns nothing usable is recorded as
`decided_by=heuristic, reason=model_unparseable` — not as a pass.

**The thumbs-down** (`POST /feedback` on the orchestrator) is the only input in
the whole stack that does not come from the stack. Everything else — the floor,
the judge, the loop signals — is the system's opinion of itself, and all of it
can be confidently and consistently wrong at once with nothing internal
disagreeing. Upstream, `/chat` returns its `trace_id` so the feedback, arriving
minutes or days later on a trace of its own, can name the trace it rates. Here
`trace_id` is always empty, so the ticket id is the only join and a thumbs-down
is countable but not openable.

**What it cost, measured upstream.** One extra model call per answered question,
on the ticket's own latency, counted into the ticket's tokens and cost like any
other — a judge you do not pay for is a judge that did not run. On this stack
that call buys nothing at all, and the numbers are the point:

> Over the first 10 graded answers against a live `qwen:0.5b`, the judge model
> produced **0 usable verdicts** while spending **2,353 tokens** and adding
> **1,462 ms** (p50) to every answered ticket. Four replies were JSON-shaped with
> a non-boolean `supported`, two contained no JSON at all. Every verdict on the
> dashboard was the deterministic check's.

Small sample, and it will not improve: it is the same finding as "2 usable
`lookup_order` arguments out of 51", for grading instead of tool calling. The
reason it is visible at all is `decided_by` — without that dimension the
verdicts look identical to a judge that agrees with everything, and a judge that
agrees with everything is indistinguishable from one that was never asked.

`JUDGE_MODE: heuristic` drops the model call and keeps the signal at zero
marginal cost and reduced coverage; it is the right setting for this stack, and
the default stays `model` for the same reason `PLANNER_MODE` does — so a run
shows the truth about the model behind the gateway rather than hiding it.

**What is still open, and it is the important part.** The judge is a detector,
and `JUDGE_ACTION` defaults to `observe` — the unsupported answer is recorded and
still sent. `withhold` replaces it with the refusal, and is the setting that
actually protects the customer; it is not the default because a weak judge
withholding good answers is a worse product than a wrong answer you can see in a
dashboard, and nobody should flip it before measuring their own false-positive
rate. There is also no offline evaluation set here, no regression suite, and
nothing that feeds a thumbs-down back into retrieval or the prompt. The signal
exists and is honest about its own quality. The loop that closes on it does not.

### 2. No baseline

The action worker counts loop steps, repeated identical tool calls and whether
the loop ended `done` or at `max_steps`, and returns them in its `/act`
response; the orchestrator rolls up `llm_calls` per ticket into the
`o11yag.ticket.handled` audit record. Upstream turns these into the
`agent.loop.*` span attributes and the `o11yag.task.llm_calls` series by intent,
which *are* the baseline when there is no fixed call graph. Here none of that is
emitted, and the per-step decision (`decided_by`, and why the model's answer was
rejected) is recorded nowhere. Whether OneAgent's own spans give a usable shape
per ticket is not yet measured.

`terminated = max_steps` still deserves attention: the agent ran out of budget
and returned a partial answer, and nothing errors.

### 3. Per-ticket economics

Tokens, LLM calls, latency and a priced cost are computed once per ticket and
written to the `o11yag.ticket.handled` audit record, with `tenant` and
`customer_id`. Upstream also emits them as `o11yag.task.*` metrics and attributes
every span to tenant / customer / ticket through Traceloop association
properties; neither exists here. Attribution in this repo is the audit record and
nothing else.

### 4. The tool blind spot

Every MCP call leaves a `o11yag.tool.called` or `o11yag.tool.blocked` audit
record with the tool, its arguments, the result and the approval status, and
mcp-crm writes `o11yag.crm.refund_issued`. Upstream adds a span per call, a
`o11yag.tool.calls` metric, and hand-written trace-context propagation over the
MCP hop; all of that is gone. The MCP SDK still calls the OpenTelemetry API
internally. With no SDK configured that should record nothing, unless OneAgent
picks those calls up, which is not yet measured. Neither is whether OneAgent
joins the action worker and the MCP server into one trace.

### The approval gate

Upstream gives the wait its own span and its own metric, so a reviewer who goes
to lunch does not swamp every latency percentile. Here the wait shows up only as
`waited_s` in the `o11yag.approval.decided` audit record, and inside whatever
span OneAgent draws around the action worker's request.

## The security act

Three attacks, all specific to agents, all of which leave a normal trace looking
perfectly healthy. Everything here is **off by default** — the reference stack
ships a clean knowledge base and an honest tool catalogue — because an attack
that ships enabled inside a reference architecture is indistinguishable from a
backdoor. Each one is a ConfigMap flag away.

> **Running one?** [`docs/SECURITY-DEMOS.md`](docs/SECURITY-DEMOS.md) is the
> runbook: which flag, what has to restart, what you should see, the ordering
> that matters for the rug pull, and how to put it all back.
>
> Want the **Error rate** tile off zero? [`docs/ERROR-DEMOS.md`](docs/ERROR-DEMOS.md)
> covers which failures count as `outcome=error` and how to cause them.

### Indirect prompt injection through the RAG corpus

The direct kind — a customer typing "ignore your instructions" — is the one
everybody pictures and the least interesting, because that text arrives labelled
as untrusted. The one that works is indirect: the instruction is written into a
*document*, retrieved on its merits by a similarity search doing exactly its job,
and reaches the model inside the context block — the part of the prompt the model
was told to trust. Nobody typed it during the ticket that fires it. It can be
planted months earlier by anyone who can write to the corpus: a scraped vendor
page, a wiki, a support macro, an uploaded PDF.

The reason it belongs in an observability repo is that the stack cannot see it
happen. Retrieval is *healthy* — the poisoned document is a genuinely good match,
so its score is high. No span errors. And if the model complies, the tool call
it produces is well-formed and in-contract, so `planner.validate_args` passes it
and the planner reports that the model decided. The trace reads as a normal
ticket in which the agent decided, by itself, to issue a refund nobody asked for.

`knowledge-worker/security.py` screens between retrieval and the prompt, which is
the only point where the text is still identifiable as *retrieved* rather than
*said*. A flagged document is dropped before the context is built
(`INJECTION_ACTION: quarantine`) — a detector that logs the finding and prompts
with the document anyway has recorded an attack it also carried out.

`KB_POISON_DOC: "true"` seeds the demo document. Qdrant keeps its volume across a
redeploy, so the knowledge worker compares the stored point count against the
configured corpus and reseeds when they differ; a flag that silently does nothing
would be a poor joke in this particular repo.

**What this path cannot do here, stated before anyone demonstrates it and finds
out.** The planted document tells the agent to issue a refund. It will not get
one. Retrieval belongs to the knowledge worker, and the knowledge worker has no
tools — it retrieves and writes prose; the agent that holds the tools never
retrieves. An injection in this corpus can therefore corrupt an *answer* and
nothing beyond it. The general attack does end in a tool call, and in an
architecture where a single agent both retrieves and acts it would end in one
here — but that is not the architecture in this repo, and a demo implying
otherwise would be doing exactly what this project argues against. The injection
that changes what the agent *does* is the tool-catalogue one below.

### Tool poisoning

An MCP tool description is attacker-controlled text that goes into the planner's
system prompt as capability documentation, and the agent is built to act on it.
Same signature as above: the resulting call is well-formed, in-schema and
attributed to the model, because it was the *intent* that was supplied by an
attacker and no schema check can see intent.

`POISON_TOOL_DESCRIPTION: "true"` on the **mcp-crm** ConfigMap makes the server
advertise `issue_refund` with an injected description. The server does the lying,
not a stub in the client: a demo where the detector is fed a canned finding
proves the detector prints, not that it detects. The action worker screens the
catalogue at discovery and blanks a flagged description before the planner sees
it (`TOOL_POISON_ACTION: redact`) — the description is removed, not the tool,
because dropping it would let anyone who can edit a description disable any tool
they like.

### The rug pull

The same server, serving a benign description until it is trusted and a different
one afterwards. Tool names identical, schemas identical, tool list identical;
nothing in a normal trace moves at all. The only thing that catches it is a
fingerprint taken over descriptions and schemas and compared to one taken
earlier. Upstream puts it on the `action_worker` span as `mcp.tools.digest`;
here it is in the action worker's log at first sight and in the
`o11yag.security.tool_catalogue_changed` audit record when it moves.

The record's `baseline` field says which comparison you are getting, and the
difference matters: `pinned` means `MCP_TOOLS_DIGEST` is set in config and a server that was
*already* poisoned at boot is caught on the first call; `first_seen` means the
first catalogue this pod saw became its own baseline, which catches a change
mid-life and is blind to a server that was compromised before the pod started. A
restart forgets. The digest is logged on first sight, so pinning it is copy and
paste.

Flip `POISON_TOOL_DESCRIPTION` on a running stack and you have performed the rug
pull: the digest stops matching and `o11yag.security.tool_catalogue_changed`
fires with both digests in the record.

**Nothing here blocks.** A worker that refuses to run because a description
changed cannot tell a deploy from an attack, and hands anyone who can edit a
description an outage. Detection produces a signal a human acts on; the control
that holds regardless is the approval gate, which does not care who asked.

## Signals reference

What this repo emits by itself is the audit trail. Everything else upstream
emitted — the twelve `o11yag.*` metrics and the custom span attributes — is
listed in [`docs/TELEMETRY.md`](docs/TELEMETRY.md) against what exists here.
What OneAgent adds on top is not yet measured.

### Audit records (stdout JSON lines, `audit.*`)

One JSON object per line on the container's stdout, written by `o11y.audit()`,
for OneAgent log monitoring to collect. Log monitoring is scoped to the
`o11yag-oa` namespace in the DynaKube.

| Event type | Written by | Carries |
|------------|-----------|---------|
| `o11yag.ticket.handled` | orchestrator | The conversation, intent and who decided it, outcome, cost, tokens, LLM calls |
| `o11yag.answer.generated` | knowledge worker | Question, answer, the documents it was grounded on, the judge's verdict |
| `o11yag.retrieval.ungrounded` | knowledge worker | A question the KB could not answer |
| `o11yag.tool.called` | action worker | Tool, arguments, result, approval status |
| `o11yag.tool.blocked` | action worker | A tool call the gate refused |
| `o11yag.approval.requested` / `.decided` | approvals | Who decided, how long they took |
| `o11yag.answer.withheld` | knowledge worker | An answer the judge rejected, kept in the record after being replaced |
| `o11yag.feedback.received` | orchestrator | A human rating |
| `o11yag.security.injection_detected` | knowledge worker | The document, the phrase that matched, and what was done |
| `o11yag.security.tool_poisoned` | action worker | The tool, the phrase, and whether the description was redacted |
| `o11yag.security.tool_catalogue_changed` | action worker | Both digests and the baseline they were compared against |
| `o11yag.crm.refund_issued` | mcp-crm | The effect on the system of record |

Every record has `audit.event.type` and `audit.schema.version`. Unlike upstream,
there is no `audit.trace_id` or `audit.span_id`: without a tracing API in the
process there is no id to read, so a record cannot pivot to its trace by id.
Whether OneAgent links the log line to the surrounding trace on its own, and
whether the JSON fields arrive parsed as attributes, is not yet measured. If
they do, this is the query:

```
fetch logs
| filter audit.event.type == "o11yag.tool.called"
| fields timestamp, audit.tool, audit.args, audit.approved,
         audit.ticket_id, audit.customer_id
| sort timestamp desc
```

## Dashboard

[`dashboards/o11yag.json`](dashboards/o11yag.json) is still upstream's. Its
tiles query the `o11yag.*` metrics and custom span attributes, which this repo
does not emit, so most of them will be empty until it is reworked around what
OneAgent and the audit records actually provide. Deploy with
`cd dashboards && ./deploy.sh`, which posts it to the Dynatrace Document API
using `$DT_ENVIRONMENT` / `$DT_PLATFORM_TOKEN`. See
[`dashboards/README.md`](dashboards/README.md).

## What this deliberately does not do

- **No evaluation loop, still.** There is now a quality *signal* — the judge and
  the feedback endpoint — and that is not the same thing. No offline evaluation
  set, no regression suite, no golden answers, and nothing that routes a
  thumbs-down back into retrieval, the prompt or a retraining queue. The
  reference architectures draw the closed loop; this draws the half of it that
  can be built honestly in a reference stack, and says which half that is.
- **The security act detects; it does not prevent.** See its own section above.
  The detectors are pattern matches over English imperatives and will miss an
  injection in another language, split across two documents, or simply phrased
  in a way the list does not cover. The durable controls are upstream (who can
  write to the corpus, what is re-verified on ingest) and downstream (the
  approval gate, which does not care who asked for the refund). Treat a hit as a
  finding about the pipeline, not as a regex to tune.
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
  of 45 runs**. In that window `issue_refund` was never once attempted, so the
  approval gate was never exercised. Six runs reached a second step and spent it
  re-calling the same tool with identical arguments. (Measured upstream.)

  **Measured later upstream, the gate does run in `model` mode — but never because
  of the model.** From 2026-09-20 to 2026-09-30 Dynatrace holds **862** `issue_refund`
  calls. Every one sits under a step with `step.decided_by = fallback`, and **0**
  were proposed by the model: at the second step the model named a tool the
  server doesn't advertise (`step.fallback_reason = unknown_tool`, 860) or
  returned something unparseable (2), and the rules took over and went on to the
  refund. 338 were auto-approved (ORD-1001, €89.90); 524 timed out waiting for a
  person (ORD-1002 and ORD-1004, both over `AUTO_APPROVE_MAX_EUR`).

  Two things in the code respond to that, and it matters which does what:

  - **Argument validation** (`planner.validate_args`) holds the model to the
    JSON schema the MCP server advertises. Wrong or missing parameters are
    rejected and the deterministic rules take over (upstream records what was
    wrong as `step.fallback_reason`; here nothing does). This fixes bad
    *arguments*.
  - **`PLANNER_MODE=rules`** skips the model for tool selection entirely. This is
    what makes the refund → approval → gate path reliably demonstrable.

  Validation does not make the *model* refund, and it does not make refunds
  reliable. Whenever the model is overruled at the right step, the rules reach
  `issue_refund` — that is the 862 above. But a model that answers
  `{"done": true}` is well-formed and in-contract, so it is accepted and the loop
  ends early with no refund. Well-formed is not the same as sensible, and only
  the second switch makes the path happen every time. Never present a `rules` run as model
  reasoning. Upstream records who decided each step as `step.decided_by` on the
  trace; here it is recorded nowhere, which is one of the gaps to measure.
- **LiteLLM is unauthenticated** inside the namespace. The real shape is a virtual
  key per agent with its own budget.
- **Audit records land in the default log bucket.** Until an OpenPipeline rule
  routes `audit.event.type` to a bucket with its own retention, the
  record-keeping claim is not fully true. That is tenant configuration, not code.
- **OneAgent is injected into every pod in the namespace**, the infrastructure
  included, while upstream instruments only the five app services. A pod opts out
  with the annotation `oneagent.dynatrace.com/inject: "false"`; whether to match
  upstream's coverage that way is still open.
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
kubectl port-forward service/o11yag-orchestrator 8000:8000 -n o11yag-oa

curl -X POST http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"ticket_id":"TK-1","customer_id":"C-7","tenant":"acme",
       "text":"I want a refund for order ORD-1001, the headphones stopped working."}'
```

**Rate an answer** (the human half of gap 1). The ticket id is the join; the
`trace_id` `/chat` returns is always empty here:
```
curl -X POST http://localhost:8000/feedback -H 'Content-Type: application/json' \
  -d '{"ticket_id":"TK-1","rating":"down","intent":"question",
       "comment":"quoted a 60-day return window that does not exist"}'
```

**The approvals page**:
```
kubectl port-forward service/o11yag-approvals 8004:8004 -n o11yag-oa
# then open http://localhost:8004/
```

**Ask the knowledge worker directly**:
```
kubectl port-forward service/o11yag-knowledge-worker 8001:8001 -n o11yag-oa
curl -X POST http://localhost:8001/answer -H 'Content-Type: application/json' \
  -d '{"ticket_id":"TK-2","text":"How long do refunds take?"}'
```

**Check the model gateway**:
```
kubectl port-forward service/litellm 4000:4000 -n o11yag-oa
curl http://localhost:4000/v1/models
```

**If every MCP tool call fails with 421**, the action worker is reaching the
server by a name the allowlist doesn't cover. The server says so:
```
kubectl logs -l app.kubernetes.io/name=o11yag-mcp-crm -n o11yag-oa | grep -i "host"
#   WARNING mcp.server.transport_security Invalid Host header: <the name>
#   INFO    ... "POST /mcp HTTP/1.1" 421 Misdirected Request
```
Add that name to `MCP_ALLOWED_HOSTS` in `mcp-crm/k8s/o11yag-mcp-crm.yaml` and
roll the pod. The server logs its allowlist on boot, so you can check what it
believes it accepts.

**Read the audit records** a service wrote, one JSON object per line:
```
kubectl logs deploy/o11yag-orchestrator -n o11yag-oa | grep '^{"audit'
```

**Check OneAgent**: the DynaKube's state, and whether a pod was injected:
```
kubectl get dynakube -n dynatrace
kubectl describe dynakube o11yag-oa -n dynatrace
kubectl get pods -n o11yag-oa -o custom-columns='POD:.metadata.name,INJECTED:.metadata.annotations.oneagent\.dynatrace\.com/injected'
```

### Turning the interesting cases on

| To see | Do |
|--------|-----|
| A real human approval | `AUTO_APPROVE=false` on the approvals ConfigMap, then use the page |
| A gate that stops regardless | Send a refund for `ORD-1004` (430 EUR, over the ceiling) |
| A refund actually reaching the gate | `PLANNER_MODE: "rules"` on the action worker ConfigMap — the local model rarely gets there on its own |
| A loop that runs out of budget | `MAX_STEPS: "1"` on the action worker ConfigMap |
| An ungrounded answer | Ask something the KB has no policy for |
| A blocked tool call | Deny an approval on the page |
| An answer graded unsupported | Ask a question the KB half-covers — or force the plumbing with `JUDGE_MIN_OVERLAP: "0.9"`, which flags ordinary paraphrase |
| That answer never reaching the customer | `JUDGE_ACTION: "withhold"` on the knowledge worker |
| The judge off the critical path | `JUDGE_MODE: "heuristic"` — deterministic checks only, no second model call |
| A prompt injection landing | `KB_POISON_DOC: "true"` **and** `INJECTION_ACTION: "observe"`, then ask about a faulty item |
| The same injection stopped | `KB_POISON_DOC: "true"` alone — quarantine is the default |
| A poisoned tool description | `POISON_TOOL_DESCRIPTION: "true"` on the **mcp-crm** ConfigMap |
| A rug pull | Flip that same flag while the action worker is running |
| A thumbs-down | `POST /feedback` — see below |
