# o11yag walkthrough

A guided read of this codebase for someone who knows observability but is new to
agentic architecture. Start here, then read the code in the order below.

> Once this makes sense, [`UPDATE-GAPS-1-2.md`](UPDATE-GAPS-1-2.md) walks the
> parts added later: the answer judge, the feedback endpoint, and the detectors
> for prompt injection, tool poisoning and rug-pulled tool definitions.
> [`SECURITY-DEMOS.md`](SECURITY-DEMOS.md) is the runbook for switching those
> attacks on and off.
> [`ERROR-DEMOS.md`](ERROR-DEMOS.md) does the same for failures that push the
> Error rate tile off zero.

> Function names are used as anchors rather than line numbers, because line
> numbers rot. `grep -n "def <name>" <file>` will find any of them.

> **This is o11yag-oa.** The code is upstream's (o11yag-otel) with every piece of
> OpenTelemetry instrumentation removed; Dynatrace OneAgent is injected instead.
> So there are no decorators, no custom span attributes and no `o11yag.*` metrics
> here. What the stack still says about itself goes into audit records: one JSON
> object per line on stdout, which OneAgent log monitoring picks up. What
> OneAgent captures on its own is not yet measured. Where this walkthrough names
> a signal, it says which of those two it is.

---

## 1. Basic idea

APM sees a system quite deterministically:

```
request → service A → service B → database → response
```

**The call graph is fixed.** The same input takes the same path. That single
assumption is what lets you baseline it, alert on deviation, and say that a given span is slow.

An agent breaks exactly that assumption:

```
request → a model decides what to do
        → do it (call a tool, search a knowledge base)
        → the model looks at what came back
        → it decides again
        → ... until it decides it is finished
        → answer
```

**The path is chosen at runtime, by a model, and it differs every time.** The
same support ticket can legitimately take 3 steps today and 11 tomorrow, with
nothing broken.

That is the whole conceptual leap. Everything strange about agent observability —
no baseline to alert against, failures that are silent and semantic, costs you
cannot predict per request — falls out of that one difference.

**An agent is an LLM in a loop with tools.** 

### This is not theoretical here

Over three hours of live traffic, this stack reported `outcome=ok` on all 45
agent runs while the agent was calling one tool with wrong arguments and then
giving up. No span had `error=true`. Every RED metric was green. The behaviour
was only visible, upstream, once custom span attributes were persisted and
someone went looking. That is the failure mode this project exists to make
legible — and whether OneAgent, with no custom attributes at all, can show it is
the question this repo asks.

---

## 2. Read the code in this order

The architecture diagram lives in the main [README](../README.md); glance at it
first, then walk the code.

### Stop 1 — `loadgen/tickets.py`

What goes in. Plain customer sentences: *"How long do refunds take to show up on
my card?"*. No API schema, no intent field, no structure.

That is the problem statement. Everything downstream exists because the input is
natural language and the output has to be an action.

### Stop 2 — `orchestrator/app.py`

The **supervisor**: it owns the ticket and decides who should work on it.

- `CLASSIFY_PROMPT` — how you ask a model to pick one label from a list.
- `classify()` — the LLM call. Upstream decorates it `@task` so it becomes a
  span of its own; here it is a plain function, and whether OneAgent shows the
  LLM call underneath is not yet measured.
- `handle()` — the whole ticket: classify, route to one worker, accumulate tokens
  and call counts, and write the `o11yag.ticket.handled` audit record. The
  per-ticket metric roll-up (`o11y.task_finished()`) is a no-op here; the same
  numbers are in the audit record, along with `intent_source`, which says
  whether the model or the fallback picked the route.
- `_heuristic_intent()` — the deterministic fallback, and worth your attention.

**Why the fallback ordering matters.** It originally keyword-matched, so *"How
long do refunds take?"* matched `refund` and went to the CRM — which knows
nothing about refund timing — instead of the knowledge base, which has exactly
that policy. Result: the knowledge worker received **zero** traffic, half the
architecture was dark, and every ticket still reported `ok`. It now tests
most-specific-first: escalation words, then an explicit `ORD-####`, then
interrogative phrasing, then bare action words.

