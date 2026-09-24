"""o11yag knowledge worker — RAG over the support knowledge base.

Answers policy questions by retrieving from Qdrant and grounding the model on
what came back. The observability point of this service is retrieval *quality*:
the scores go on the span and into a metric, so a wrong answer caused by a bad
retrieval is diagnosable instead of merely visible.
"""
from flask import Flask, jsonify, request

import o11y
from config import Config

app = Flask(__name__)

o11y.init(Config.SERVICE_NAME, Config.OTEL_EXPORTER_OTLP_ENDPOINT,
          enabled=Config.OTEL_ENABLED, flask_app=app)

try:
    from traceloop.sdk.decorators import agent, task
except ImportError:
    def agent(*a, **k):
        return lambda f: f

    task = agent

from opentelemetry import trace  # noqa: E402

import judge      # noqa: E402
import llm        # noqa: E402  (after init, so the OpenAI client is instrumented)
import retrieval  # noqa: E402
import security   # noqa: E402

tracer = trace.get_tracer("o11yag")

REFUSAL = "I don't have a policy that covers that — let me pass you to a colleague."

ANSWER_PROMPT = (
    "You are a customer support assistant. Answer the customer's question using "
    "ONLY the policy extracts provided. If the extracts do not contain the answer, "
    "say you don't know and offer to pass it to a colleague. Be brief."
)

retrieval.ensure_seeded()


@task(name="retrieve")
def retrieve(question: str):
    hits = retrieval.search(question, Config.TOP_K)
    top = hits[0][1] if hits else 0.0

    # What OpenLLMetry's qdrant span does not tell you: whether this was any good.
    span = trace.get_current_span()
    span.set_attribute("rag.hits", len(hits))
    span.set_attribute("rag.top_score", top)
    span.set_attribute("rag.doc_ids", [h[0] for h in hits])
    span.set_attribute("rag.scores", [round(h[1], 4) for h in hits])
    span.set_attribute("rag.grounded", top >= Config.MIN_SCORE)
    o11y.retrieval_scored(Config.COLLECTION, top, len(hits))
    return hits, top


@task(name="screen_retrieval")
def screen(hits, ticket_id: str):
    """ Security screening.
    Drop retrieved documents that contain instructions, before they are trusted.

    This sits between retrieval and the prompt because that is the only point at
    which the text is still identifiable as something the *corpus* said rather
    than something the customer said. Once it is inside the context block it is
    indistinguishable from policy, which is the entire mechanism of the attack.

    Everything upstream of here reports success while it happens: the document
    was retrieved on merit, so rag.top_score is high and no span errors.
    """
    clean, detections = security.screen_hits(hits)
    span = trace.get_current_span()
    span.set_attribute("security.injection.detected", bool(detections))
    if not detections:
        return hits

    quarantine = Config.INJECTION_ACTION == "quarantine"
    action = "quarantined" if quarantine else "observed"
    span.set_attribute("security.injection.action", action)
    span.set_attribute("security.injection.kinds", sorted({d["kind"] for d in detections}))
    span.set_attribute("security.injection.doc_ids", [d["doc_id"] for d in detections])

    for d in detections:
        o11y.security_event(kind="prompt_injection", action=action, source="rag_corpus")
        o11y.audit("o11yag.security.injection_detected",
                   ticket_id=ticket_id, doc_id=d["doc_id"], kind=d["kind"],
                   match=d["match"], score=d["score"], action=action,
                   collection=Config.COLLECTION)

    # `observe` returns the untouched hits on purpose: it is the mode that shows
    # what the injection does when nothing stops it.
    return clean if quarantine else hits


