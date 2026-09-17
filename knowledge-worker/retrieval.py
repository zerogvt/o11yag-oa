"""Embedding + vector search against Qdrant, and the one-time seed.

The qdrant client is auto-instrumented by OpenLLMetry, so the search itself
becomes a span without any code here. What OpenLLMetry does *not* give you is
whether the search was any good — so search() returns the scores and app.py puts
them on the span and into a metric. A retrieval that takes 9ms and returns
garbage looks identical to a healthy one until you record the score.
"""
import logging

# qdrant-client: the official Python client for the Qdrant vector store
# (https://github.com/qdrant/qdrant-client). Runs as its own pod in this stack.
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

import kb
import llm
from config import Config

log = logging.getLogger("o11yag.retrieval")

_qdrant = QdrantClient(url=Config.QDRANT_URL, timeout=30)


def ensure_seeded():
    """Create and fill the collection if it isn't there.

    Idempotent by design: every gunicorn worker runs this on boot, and so does
    every pod restart. Two workers racing on first boot would both try to create
    the collection; the loser's error is caught and the data is identical either
    way.
    """
    try:
        if _qdrant.collection_exists(Config.COLLECTION):
            return
        log.info("seeding collection %s", Config.COLLECTION)
        _qdrant.create_collection(
            collection_name=Config.COLLECTION,
            vectors_config=VectorParams(size=Config.EMBED_DIM, distance=Distance.COSINE),
        )
        points = [
            PointStruct(id=i, vector=llm.embed(text), payload={"doc_id": doc_id, "text": text})
            for i, (doc_id, text) in enumerate(kb.DOCS)
        ]
        _qdrant.upsert(collection_name=Config.COLLECTION, points=points)
        log.info("seeded %d documents", len(points))
    except Exception as exc:  # noqa: BLE001
        log.warning("seed skipped or failed (may be a benign race): %s", exc)


def search(query: str, k: int):
    """Return [(doc_id, score, text), ...], best first."""
    hits = _qdrant.query_points(
        collection_name=Config.COLLECTION, query=llm.embed(query), limit=k, with_payload=True
    ).points
    return [(h.payload.get("doc_id", str(h.id)), float(h.score), h.payload.get("text", ""))
            for h in hits]