A routing bug that produces no errors is a very good introduction to this domain.

### Stop 3 — `knowledge-worker/retrieval.py`, then `app.py`

**RAG** (retrieval-augmented generation), the most common enterprise pattern
there is. The model does not know your return policy, so:

1. turn the question into a vector (`llm.embed`)
2. find the nearest documents in Qdrant (`search`)
3. paste them into the prompt and instruct the model to answer *only* from those

`retrieval.py` is the whole idea in about 50 lines. In `app.py`, note `answer()`
refusing to answer at all when the best match is below `MIN_SCORE`. Upstream puts
the retrieval scores on the `retrieve` span (`rag.top_score`); here the top score
reaches telemetry only through the `o11yag.answer.generated` and
`o11yag.retrieval.ungrounded` audit records.

**That refusal is the cheapest real guardrail in this repo.** The alternative is
a model inventing a returns policy, fluently, with a 200 OK.

### Stop 4 — `action-worker/app.py`, the loop inside `act()`

```python
for step in range(Config.MAX_STEPS):
    decision, step_tokens, decided_by, why = planner.next_step(...)
    if decision.get("done"):
        break
    result = invoke(name, args, ticket_id, customer_id)
    history.append({"tool": name, "args": args, "result": result})
else:
    terminated = "max_steps"
```

**This loop is the agent.** Everything else in this repository is plumbing around
it. Three things to notice:

- **`history` is the loop closing.** The result of step *n* is fed back into the
  decision for step *n+1*. That is the entire difference between an agent and a
  script: a script has its sequence fixed in advance.
- **`MAX_STEPS` exists at all.** An agent that cannot work out it is finished
  runs forever. A budget is the only control that always works — everything else
  depends on noticing first. `terminated = "max_steps"` is a *silent* failure:
  the customer gets a partial answer and nothing errors.
- **Deciding and doing are separate.** `next_step()` only chooses; `invoke()`
  acts. The gap between them is where the approval gate fits.

### Stop 5 — `action-worker/planner.py`

How the agent decides, and — more useful — **what to do when the model decides
badly**.

- `next_step()` — asks the model first, falls back to deterministic rules.
- `validate_args()` — checks the model's proposed arguments against the JSON
  schema the MCP server advertises.

**Why `validate_args` exists.** Measured over three hours of real traffic,
qwen:0.5b produced usable arguments for `lookup_order` **2 times out of 51**. It
mostly proposed `{"id": "C-7"}` — a customer id, under a parameter name the tool
does not have. The tool signature is `lookup_order(order_id: str)`.

The general lesson is worth more than the code:

> **Well-formed is not the same as sensible.**

The original fallback only triggered when the model's output was *unusable*. But
`{"tool": "lookup_order", "args": {"id": "C-7"}}` is valid JSON naming a real
tool — so it was accepted, sent, and failed at the server. The fix is to hold the
model to the contract the server already publishes. `next_step()` returns why it
rejected the model (`missing:order_id`, `unknown_tool`, `unparseable`). Upstream
records that as `step.fallback_reason` on the step span; here nothing records it.

- `PLANNER_MODE` — `model` (default) or `rules`. In `rules` the model is not
  consulted for tool selection at all. It exists because a small local model
  rarely reaches a consequential tool, so the approval path is otherwise not
  demonstrable. Upstream tags every step it decides `step.decided_by=fallback`,
  so a rules-driven run can never be passed off as model reasoning. Here that
  tag does not exist, and nothing in the telemetry tells a rules run from a
  model run — keep that in mind before reading any trace from `rules` mode.

**Honest limit:** validation fixes bad *arguments*. It does not fix a model that
answers `{"done": true}` after one step — that is well-formed and in-contract, so
it is still accepted. Only `PLANNER_MODE=rules` addresses that.

### Stop 6 — `mcp-crm/server.py`

**Tools.** A tool is a function plus a description the model can read. The model
names one and supplies arguments; your code executes it.

`@mcp.tool()` turns a plain Python function into one. `CONSEQUENTIAL` lists the
tools that move real money — stated once, next to the tools themselves, rather
than in a prompt a model could argue its way around.

