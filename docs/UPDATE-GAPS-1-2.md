# Update walkthrough — the quality signal and the security act

A guided read of what changed when gap 1 (silent semantic failure) and the
security act were built. Assumes you have read
[`WALKTHROUGH.md`](WALKTHROUGH.md) — this is the same kind of document for the
parts that did not exist then.

> Function names are used as anchors rather than line numbers.
> `grep -n "def <name>" <file>` will find any of them.

> To actually stage one of the gap 2 attacks rather than read about it, use
> [`SECURITY-DEMOS.md`](SECURITY-DEMOS.md) — this document explains the
> mechanism, that one is the operating procedure.

> **This is o11yag-oa.** These parts were built upstream (o11yag-otel), where
> their signals are OTel spans, span attributes and `o11yag.*` metrics. All of
> that instrumentation is removed here and Dynatrace OneAgent is injected
> instead. The logic is unchanged; what it records goes into audit records, one
> JSON object per line on stdout, picked up by OneAgent log monitoring. What
> OneAgent captures beyond that is not yet measured. Where this document names a
> signal, it says which one survives here.

---

## 1. The two ideas

**Gap 1.** Everything the stack had before measures whether the machinery ran.
Nothing measured whether the answer was *true*. Those are different questions,
and for an agent the second one fails silently: the customer gets a confident
paragraph, the span tree is clean, every RED metric is green. The retrieval floor
that already existed catches only the narrow case where the model had nothing to
stand on. The larger case — right document retrieved, wrong answer written —
needed a signal that did not exist.

**Gap 2.** An agent trusts two things it did not write: the documents it
retrieves and the tool descriptions it is handed. Both are attacker-controlled in
any real deployment. An instruction planted in either one produces a trace that
is *indistinguishable from the agent deciding by itself* — well-formed call, in
schema, and upstream even tagged `step.decided_by=model`. There was no signal for
that either.

Both gaps have the same shape, which is why they were built together: **the
system succeeds, and the success is the bug.**

---

## 2. The moving parts

| File | New? | What it is |
|---|---|---|
| `knowledge-worker/judge.py` | new | Grades an answer against its extracts. Two graders |
| `knowledge-worker/security.py` | new | Screens retrieved documents *and* tool catalogues |
| `action-worker/security.py` | new | The same file, copied verbatim (see below) |
| `knowledge-worker/app.py` | changed | Two new stages, `screen()` and `judge_answer()` (upstream: two new spans) |
| `knowledge-worker/kb.py` | changed | `POISON_DOC` + `docs()`, gated by a flag |
| `knowledge-worker/retrieval.py` | changed | Reseeds when the corpus size changes |
| `action-worker/app.py` | changed | `_screen_catalogue()` between discovery and the loop |
| `mcp-crm/server.py` | changed | Can advertise a poisoned `issue_refund` description |
| `orchestrator/app.py` | changed | Returns `trace_id` (always empty here); new `POST /feedback` |
| `*/o11y.py` | changed | Upstream: three new metric families. Here: `answer_judged()`, `feedback_received()` and `security_event()` exist as no-ops |
| `litellm/k8s/…` | changed | New `support-judge` gateway alias |

**Why `security.py` is duplicated.** Same reason as `o11y.py`: each Docker build
context is its own directory, so a shared module has to be a copied file. The two
copies are byte-identical and must stay that way — `md5sum */security.py` is the
check. It is one file rather than two focused ones because it answers one
question of two inputs: *did something that should only supply data just supply
an instruction?*

---

## 3. Gap 1 — three guards at three distances from the truth

### Stop 1 — `knowledge-worker/app.py`, the `answer()` function

Read it top to bottom. The order is the design:

```
retrieve()        → hits, scored          (existing)
screen()          → drop poisoned docs    (new — gap 2, but it runs first)
MIN_SCORE floor   → refuse, or continue   (existing)
generate()        → the answer            (existing)
judge_answer()    → a verdict on it       (new)
JUDGE_ACTION      → send it, or withhold  (new)
```

