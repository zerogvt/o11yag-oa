"""Configuration for the approval gate."""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "o11yag_approvals")
    ENV = os.getenv("ENV", "local")
    PORT = int(os.getenv("PORT", "8004"))

    REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
    TTL_S = int(os.getenv("APPROVAL_TTL_S", "3600"))

    # A simulated reviewer, so loadgen can run unattended. It is a *stand-in for*
    # a human, not a human: with it on, every approval is granted and the gate
    # proves nothing about governance. Turn it off (AUTO_APPROVE=false) to use
    # the web page at / and get a real decision — that is the honest demo, and
    # the one where the wait-time telemetry becomes interesting.
    AUTO_APPROVE = _bool("AUTO_APPROVE", True)
    AUTO_APPROVE_AFTER_S = float(os.getenv("AUTO_APPROVE_AFTER_S", "8"))
    # Refunds above this are never auto-approved, so there is always one path
    # through the gate that genuinely stops and waits for a person.
    AUTO_APPROVE_MAX_EUR = float(os.getenv("AUTO_APPROVE_MAX_EUR", "100"))

    OTEL_ENABLED = _bool("OTEL_ENABLED", False)
    OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