**MCP** (Model Context Protocol) is just a standard for exposing those functions
over a network instead of hardcoding them into the agent. Here it runs over
Streamable HTTP. Upstream carries trace context across that hop by hand; this
repo removed that code, so whether the MCP call joins the ticket's trace is down
to OneAgent, and not yet measured.

### Stop 7 — `approvals/app.py`

Human-in-the-loop: the gate in front of anything consequential.

`_maybe_auto_decide()` is a simulated reviewer — it approves after
`AUTO_APPROVE_AFTER_S`, but **never** above `AUTO_APPROVE_MAX_EUR`, so one path
always genuinely waits for a person. With auto-approve on, the gate proves
nothing about governance; it is a timer wearing a reviewer's hat. That is stated
in the README rather than glossed.

The observability point: upstream gives the wait its own span (`approval_wait`)
and its own metric (`o11yag.approval.wait`), both on the worker's side of the
gate in `action-worker/approvals.py` `request_and_wait()`. Neither exists here:
the span was OTel and `o11y.approval_waited()` is a no-op. What is left is the
`o11yag.approval.decided` audit record this file writes, which carries
`decision`, `decided_by` and `waited_s` per approval.

Without a separate series the wait is still measured, just inside the wrong
numbers. The orchestrator is blocked for as long as the reviewer thinks, so every
second lands in the ticket's wall-clock latency (`latency_ms` on the
`o11yag.ticket.handled` record, and whatever request duration OneAgent reports),
and stretches every span above it. Take 95 tickets the machine finishes in ~3 s
and 5 refunds a person sits on for ~20 minutes (illustrative figures, not
measured):

| | Wait mixed in | Machine time only |
|---|---|---|
| mean | ~63 s | ~3 s |
| p95 / p99 | ~20 min | ~3–4 s |

A latency alert then fires whenever someone takes their time, a model that slows
from 3 s to 8 s disappears under the outliers, and "why are tickets slow?" has no
answer. Kept apart, they are two questions for two owners: the approval wait is
staffing, and task latency minus it is engineering. Here, keeping them apart
means joining the two audit records by `ticket_id`.

---

## 3. One request, end to end

For a refund ticket, the path through the code:

```
loadgen                POST /chat
orchestrator           handle()
  ├─ classify()                            LLM: which intent?
  └─ delegate()                            HTTP → action worker
action-worker          act()
  ├─ planner.next_step()                   decide: lookup_order(ORD-1001)
  ├─ invoke()                              → MCP → mcp-crm executes it
  ├─ planner.next_step()                   decide again, now knowing the total
  ├─ invoke()                              issue_refund — consequential!
  │    └─ approvals.request_and_wait()     BLOCKS until a decision
  │         └─ mcp_client.call_tool()      only if approved
  └─ summarise                             LLM: tell the customer what happened
orchestrator           audit record
```

Upstream decorates these functions (`@workflow`, `@task`, `@agent`, `@tool`) so
that this tree *is* the trace. Here nothing names them: the trace is whatever
OneAgent builds from the HTTP, MCP and LLM calls it sees, and how close that
comes to this tree is not yet measured.

---

## 4. Vocabulary

| Term | What it actually is |
|---|---|
| **Agent** | An LLM in a loop with tools |
| **Tool / function calling** | The model names a function and its arguments; your code runs it |
| **Tool schema** | The contract a tool publishes — validate the model against it |
| **RAG** | Retrieve relevant documents, paste into the prompt, answer only from those |
| **Grounding** | Answering from supplied facts rather than training data |
| **Context window** | Working memory limit, in tokens. Everything competes for it |
| **Token** | Roughly ¾ of a word. The billing unit and the limit unit |
| **Embedding** | Text → vector, so "similar meaning" becomes "close together" |
| **Orchestrator / supervisor** | An agent whose job is routing to other agents |
| **MCP** | A protocol for exposing tools to agents over a network |
| **Hallucination** | Confident, fluent, wrong — your 200 OK with a bad answer |

---

## 5. Try it

```
kubectl port-forward service/o11yag-orchestrator 8000:8000 -n o11yag-oa

curl -X POST http://localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"ticket_id":"LEARN-1","customer_id":"C-7","tenant":"acme",
       "text":"I want a refund for order ORD-1001, the headphones stopped working."}'
```