Two orderings in there are load-bearing and easy to get backwards:

- **Screening happens before the floor.** A quarantined document is not
  evidence, so the floor has to be applied to the best *usable* score. Dropping
  the top hit can legitimately push the ticket into the ungrounded path — that is
  the stack declining to answer off a corpus it cannot trust, not an edge case.
- **Judging happens after generation, in its own function.** Upstream gives it
  its own span; folding it into `generate_answer` would blend answering latency
  with checking latency and make "what did the quality signal cost" unanswerable.
  Same reasoning that keeps `approval_wait` out of the tool call. Here neither
  span exists, so whether OneAgent separates the judge's LLM call from the
  answer's is not yet measured; the judge's tokens are in the total `tokens` on
  `o11yag.answer.generated`, not on their own.

### Stop 2 — `knowledge-worker/judge.py`, the `grade()` function

The control flow, in the order the code takes it:

```
JUDGE_MODE == "off"          → verdict "skipped", nothing runs
heuristic() says unsupported → verdict "unsupported", decided_by "heuristic"
                               the model is NOT consulted — nothing it could say
                               makes an invented figure supported
JUDGE_MODE != "model"        → verdict "supported", decided_by "heuristic"
otherwise                    → ask the judge model; it may overrule the pass
```

`heuristic()` itself, also in order:

| Check | Fires on | Reason string |
|---|---|---|
| invented numbers | a figure in the answer absent from the extracts | `unsupported_number:60` |
| decline | the answer declines to answer | `declined` (passes) |
| no content words | nothing was claimed | `no_claims` (passes) |
| overlap | content words largely absent from the extracts | `low_overlap:0.12` |

The number check runs first on purpose: "I don't know, but returns are 60 days"
is a decline *and* an invented figure, and the figure is what matters.

The decline check exists because it was the judge's most common false positive on
this corpus, and the expensive kind — a correct refusal is by construction made
of words the extracts do not contain, so the overlap check read the one honest
failure mode as the dishonest one.

### Stop 3 — `decided_by`, and why it is not decoration

The judge is itself a model, and here it is the same `qwen:0.5b` that could not
reliably emit a tool call. So the verdict alone is not enough — you need to know
who produced it:

| `decided_by` | `verdict` | What actually happened |
|---|---|---|
| `heuristic` | `unsupported` | A deterministic check fired. Trustworthy |
| `heuristic` | `supported` | Checks passed and the model was not asked, or answered unusably — read `reason` |
| `model` | either | The judge model produced a real boolean |
| `none` | `skipped` | `JUDGE_MODE: off` |

A judge that returns nothing usable is recorded as
`decided_by=heuristic, reason=model_unparseable` — **never as a pass**. That
distinction is the whole reason the field exists: without it, a judge that has
never once produced a verdict looks exactly like a judge that always agrees.

### Stop 4 — `orchestrator/app.py`, `feedback_route()`

Everything above is the stack's opinion of itself, and all of it can be
confidently wrong at once with nothing internal disagreeing. `POST /feedback` is
the only input that can contradict it.

The mechanics worth noticing:

- **`/chat` returns `trace_id`, but here it is always empty.** Feedback arrives
  minutes or days later on a trace of its own; upstream hands the caller the
  trace id so the rating can name the trace it is about. Without the OTel API
  there is no trace id to read, so here the only join is the ticket id, and a
  thumbs-down is a thing you can count but not open.
- **Upstream puts two trace ids on one record**: `audit.trace_id` (this request)
  and `audit.subject_trace_id` (the trace being rated). Here the audit record
  has no `trace_id`, and `subject_trace_id` is only present if a caller supplies
  one.
- It lives on the orchestrator because the orchestrator owns the ticket — the
  verdict lands beside the cost and the intent rather than in a system nobody
  joins to.

Nothing sends feedback automatically. `loadgen` does not fabricate ratings, for
the same reason the approval gate's honest mode needs a human: a synthetic
thumbs-down measures nothing.

---

## 4. Gap 2 — three attacks, one question

