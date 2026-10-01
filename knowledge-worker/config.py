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

    # --- gap 1: the quality signal (see judge.py) ---
    #   off        no grading. The retrieval floor above is the only guard, and
    #              an answer grounded in the right document is never checked.
    #   heuristic  deterministic checks only. Free, and catches the invented
    #              figure, which is the failure that costs money.
    #   model      the deterministic checks, then a judge model for whatever
    #              they pass.
    JUDGE_MODE = os.getenv("JUDGE_MODE", "model").strip().lower()

    # A gateway alias of its own rather than CHAT_MODEL, for two reasons: a judge
    # sharing the graded model's weights shares its blind spots, and this is the
    # one place in the stack where swapping in something better is most worth
    # doing. Both aliases point at qwen:0.5b today — see litellm's ConfigMap.
    JUDGE_MODEL = os.getenv("JUDGE_MODEL", "support-judge")
    JUDGE_MAX_TOKENS = int(os.getenv("JUDGE_MAX_TOKENS", "60"))

    # Fraction of the answer's content words that must also appear in the
    # extracts. The crude check, and the one that produces false positives — tune
    # it against o11yag.answer.quality split by decided_by=heuristic.
    JUDGE_MIN_OVERLAP = float(os.getenv("JUDGE_MIN_OVERLAP", "0.35"))

    #   observe   record the verdict and send the answer anyway (default)
    #   withhold  replace an unsupported answer with the refusal
    # Default observe because a weak judge withholding good answers is a worse
    # product than a wrong answer you can see in a dashboard. Flip it once the
    # false-positive rate on this corpus is something you have measured.
    JUDGE_ACTION = os.getenv("JUDGE_ACTION", "observe").strip().lower()

    # --- gap 2: the security act (see security.py) ---
    # Seed the indirect-prompt-injection document into the corpus. Off by
    # default: the reference stack ships a clean knowledge base.
    KB_POISON_DOC = _bool("KB_POISON_DOC", False)

    #   quarantine  drop a flagged document before it reaches the prompt
    #   observe     record the detection and build the prompt with it anyway
    # observe exists to demonstrate what the attack does when nothing stops it.
    # It is not a monitoring mode — it is the attack succeeding, on purpose.
    INJECTION_ACTION = os.getenv("INJECTION_ACTION", "quarantine").strip().lower()
