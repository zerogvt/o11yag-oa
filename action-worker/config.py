"""Configuration for the action worker (tool-using agent)."""
import os


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    SERVICE_NAME = os.getenv("SERVICE_NAME", "o11yag_action_worker")
    ENV = os.getenv("ENV", "local")
    PORT = int(os.getenv("PORT", "8002"))

    LITELLM_URL = os.getenv("LITELLM_URL", "http://litellm:4000/v1")
    LITELLM_KEY = os.getenv("LITELLM_KEY", "not-needed-locally")
    CHAT_MODEL = os.getenv("CHAT_MODEL", "support-chat")

    MCP_URL = os.getenv("MCP_URL", "http://o11yag-mcp-crm:8003/mcp")
    MCP_TIMEOUT_S = float(os.getenv("MCP_TIMEOUT_S", "60"))

    APPROVALS_URL = os.getenv("APPROVALS_URL", "http://o11yag-approvals:8004")
    APPROVAL_TIMEOUT_S = float(os.getenv("APPROVAL_TIMEOUT_S", "180"))
    APPROVAL_POLL_S = float(os.getenv("APPROVAL_POLL_S", "2"))

    # Who picks the next tool call.
    #   model  — ask the LLM, and fall back to the deterministic rules only when
    #            its answer is unusable (unparseable, unknown tool, or arguments
    #            that do not match the tool's advertised schema)
    #   rules  — skip the LLM entirely for tool selection
    #
    # "rules" exists because a small local model is a poor tool-caller, and a demo
    # that never reaches a consequential tool never exercises the approval gate.
    # It is not a better agent — it is a deterministic stand-in, and every step it
    # decides is labelled `step.decided_by=fallback` in the trace so a run can
    # never be passed off as model reasoning.
    PLANNER_MODE = os.getenv("PLANNER_MODE", "model").strip().lower()

    # The loop budget. An agent that cannot terminate is the failure mode with no
    # equivalent in a request/response service, and a cap is the only control
    # that always works — every other guard depends on noticing first.
    MAX_STEPS = int(os.getenv("MAX_STEPS", "4"))

    # Tools that change something a customer would notice. Kept here as well as
    # on the server so the gate holds even if the server's list drifts; the
    # server's copy is the one that matters, this one fails closed.
    CONSEQUENTIAL_TOOLS = set(
        os.getenv("CONSEQUENTIAL_TOOLS", "issue_refund").split(","))

    OTEL_ENABLED = _bool("OTEL_ENABLED", False)
    OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