### Stop 5 — `knowledge-worker/security.py`, the `scan()` function

One pattern list, grouped by **what the instruction is trying to do** rather than
by phrasing, because the kind of attempt is what belongs in an alert:

| Kind | Trying to |
|---|---|
| `override` | Cancel the standing instructions |
| `impersonate` | Reassign the model's role |
| `tool_use` | Direct a specific tool call |
| `conceal` | Keep the result from the customer |
| `exfiltrate` | Send something to an external address |

`conceal` is the tell. A legitimate policy document has no reason to care what
the customer is told.

### Stop 6 — indirect prompt injection

Read `kb.py`'s `POISON_DOC` and notice what it is *not*: it is not obviously
malicious text. The first two sentences are plausible refund policy, which is
what makes it embed close to genuine refund questions and come back with a high
score against exactly the tickets that make the instruction worth planting.

Then read `screen()` in `app.py`. The screening point is between retrieval and
the prompt, and that position is the entire design: it is the only place where
the text is still identifiable as something the *corpus* said rather than
something the customer said. Once it is inside the context block it is
indistinguishable from policy, which is the mechanism of the attack.

`INJECTION_ACTION: quarantine` drops the document before the context is built. A
detector that logs the finding and prompts with the document anyway has recorded
an attack it also carried out.

**Know the ceiling of this demo before you run it.** The document asks for a
refund and cannot get one: the knowledge worker has no tools, and the agent that
does never retrieves. Switching `INJECTION_ACTION` to `observe` shows the
instruction reaching the model and colouring the *answer* — not a refund, not a
tool call, not money moving. The version that ends in a tool call is Stop 7's,
where the attacker-controlled text is a tool description and the agent reading it
holds the tools.

### Stop 7 — `action-worker/app.py`, `_screen_catalogue()`

Runs once per ticket, between `mcp_client.list_tools()` and the loop. It catches
two different things:

**Tool poisoning** — an instruction written into a description. It reaches the
planner's system prompt as capability documentation, and the agent is *built* to
act on it. If it works, the resulting call is well-formed and in-schema, so
`planner.validate_args` passes it: the arguments really are valid. It was the
intent that was supplied by an attacker, and no schema check can see intent.

**The rug pull** — the description changing after the server earned trust. Names
identical, schemas identical, tool list identical. Nothing in a normal trace
moves. The only thing that catches it is `catalogue_digest()`: a sha256 over
names, descriptions and schemas, sorted by name so the server's iteration order
is not mistaken for a change.

Know which baseline was in force before you trust a change. Upstream puts it on
the span as `mcp.tools.baseline`; here it is the `baseline` field on the
`o11yag.security.tool_catalogue_changed` audit record:

| Baseline | Set by | Catches | Blind to |
|---|---|---|---|
| `pinned` | `MCP_TOOLS_DIGEST` in config | A server poisoned before this pod ever ran | Nothing, while the pin is current |
| `first_seen` | Nothing — the default | A change during this pod's life | A server that was already poisoned at boot. A restart forgets |

The digest is logged on first sight, so pinning it is copy and paste.

### Stop 8 — why nothing blocks

`redact` blanks a flagged description and keeps the tool callable. It does not
drop the tool, because an attacker who can write a description could then disable
any tool they liked — the control would become the denial of service.

A changed catalogue is recorded and nothing stops. A worker that refuses to run
because a description changed cannot tell a deploy from an attack, and hands
anyone who can edit a description an outage.

The control that holds regardless of all of this is the approval gate, which does
not care who asked for the refund.

---

## 5. One question ticket, end to end

```
POST /chat                            orchestrator
└─ handle()
   ├─ classify()                      LLM: which intent?
   └─ delegate()                      HTTP → knowledge worker
      └─ POST /answer
         └─ answer()
            ├─ retrieve()             embed + qdrant
            ├─ screen()               ← NEW  audit: o11yag.security.injection_detected
            ├─ generate()             LLM: the answer
            └─ judge_answer()         ← NEW  a second LLM call
   audit: o11yag.answer.generated     top_score, quality_verdict, quality_decided_by
   audit: o11yag.ticket.handled       quality now rides along in it

POST /feedback                        orchestrator — ITS OWN TRACE, later
```

