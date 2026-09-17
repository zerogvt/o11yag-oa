"""Configuration for the knowledge worker (RAG over the support knowledge base)."""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "o11yag_knowledge_worker")
    ENV = os.getenv("ENV", "local")
    PORT = int(os.getenv("PORT", "8001"))

    LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000/v1")
    LITELLM_KEY = os.getenv("LITELLM_KEY", "not-needed-locally")
    CHAT_MODEL = os.getenv("CHAT_MODEL", "support-chat")
    EMBED_MODEL = os.getenv("EMBED_MODEL", "support-embed")

    QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
    COLLECTION = os.getenv("COLLECTION", "support_kb")
    EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))     # nomic-embed-text
    TOP_K = int(os.getenv("TOP_K", "3"))

    # Below this best-match score we treat retrieval as having failed and say so
    # rather than letting the model answer from nothing. See app.py.
    MIN_SCORE = float(os.getenv("MIN_SCORE", "0.3"))

    OTEL_ENABLED = _bool("OTEL_ENABLED", False)
    OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
