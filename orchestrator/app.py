"""o11yag orchestrator — the supervisor agent.

Owns the ticket end to end: classifies intent, routes to one worker, and closes
the workflow with the roll-up nobody gets for free (cost, tokens and LLM calls
per *resolved ticket*, not per model call).

Upstream, this is also where request-scoped attribution is set: Traceloop
association properties put tenant / customer / ticket on every span the
workflow produces, workers included. That is OTel code and is gone here. The
tenant and customer now reach telemetry only through the audit record.
"""
import re
import uuid

import requests
from flask import Flask, jsonify, request

import o11y
from config import Config

app = Flask(__name__)

o11y.init(Config.SERVICE_NAME)

import llm  # noqa: E402

INTENTS = ("question", "order_action", "escalate")

CLASSIFY_PROMPT = (
    "Classify the customer support message into exactly one word from this list: "
    "question, order_action, escalate.\n"
    "question = asking about a policy or how something works.\n"
    "order_action = wants something done to an order (refund, cancel, status).\n"
    "escalate = angry, legal threat, or asking for a human.\n"
    "Answer with the single word only."
)


ORDER_RE = re.compile(r"\bORD-\d+\b", re.I)

QUESTION_OPENERS = ("how ", "what ", "when ", "where ", "why ", "can i ",
                    "do you ", "is there", "are there", "do i ")


def _heuristic_intent(text: str) -> str:
    """Fallback when the model returns something that isn't one of the labels.

    qwen:0.5b regularly ignores 'answer with one word', so this decides more
    often than the model does. That makes its ordering load-bearing, and the
    obvious keyword sweep gets it wrong: matching "refund" or "cancel" anywhere
    sends "how long do refunds take to show up on my card?" to the CRM, which
    has no idea, instead of to the knowledge base, which does. The knowledge
    worker then receives no traffic at all and half the architecture goes dark
    while every ticket still reports `outcome=ok`.

    So the tests run most-specific first:
      1. escalation words win outright — never answer those automatically
      2. an explicit order reference means there is something concrete to act on
      3. an interrogative is a policy question, even when it says "refund"
      4. only then does a bare action word imply an action
    """
    low = text.lower().strip()
    if any(w in low for w in ("lawyer", "sue", "manager", "human", "complaint")):
        return "escalate"
    if ORDER_RE.search(text):
        return "order_action"
    if low.startswith(QUESTION_OPENERS) or low.endswith("?"):
        return "question"
    if any(w in low for w in ("refund", "cancel", "money back", "return my")):
        return "order_action"
    return "question"


def classify(text: str):
    try:
        raw, tokens = llm.chat(
            [{"role": "system", "content": CLASSIFY_PROMPT}, {"role": "user", "content": text}],
            max_tokens=8,
        )
    except Exception as exc:  # gateway or model down: route on keywords instead
        app.logger.warning("classification failed, using heuristic: %s", exc)
        return _heuristic_intent(text), 0, "heuristic_error"

    word = raw.strip().strip(".").split()[0].lower() if raw.strip() else ""
    if word in INTENTS:
        return word, tokens, "model"
    return _heuristic_intent(text), tokens, "heuristic_fallback"


