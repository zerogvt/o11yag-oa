# Making tickets fail

On a healthy stack no ticket ends in `error`. This is how to change that: which
failure to cause, what it costs, how to see it, and how to put it back.

> **This repo has no error-rate metric.** Upstream (o11yag-otel) drives an
> **Error rate** dashboard tile from the `o11yag.tasks` metric. Here that
> metric is a no-op (see `o11y.py`), so the tile stays empty. What is left is
> the orchestrator's audit record for every ticket, in its pod log, and
> whatever OneAgent captures. What OneAgent shows for these failures has not
> been measured yet.

**What counts.** A ticket ends in `error` in exactly two ways:

1. the orchestrator's call to a worker raises (connection refused, timeout,
   HTTP 5xx) — `handle()` in `orchestrator/app.py`;
2. the action worker cannot reach the MCP server and returns `outcome=error` —
   `act()` in `action-worker/app.py`.

`blocked` (the approval gate stopping ORD-1004), `escalated` and `incomplete` are
**not** errors. Losing LiteLLM does not reliably produce errors either —
classification falls back to keywords instead of failing.

**Count outcomes from the audit records.** The orchestrator writes one
`o11yag.ticket.handled` record per ticket to stdout, as a JSON line. This tallies
them for the last 30 minutes:

```
kubectl logs deploy/o11yag-orchestrator -n o11yag-oa --since=30m \
  | grep '"audit.event.type": "o11yag.ticket.handled"' \
  | grep -o '"audit.outcome": "[a-z_]*"' | sort | uniq -c
```

In Dynatrace, a starting point once log monitoring is confirmed to be ingesting
them (not yet measured, including whether the JSON fields are parsed):

```
fetch logs, from: now()-30m
| filter k8s.namespace.name == "o11yag-oa"
| filter contains(content, "o11yag.ticket.handled")
```

> Recipe 1 was run once, upstream, on 2026-09-23 (see its section). Recipes 2
> and 3 have not been run yet, and their percentages come from the loadgen
> weights in `loadgen/tickets.py`, not from a measurement. None of them has
> been run on this repo.

---

## Recipe 1 — Take the CRM away (recommended)

The cleanest demo: only order actions fail, so the error share lands at a
believable partial rate instead of jumping to most of the traffic.

```
kubectl scale deployment/o11yag-mcp-crm -n o11yag-oa --replicas=0
```

Leave loadgen running for 5–10 minutes.

**What you should see**

| Where | What |
|---|---|
| Orchestrator audit records | roughly **25%** with `"audit.outcome": "error"` — 6 of the 23 weighted tickets are order actions; `blocked` disappears, because ORD-1004 never reaches the gate |
| Orchestrator audit record | `o11yag.ticket.handled` with `outcome = error`, `intent = order_action` |
| Action worker log | `MCP server unreachable` with a traceback |
| Dynatrace | not yet measured. Upstream also marks the `action_worker.agent` span with `error.kind = mcp_unavailable`; that span attribute is upstream-only |

```
kubectl logs deploy/o11yag-action-worker -n o11yag-oa --since=30m | grep 'MCP server unreachable'
kubectl logs deploy/o11yag-orchestrator -n o11yag-oa --since=30m \
  | grep '"audit.event.type": "o11yag.ticket.handled"' | grep '"audit.outcome": "error"'
```

**Measured upstream (2026-09-23, 7 minutes down).** 15 tickets arrived while
mcp-crm was down: 2 `error`, 1 `blocked` (most likely a ticket already in flight
at the moment of the scale-down), 5 `escalated`, 7 `ok`. That is 13% inside the
outage, not the ~25% the weights suggest, because a sample that small follows
whatever loadgen happened to pick. Upstream's tile read **4.8%** (2 of 42) over
_Last 30 minutes_, because the healthy 23 minutes before the outage dilute it.
Leave it down longer if you want a bigger number.

Put it back:

```
kubectl scale deployment/o11yag-mcp-crm -n o11yag-oa --replicas=1
```

---

## Recipe 2 — Take the knowledge worker away

Every policy question fails at the orchestrator's hop. Loud and immediate —
connection refused, no waiting on a timeout.

```
kubectl scale deployment/o11yag-knowledge-worker -n o11yag-oa --replicas=0
```

**What you should see:** roughly **65%** of tickets ending in `error` (15 of 23
weighted tickets are questions), and the orchestrator log printing
`worker call failed` with a connection-refused traceback:

```
kubectl logs deploy/o11yag-orchestrator -n o11yag-oa --since=10m | grep -A3 'worker call failed'
```

Upstream also marks the `delegate_to_worker` span failed; that span is
upstream-only. Whether OneAgent shows the failed outbound HTTP call is not yet
measured.

Put it back:

```
kubectl scale deployment/o11yag-knowledge-worker -n o11yag-oa --replicas=1
```

The Qdrant collection is on a volume, so the worker comes back without
reseeding.

---

## Recipe 3 — Make the orchestrator impatient

Nothing is down; the orchestrator just stops waiting. This is what a real
latency regression looks like from the caller's side, which makes it the most
realistic of the three and the least predictable.

`orchestrator/k8s/o11yag-orchestrator.yaml`:

```yaml
  WORKER_TIMEOUT_S: "2"      # default 300
```

Redeploy, or apply and restart by hand — a ConfigMap change does not roll the
pod on its own (see [`SECURITY-DEMOS.md`](SECURITY-DEMOS.md) §0):

```
bash build_deploy.sh --no-build
kubectl exec deploy/o11yag-orchestrator -n o11yag-oa -- printenv WORKER_TIMEOUT_S
```

**What you should see:** `worker call failed` with a `ReadTimeout` in the
orchestrator log, and an error share that depends entirely on how slow the
model is. With `qwen:0.5b` on CPU most knowledge-worker answers should overrun
2 s; if few tickets fail, drop the value to `"1"`. The `latency_ms` in the
audit records stays flat, since the timeout caps it. Upstream shows the
`ReadTimeout` on `delegate_to_worker` spans (upstream-only); what OneAgent
shows is not yet measured.

Set it back to `"300"` and redeploy.

---

## Resetting

```
kubectl scale deployment/o11yag-mcp-crm deployment/o11yag-knowledge-worker \
  -n o11yag-oa --replicas=1
```

and make sure `WORKER_TIMEOUT_S` is `"300"` again. Re-running
`bash build_deploy.sh --no-build` also restores all three, because it re-applies
the manifests with `replicas: 1`.
