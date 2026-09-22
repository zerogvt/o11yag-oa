# o11yag Dynatrace dashboard

A Dynatrace **platform dashboard** built from the metrics the o11yag services
emit through OpenLLMetry → OTel Collector → Dynatrace.

## Deploy

```
./deploy.sh                 # create, or update the one in .dashboard-id
./deploy.sh <document-id>   # update a specific dashboard
```

`deploy.sh` posts `o11yag.json` to the Dynatrace **Document API** using
`$DT_ENVIRONMENT` and `$DT_PLATFORM_TOKEN` — the same two variables the
Dynatrace MCP plugin reads. `dtctl` is not needed and neither is a manual
upload.

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

Every query was validated against the live tenant before the file was written —
all twelve data tiles returned rows, not just valid syntax.

The file is also a valid **Dashboards → Upload** import if you would rather do it
by hand; it is the same top-level `tiles` / `layouts` shape as
`observai/dashboards/observAI.json`.

## Tiles

| Row | Tiles | Question it answers |
|---|---|---|
| KPIs | Tickets handled · Error rate · Cost · Avg loop depth | Is it working, and what did it cost? |
| Volume | Ticket volume by outcome · Ticket mix by intent | What came in, and where did it get routed? |
| Agent work | Loop depth per ticket · Ticket latency | How hard did the agent work per ticket, and is that changing? |
| Economics | Tokens by intent · Cost by intent | Which kind of ticket is expensive? |
| Tools & RAG | MCP tool calls · Retrieval quality | What did the agent *do*, and did retrieval give it anything to stand on? |

## Why these metrics and not the LLM-call ones

OpenLLMetry already gives you per-call model, token and latency spans for free.
This dashboard deliberately shows the roll-ups it does **not** give you, because
those are the ones a person actually asks about:

- **Per resolved ticket, not per LLM call.** Nobody budgets per model call.
- **Loop depth** (`o11yag.task.llm_calls`) is the headline series. Agent work is
  non-deterministic — the same ticket can legitimately take 3 calls today and 11
  tomorrow — so there is no fixed call graph to alert against. This series is the
  closest thing to a baseline, and it is the right input for Davis anomaly
  detection.
- **Ticket mix by intent** is a correctness check disguised as a volume chart. If
  an intent is missing entirely, routing is broken and a whole branch of the
  architecture is dark while every ticket still reports `outcome=ok`. That is not
  hypothetical — it is exactly how the knowledge worker was found receiving zero
  traffic.
- **Retrieval quality**, not retrieval latency. A vector search that takes 9ms and
  returns nothing relevant looks healthy until you record the score.

## The empty Error rate tile, and why

Worth writing down, because it cost real time and the failure is silent.

The tile used to compute its percentage from two series in one block:

```
timeseries {
  all = sum(o11yag.tasks),
  err = sum(o11yag.tasks, filter: {outcome == "error"})
}
| fieldsAdd error_rate = arraySum(err) * 100.0 / arraySum(all)
```

When the timeframe contains no error tickets, `err` matches nothing — and a
`timeseries` block in which **one** aggregation matches nothing returns **no rows
at all**, dropping `all` with it. Not a null percentage, not zero: an empty
result. So the tile went blank precisely when the system was healthiest, on a
dashboard whose neighbouring tiles were full of data. `default: 0` does not help;
the row is gone before the default would apply.

Verified against the tenant: over a 9-hour window with 320 tickets and no errors,
the block returned zero records while `timeseries all = sum(o11yag.tasks)` alone
returned 320.

The fix is to never filter a second series — group on the dimension instead, so
every row comes from data that exists, and do the arithmetic afterwards:

```
timeseries tickets = sum(o11yag.tasks), by: {outcome}
| fieldsAdd n = arraySum(tickets)
| summarize all = sum(n), err = sum(if(outcome == "error", n, else: 0))
| fieldsAdd error_rate = err * 100.0 / all
| fields error_rate
```

Same window, same data: `0`. Which is the answer, and is what a healthy system
should display.

## Known gaps

- **Three tiles are empty in the healthy case**, and an empty tile is
  indistinguishable from a broken one. Human feedback is empty because nothing
  sends feedback automatically; Security detections is empty because every attack
  is off by default; Answer quality is empty when no ticket routed to `question`.
  Each tile's description says so, and the footer tile says to check **Tickets
  handled** first. That is the only mitigation short of a threshold, and it is
  worth restating whenever one of these is added to an alert.
- **`o11yag.answer.quality` is charted twice on purpose.** Once by `verdict`, once
  by `verdict` *and* `decided_by`. A rising `unsupported` decided by `heuristic`
  is the model inventing figures; the same count decided by `model` may only be a
  weak judge. Summed into one series they cancel, so the split is the tile that
  matters and the total is the one that looks tidy.
- **The gap 1 and gap 2 queries were validated for syntax, not against data.**
  Every other query in this file was written against a live tenant with real
  numbers behind it. `o11yag.answer.quality`, `o11yag.feedback` and
  `o11yag.security.events` have never been ingested — the build that emits them
  has not been deployed — so their tiles are shape-correct and unproven. Re-check
  them once the stack has run.
- **No audit-record tiles.** Tool arguments, approval decisions and conversation
  text are emitted as OTLP **logs**, not metrics, and need the `logs.ingest` scope
  on the Dynatrace token. Without it the Collector accepts them and Dynatrace
  returns 403, which is silent unless you read the Collector's own log.
- **Cost is priced, not measured.** `COST_PER_1K_TOKENS_USD` prices tokens as if a
  hosted model were behind LiteLLM. The tiles prove the attribution works; they are
  not a spend figure.