def delegate(url: str, payload: dict):
    """One hop to a worker. Any trace context on it is OneAgent's."""
    r = requests.post(url, json=payload, timeout=Config.WORKER_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def handle(body: dict):
    ticket_id = body.get("ticket_id") or uuid.uuid4().hex[:12]
    customer_id = str(body.get("customer_id", "unknown"))
    tenant = str(body.get("tenant", "acme"))
    text = str(body.get("text", ""))

    with o11y.timed() as elapsed:
        intent, tokens, how = classify(text)
        llm_calls = 1
        outcome, answer, quality = "ok", "", ""

        try:
            if intent == "question":
                res = delegate(f"{Config.KNOWLEDGE_URL}/answer",
                               {"ticket_id": ticket_id, "text": text})
                answer = res.get("answer", "")
                tokens += res.get("tokens", 0)
                llm_calls += res.get("llm_calls", 0)
                # The knowledge worker graded its own answer. Carrying the
                # verdict up here puts it in the ticket's audit record next to
                # the cost, which is where anyone asking "what did we spend on
                # answers we then judged unsupported" has to start.
                quality = res.get("quality", "")
            elif intent == "order_action":
                res = delegate(f"{Config.ACTION_URL}/act",
                               {"ticket_id": ticket_id, "customer_id": customer_id, "text": text})
                answer = res.get("answer", "")
                tokens += res.get("tokens", 0)
                llm_calls += res.get("llm_calls", 0)
                outcome = res.get("outcome", "ok")
            else:
                answer = "Handing you to a human colleague now."
                outcome = "escalated"
        except Exception as exc:
            app.logger.exception("worker call failed")
            outcome, answer = "error", "Sorry — we couldn't complete that right now."
            res = {"error": str(exc)}

        ms = elapsed()

    cost = tokens / 1000.0 * Config.COST_PER_1K_TOKENS_USD
    o11y.task_finished(intent=intent, tenant=tenant, outcome=outcome, ms=ms,
                       llm_calls=llm_calls, tokens=tokens, cost_usd=cost)

    content = {}
    if Config.AUDIT_LOG_CONTENT:
        content = {
            "prompt": text[: Config.AUDIT_MAX_TEXT_CHARS],
            "response": answer[: Config.AUDIT_MAX_TEXT_CHARS],
        }
    o11y.audit(
        "o11yag.ticket.handled",
        ticket_id=ticket_id, customer_id=customer_id, tenant=tenant,
        intent=intent, intent_source=how, outcome=outcome,
        quality=quality or None,
        latency_ms=round(ms, 1), llm_calls=llm_calls, tokens=tokens,
        cost_usd=round(cost, 6),
        content_mode="text" if Config.AUDIT_LOG_CONTENT else "omitted:disabled",
        **content,
    )

    # Upstream returns the trace id here so feedback arriving later can name the
    # trace it is about. Without the OTel API there is no trace id to read, so
    # the field stays in the response, empty, and the only join left for
    # feedback is the ticket id.
    return {"ticket_id": ticket_id, "intent": intent, "outcome": outcome,
            "answer": answer, "tokens": tokens, "llm_calls": llm_calls,
            "cost_usd": round(cost, 6), "latency_ms": round(ms, 1),
            "quality": quality,
            "trace_id": ""}


@app.post("/chat")
def chat_route():
    return jsonify(handle(request.get_json(force=True, silent=True) or {}))


@app.post("/feedback")
def feedback_route():
    """A human's verdict on an answer that already went out.

    The other half of gap 1, and the half that cannot be faked. Everything else
    in this stack is the stack's opinion of itself: the retrieval floor, the
    judge, the loop signals. All of them can be confidently, consistently wrong
    together, and when they are, nothing internal disagrees. This endpoint is
    the only input that can.

    It lives on the orchestrator because the orchestrator owns the ticket — the
    same place the cost and the intent were recorded, so the verdict lands
    beside them rather than in a system nobody joins to.

    It is a separate request, minutes or days later, on a trace of its own.
    Upstream, `subject_trace_id` names the trace being graded, taken from what
    /chat returned. Here /chat returns an empty trace id, so the field is
    normally absent and the ticket id is the only join.

    Deliberately unauthenticated and unvalidated beyond the shape, like the rest
    of the stack. In a real deployment the rating has to be attributable, or the
    series is a vote anyone can stuff.
    """
    body = request.get_json(force=True, silent=True) or {}
    ticket_id = str(body.get("ticket_id", "")).strip()
    rating = str(body.get("rating", "")).strip().lower()
    if not ticket_id or rating not in ("up", "down"):
        return jsonify({"error": "ticket_id and rating (up|down) are required"}), 400

    intent = str(body.get("intent", "unknown"))
    tenant = str(body.get("tenant", "acme"))
    o11y.feedback_received(rating=rating, intent=intent, tenant=tenant)
    o11y.audit("o11yag.feedback.received",
               ticket_id=ticket_id, rating=rating, intent=intent, tenant=tenant,
               subject_trace_id=str(body.get("trace_id", ""))[:32] or None,
               comment=str(body.get("comment", ""))[: Config.AUDIT_MAX_TEXT_CHARS] or None)
    return jsonify({"ok": True, "ticket_id": ticket_id, "rating": rating})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": Config.SERVICE_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT)