Then open the trace in Dynatrace and read it top to bottom, and read the audit
records for the same ticket next to it:

```
kubectl logs -n o11yag-oa -l app.kubernetes.io/part-of=o11yag --prefix --max-log-requests=20 | grep LEARN-1
```

Upstream, the signals below are span attributes. Here, each one is either in an
audit record or gone:

| Upstream attribute | What it tells you | Here |
|---|---|---|
| `agent.loop.steps` | How many iterations this ticket actually took | in the `/act` response (`steps`), not in any record |
| `agent.loop.terminated` | `done`, or `max_steps` — the silent partial answer | in the `/act` response (`terminated`); `outcome=incomplete` on `o11yag.ticket.handled` |
| `agent.loop.repeated` | Same tool, same arguments, twice. No error, just waste | in the `/act` response (`repeated_calls`), not in any record |
| `step.decided_by` | Model or deterministic rules | gone |
| `step.fallback_reason` | *Why* the model was overruled | gone |
| `gen_ai.tool.call.arguments` | What the model actually proposed | `args` on `o11yag.tool.called` / `o11yag.tool.blocked` |
| `rag.top_score` | Whether retrieval gave the answer anything to stand on | `top_score` on `o11yag.answer.generated` / `o11yag.retrieval.ungrounded` |
| `quality.verdict` / `.decided_by` | Whether the answer was supported by its extracts, and who decided | `quality_verdict` / `quality_decided_by` on `o11yag.answer.generated` |
| `security.injection.detected` | A retrieved document that carried instructions | an `o11yag.security.injection_detected` record per document |
| `mcp.tools.digest` / `.changed` | The tool catalogue's fingerprint, and whether it moved | an `o11yag.security.tool_catalogue_changed` record when it moved; the first digest is in the action-worker log |

The governance path — a refund reaching the gate, waiting, getting approved, and
changing the system of record — already runs in `model` mode, but only by
accident: when the model's step is rejected, the rules take over and go on to
`issue_refund`. The model itself has never proposed a refund (0 of 862 measured,
see the README's limits section); when it answers `done` early, no refund
happens. To see the path on every ticket, set `PLANNER_MODE: "rules"` on the
action-worker ConfigMap and restart it.
Set it back to `model` afterwards; `rules` is a demo aid, not an honest default.

---

## 6. What this codebase learned the hard way

Each of these cost real debugging time and is documented where it bites. Three
of the lessons upstream learned were about its OTel instrumentation (persisting
custom span attributes in Dynatrace, an undeclared `httpx` import in the
Traceloop SDK, carrying trace context across the MCP hop by hand); that code is
gone from this repo, so see upstream for those. What still applies here:

- **The MCP server validates the `Host` header** and auto-allows only localhost,
  so in Kubernetes everything returns 421 until the Service name is allow-listed.
- **`startupProbe.timeoutSeconds` defaults to one second.** Upstream's
  instrumentation imports made that too short and healthy pods were killed at
  60s; the longer timeout is kept here because OneAgent injection adds boot
  time of its own.
- **A Dynatrace Operator in `applicationMonitoring` mode runs log monitoring
  standalone**, and refuses the DynaKube unless it knows which log-module image
  to run. See the comments in `dynatrace/k8s/dynakube.yaml`.

The pattern across all of them: everything static passed — manifests parsed,
Python compiled, queries validated — and the system still misbehaved at runtime.
For this kind of stack the fragile parts are the dependency graph and the
backend's ingest rules, not the code.

---

## 7. Where to go next

The main [README](../README.md) covers the four observability gaps, the full
signals reference, and what the project deliberately does not do.

Gap 1 — silent semantic failure — has a signal rather than a hole, carried here
by audit records rather than spans and metrics:
[`UPDATE-GAPS-1-2.md`](UPDATE-GAPS-1-2.md) walks the judge that grades an answer
against the extracts it was given, the feedback endpoint that is the only input
the stack cannot generate about itself, and the security act — indirect prompt
injection through the corpus, tool poisoning, and the rug pull. It also says
which parts of all that have been verified and which have only been written.
