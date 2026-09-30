"""Configuration for the orchestrator (supervisor agent).

Everything comes from environment variables; see .env.example.
"""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "o11yag_orchestrator")
    ENV = os.getenv("ENV", "local")
    PORT = int(os.getenv("PORT", "8000"))

    # --- model gateway (LiteLLM), never Ollama directly ---
    LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000/v1")
    LITELLM_KEY = os.getenv("LITELLM_KEY", "not-needed-locally")
    CHAT_MODEL = os.getenv("CHAT_MODEL", "support-chat")

    # --- workers ---
    KNOWLEDGE_URL = os.getenv("KNOWLEDGE_URL", "http://o11yag-knowledge-worker:8001")
    ACTION_URL = os.getenv("ACTION_URL", "http://o11yag-action-worker:8002")
    WORKER_TIMEOUT_S = float(os.getenv("WORKER_TIMEOUT_S", "300"))

    # --- telemetry ---

    # A local model costs nothing, which makes cost-per-ticket a column of
    # zeroes and the whole cost-attribution story unprovable. So we price the
    # tokens as if the gateway were routing to a hosted model. The number is a
    # stand-in, not a measurement — it is here to show that the *attribution*
    # works, and it should be replaced by the gateway's own reported cost the
    # moment a real provider is behind LiteLLM.
    COST_PER_1K_TOKENS_USD = float(os.getenv("COST_PER_1K_TOKENS_USD", "0.0002"))

    # Audit record content. Off means the record keeps every field except the
    # customer's words. Upstream's o11y.py explains why content goes to the audit
    # record rather than onto spans.
    AUDIT_LOG_CONTENT = _bool("AUDIT_LOG_CONTENT", True)
    AUDIT_MAX_TEXT_CHARS = int(os.getenv("AUDIT_MAX_TEXT_CHARS", "2000"))