@task(name="judge_answer")
def judge_answer(question: str, answer: str, hits):
    """Grade the answer, on its own span so its latency and cost are separable.

    Kept out of generate_answer for the same reason approval_wait is kept out of
    the tool call: a second model call folded into the first makes every latency
    number in the service a blend of answering and checking, and there is then no
    way to say what the quality signal cost.
    """
    verdict = judge.grade(question, answer, hits)
    span = trace.get_current_span()
    span.set_attribute("quality.verdict", verdict["verdict"])
    span.set_attribute("quality.decided_by", verdict["decided_by"])
    span.set_attribute("quality.reason", verdict["reason"])
    span.set_attribute("quality.judge.mode", Config.JUDGE_MODE)
    span.set_attribute("quality.judge.model", Config.JUDGE_MODEL)
    span.set_attribute("quality.judge.tokens", verdict["tokens"])
    o11y.answer_judged(verdict["verdict"], verdict["decided_by"])
    return verdict


@task(name="generate_answer")
def generate(question: str, hits):
    context = "\n\n".join(f"[{doc_id}] {text}" for doc_id, _, text in hits)
    return llm.chat([
        {"role": "system", "content": ANSWER_PROMPT},
        {"role": "user", "content": f"Policy extracts:\n{context}\n\nQuestion: {question}"},
    ])


@agent(name="knowledge_worker")
def answer(body: dict):
    question = str(body.get("text", ""))
    ticket_id = body.get("ticket_id")
    hits, _ = retrieve(question)

    # Remove suspicious documents *before* the MIN_SCORE check below, not after.
    # Otherwise a poisoned document with a high score could get the question
    # past the check, then be removed, leaving an answer built on weaker
    # documents that never cleared the bar themselves. So only documents that
    # survive security screening count towards the check. If the poisoned one was the
    # best match, what is left may score below MIN_SCORE and the customer gets
    # the "no policy" reply. That is intended: when the only good match cannot
    # be trusted, saying "I don't know" beats letting the model guess.
    hits = screen(hits, ticket_id)
    top = hits[0][1] if hits else 0.0

    # Refuse to answer off an empty retrieval rather than let the model invent a
    # policy. This is the cheapest guard against the failure mode that matters
    # most here, and it turns a silent wrong answer into a visible signal.
    if not hits or top < Config.MIN_SCORE:
        o11y.audit("o11yag.retrieval.ungrounded",
                   ticket_id=ticket_id, question=question[:500],
                   top_score=round(top, 4), hits=len(hits))
        return {"answer": REFUSAL, "tokens": 0, "llm_calls": 0,
                "grounded": False, "top_score": round(top, 4), "quality": "skipped"}

    r = generate(question, hits)
    text = (r.choices[0].message.content or "").strip()
    tokens = r.usage.total_tokens if r.usage else 0
    llm_calls = 1

    verdict = judge_answer(question, text, hits)
    tokens += verdict["tokens"]
    llm_calls += verdict["llm_calls"]

    o11y.audit("o11yag.answer.generated",
               ticket_id=ticket_id, question=question[:500],
               answer=text[:2000], doc_ids=[h[0] for h in hits],
               top_score=round(top, 4), tokens=tokens,
               quality_verdict=verdict["verdict"],
               quality_decided_by=verdict["decided_by"],
               quality_reason=verdict["reason"])

    withheld = verdict["verdict"] == "unsupported" and Config.JUDGE_ACTION == "withhold"
    if withheld:
        # The answer the customer would have received is kept in the audit
        # record, not thrown away: "what did we nearly tell them" is the whole
        # value of having graded it, and it is the only copy once this returns.
        o11y.audit("o11yag.answer.withheld",
                   ticket_id=ticket_id, question=question[:500],
                   answer=text[:2000], reason=verdict["reason"],
                   decided_by=verdict["decided_by"])
        text = REFUSAL

    return {"answer": text, "tokens": tokens, "llm_calls": llm_calls,
            "grounded": True, "top_score": round(top, 4),
            "doc_ids": [h[0] for h in hits],
            "quality": verdict["verdict"], "withheld": withheld}


@app.post("/answer")
def answer_route():
    return jsonify(answer(request.get_json(force=True, silent=True) or {}))


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": Config.SERVICE_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT)