Upstream, every function here is decorated (`@workflow`, `@task`, `@agent`), so
the tree is the waterfall and the two new stages appear as spans carrying
`security.injection.*` and `quality.*`. Here nothing is decorated: how much of
this tree OneAgent reconstructs from the HTTP and LLM calls is not yet measured.

---

## 6. Every new switch, and its default

| Setting | Where | Default | Flip it to |
|---|---|---|---|
| `JUDGE_MODE` | knowledge worker | `model` | `heuristic` for zero marginal cost; `off` to disable |
| `JUDGE_MODEL` | knowledge worker | `support-judge` | A capable model — edit LiteLLM's ConfigMap, not the code |
| `JUDGE_MIN_OVERLAP` | knowledge worker | `0.35` | `0.9` to force the plumbing to fire on ordinary paraphrase |
| `JUDGE_ACTION` | knowledge worker | `observe` | `withhold` — stops the answer reaching the customer |
| `KB_POISON_DOC` | knowledge worker | `false` | `true` to seed the injection document |
| `INJECTION_ACTION` | knowledge worker | `quarantine` | `observe` to watch the attack work unopposed |
| `MCP_TOOLS_DIGEST` | action worker | *(empty)* | The logged digest, to pin it |
| `TOOL_POISON_ACTION` | action worker | `redact` | `observe` to let the poisoned description reach the planner |
| `POISON_TOOL_DESCRIPTION` | **mcp-crm** | `false` | `true` — the server starts lying |

Everything adversarial is off by default. An attack that ships enabled inside a
reference architecture is indistinguishable from a backdoor.

`JUDGE_ACTION` is the one to think hardest about. `observe` is the default
because a weak judge withholding good answers is a worse product than a wrong
answer you can see in the audit records — but `observe` means the unsupported answer
*is still sent*. Nobody should flip it to `withhold` before measuring their own
false-positive rate on their own corpus.

---

## 7. Try it

Each of these produces one specific audit record. After a ConfigMap edit, run
`bash build_deploy.sh --no-build`; it restarts only the pods whose config
changed. Read the records with
`kubectl logs deploy/<service> -n o11yag-oa | grep '^{'`.

**An answer graded unsupported.** The blunt way, which exercises the plumbing
rather than a real hallucination: set `JUDGE_MIN_OVERLAP: "0.9"` and ask anything.
Look for `audit.quality_verdict=unsupported`,
`audit.quality_decided_by=heuristic`, `audit.quality_reason=low_overlap:…` on
`o11yag.answer.generated` in the knowledge-worker log.

**A thumbs-down joined to its ticket.** Upstream joins it to the trace; here
`/chat` returns an empty `trace_id`, so the ticket id is the join.

```
curl -X POST http://localhost:8000/feedback -H 'Content-Type: application/json' \
  -d '{"ticket_id":"LEARN-9","rating":"down","intent":"question",
       "comment":"quoted a 60-day return window that does not exist"}'
```

Then find both records for the ticket: `o11yag.feedback.received` and
`o11yag.ticket.handled` with `audit.ticket_id == "LEARN-9"`, in the orchestrator
log, or in Dynatrace once OneAgent log monitoring has ingested them.

**An injection landing, then stopped.** Set `KB_POISON_DOC: "true"` and
`INJECTION_ACTION: "observe"`, then ask about a faulty item and read the
generated answer and the agent's behaviour. Then set `INJECTION_ACTION` back to
`quarantine` and ask again: an `o11yag.security.injection_detected` record is
written in both runs; what changes is its `action`, and what the model was given.

**A rug pull.** With the stack running and traffic flowing, set
`POISON_TOOL_DESCRIPTION: "true"` on the **mcp-crm** ConfigMap and roll that pod
only. The next ticket writes an `o11yag.security.tool_catalogue_changed` audit
record with both digests (`digest`, `expected`) and the `baseline` in force.
Upstream also marks the `action_worker` span `mcp.tools.changed=true`.

