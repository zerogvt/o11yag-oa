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

import llm        # noqa: E402  (after init, so the OpenAI client is instrumented)
import retrieval  # noqa: E402

tracer = trace.get_tracer("o11yag")

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
    hits, top = retrieve(question)

    # Refuse to answer off an empty retrieval rather than let the model invent a
    # policy. This is the cheapest guard against the failure mode that matters
    # most here, and it turns a silent wrong answer into a visible signal.
    if not hits or top < Config.MIN_SCORE:
        o11y.audit("o11yag.retrieval.ungrounded",
                   ticket_id=body.get("ticket_id"), question=question[:500],
                   top_score=round(top, 4), hits=len(hits))
        return {"answer": "I don't have a policy that covers that — let me pass you to a colleague.",
                "tokens": 0, "llm_calls": 0, "grounded": False, "top_score": round(top, 4)}

    r = generate(question, hits)
    text = (r.choices[0].message.content or "").strip()
    tokens = r.usage.total_tokens if r.usage else 0

    o11y.audit("o11yag.answer.generated",
               ticket_id=body.get("ticket_id"), question=question[:500],
               answer=text[:2000], doc_ids=[h[0] for h in hits],
               top_score=round(top, 4), tokens=tokens)

    return {"answer": text, "tokens": tokens, "llm_calls": 1,
            "grounded": True, "top_score": round(top, 4),
            "doc_ids": [h[0] for h in hits]}


@app.post("/answer")
def answer_route():
    return jsonify(answer(request.get_json(force=True, silent=True) or {}))


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": Config.SERVICE_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT)
