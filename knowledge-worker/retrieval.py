"""Embedding + vector search against Qdrant, and the one-time seed.

Upstream, OpenLLMetry auto-instruments the qdrant client, so the search itself
becomes a span without any code here; whether OneAgent does the same is one of
the things to measure. Neither gives you whether the search was any good — so
search() returns the scores, and upstream's app.py puts them on the span and
into a metric. Here they only reach the audit records. A retrieval that takes 9ms and returns
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
    """Create and fill the collection if it isn't there, or if the corpus grew.

    Idempotent by design: every gunicorn worker runs this on boot, and so does
    every pod restart. Two workers racing on first boot would both try to create
    the collection; the loser's error is caught and the data is identical either
    way. Point ids are the corpus index, so a re-upsert overwrites rather than
    duplicates.

    The size check is what makes KB_POISON_DOC usable. Qdrant keeps its volume
    across a redeploy, so "skip if the collection exists" would leave the
    injection demo switched on in config and absent from the data — the flag
    would appear not to work, on a stack whose whole point is that silent
    mismatches are what get you. Comparing the count is cheap and catches the
    document being added or removed in either direction.
    """
    try:
        corpus = kb.docs()
        if _qdrant.collection_exists(Config.COLLECTION):
            existing = _qdrant.count(Config.COLLECTION, exact=True).count
            if existing == len(corpus):
                return
            log.info("corpus changed (%d stored, %d configured) — reseeding",
                     existing, len(corpus))
        else:
            log.info("seeding collection %s", Config.COLLECTION)
            _qdrant.create_collection(
                collection_name=Config.COLLECTION,
                vectors_config=VectorParams(size=Config.EMBED_DIM, distance=Distance.COSINE),
            )
        points = [
            PointStruct(id=i, vector=llm.embed(text), payload={"doc_id": doc_id, "text": text})
            for i, (doc_id, text) in enumerate(corpus)
        ]
        _qdrant.upsert(collection_name=Config.COLLECTION, points=points)
        log.info("seeded %d documents", len(points))
    except Exception as exc:  # noqa: BLE001
        log.warning("seed skipped or failed (may be a benign race): %s", exc)

# find the nearest documents in Qdrant
def search(query: str, k: int):
    """Return [(doc_id, score, text), ...], best first."""
    # turn the question into a vector (llm.embed)
    hits = _qdrant.query_points(
        collection_name=Config.COLLECTION, query=llm.embed(query), limit=k, with_payload=True
    ).points
    return [(h.payload.get("doc_id", str(h.id)), float(h.score), h.payload.get("text", ""))
            for h in hits]
