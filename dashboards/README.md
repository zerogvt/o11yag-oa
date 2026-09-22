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

## Known gaps

- **`o11yag.approval.wait` has no tile.** The metric only exists once the approval
  gate actually fires, which needs a refund above `AUTO_APPROVE_MAX_EUR` — send one
  for `ORD-1004` (€430). Add the tile once there is data behind it rather than
  shipping an empty one.
- **The gap 1 and gap 2 series have no tiles yet.** `o11yag.answer.quality`,
  `o11yag.feedback` and `o11yag.security.events` were added after this dashboard
  was validated against a live tenant, and every query in this file was written
  against real data on purpose. `o11yag.security.events` is the one to be most
  careful with: it is empty unless an attack is switched on, so an empty tile is
  the healthy case and reads identically to a broken one. Give it a threshold and
  a sentence of tile text before shipping it, or leave it out.
- **`o11yag.answer.quality` deserves a split, not a total.** The useful chart is
  verdict by `decided_by` — a rising `unsupported` count decided by `heuristic`
  is the model inventing figures, and the same count decided by `model` is a
  judge that may simply be wrong. Summed into one number they cancel.
- **No audit-record tiles.** Tool arguments, approval decisions and conversation
  text are emitted as OTLP **logs**, not metrics, and need the `logs.ingest` scope
  on the Dynatrace token. Without it the Collector accepts them and Dynatrace
  returns 403, which is silent unless you read the Collector's own log.
- **Cost is priced, not measured.** `COST_PER_1K_TOKENS_USD` prices tokens as if a
  hosted model were behind LiteLLM. The tiles prove the attribution works; they are
  not a spend figure.
