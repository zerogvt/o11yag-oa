"""Configuration for the load generator."""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://o11yag-orchestrator:8000")
    INTERVAL_S = float(os.getenv("INTERVAL_S", "20"))
    TIMEOUT_S = float(os.getenv("TIMEOUT_S", "420"))
    TENANT = os.getenv("TENANT", "acme")
    LOG_RESPONSES = _bool("LOG_RESPONSES", True)