### Not yet built: producing a genuinely false answer

Worth separating from everything above, because the two look identical in the
records and are not the same test.

Raising `JUDGE_MIN_OVERLAP` to `0.9` flags ordinary paraphrase. It exercises the
judge and its audit record, and it tells you nothing about hallucination — it
is a plumbing test wearing a quality test's clothes.

Inducing an answer that is actually unsupported needs three things, none of which
exist yet:

- **`MIN_SCORE: "0.0"` on the knowledge worker.** Without it the retrieval floor
  refuses most trap questions before the model ever answers, and a refusal scores
  `declined`, not `unsupported`. Dropping the floor converts the refuse path into
  the answer-badly path, which is the failure gap 1 is actually about. This one
  is only a ConfigMap edit.
- **Trap questions in `loadgen/tickets.py`** — questions demanding a figure the
  corpus does not contain ("the restocking fee for opened electronics", "the
  express delivery surcharge"), which invite an invented number and trip
  `unsupported_number:N` rather than the fuzzier overlap check. The rate is
  arithmetic, because POPULATION repeats each ticket `weight` times: 15 of the 23
  entries are policy questions, so traps of total weight `t` are `t / (15 + t)`
  of everything the knowledge worker answers — weight 4 is roughly 1 in 5. Traps
  must be phrased as questions and carry no `ORD-` reference or escalation word,
  or the orchestrator never routes them here. Build it as a flag that is off by
  default, like the security demos.
- **`ANSWER_TEMPERATURE` on the knowledge worker.** `llm.py` hardcodes
  `temperature=0.2`; temperature is the most direct dial on invention and there
  is currently no way to turn it.

The reason to build this is not the demo. It is that every verdict the judge
records today is unlabelled: 5 of the first 12 answers were flagged
`low_overlap`, and nothing in the stack can say whether those were right. Trap
questions are the only cheap source of ground truth — a known-bad answer the
judge *should* catch, and a known-good one it should leave alone. Without them,
"the judge flags 40% of answers" is a number with no denominator you can trust.

---

## 8. What has *not* been proven

Stated plainly, because this is the part a reader would otherwise assume:

- ~~The judge's **model** path has never been asked for a verdict by a real
  model.~~ **Now run, and it fails.** 10 attempts against a live `qwen:0.5b`
  produced 0 usable verdicts — 4 `model_no_verdict`, 2 `model_unparseable`, the
  rest settled by the heuristic first — for 2,353 tokens and 1,462 ms p50 of
  added latency per ticket. The plumbing works; the judge does not. See the
  README's gap 1 section.
- The reseed that makes `KB_POISON_DOC` take effect has never run against a live
  Qdrant.
- No agent has been observed actually **complying** with an injection it was fed.
  The detectors are verified; the demonstrations they exist for are not.

What *was* verified, as pure functions against the real corpus and a real tool
catalogue: zero false positives from the injection patterns across all eight
benign documents, the poison document caught, the digest stable under reordering
and moving on a changed description, and the heuristic separating an invented
figure from grounded paraphrase and from an honest refusal.

---

## 9. Where the ceiling is

- **The detectors are pattern matches over English imperatives.** They will miss
  an injection in another language, split across two documents, or simply phrased
  in a way the list does not cover. A hit is a finding about the *pipeline* —
  someone wrote to the corpus who should not have — and the right response is to
  go and look at how that document got in, not to tune the regex.
- **There is still no evaluation loop.** A quality *signal* is not a closed loop.
  No offline evaluation set, no regression suite, no golden answers, and nothing
  routes a thumbs-down back into retrieval or the prompt.
- **A judge sharing the graded model's weights shares its blind spots.** Today
  `support-judge` and `support-chat` both resolve to `qwen:0.5b`, so treat
  agreement between them as no evidence at all. That is what the separate alias
  is for.
