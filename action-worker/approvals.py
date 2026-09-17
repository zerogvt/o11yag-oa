"""Asking the approval gate and waiting for a person.

The wait gets its own span and its own metric. Left inside the tool-call span it
would make every latency percentile in the stack meaningless — a reviewer who
goes to lunch is not a slow tool.
"""
import time

import requests

import o11y
from config import Config


def request_and_wait(tool: str, args: dict, ticket_id: str, customer_id: str, reason: str):
    """Return (decision, approval_id, waited_ms). decision ∈ approved|denied|timeout|error."""
    from opentelemetry import trace

    tracer = trace.get_tracer("o11yag")
    with tracer.start_as_current_span("approval_wait") as span:
        span.set_attribute("approval.tool", tool)
        span.set_attribute("approval.ticket_id", ticket_id)
        start = time.monotonic()
        try:
            created = requests.post(
                f"{Config.APPROVALS_URL}/request",
                json={"tool": tool, "args": args, "ticket_id": ticket_id,
                      "customer_id": customer_id, "reason": reason},
                timeout=15,
            )
            created.raise_for_status()
            approval_id = created.json()["approval_id"]
            span.set_attribute("approval.id", approval_id)
        except Exception as exc:
            span.set_attribute("approval.error", str(exc))
            # Fail CLOSED. Everywhere else in this stack a telemetry failure is
            # swallowed; an approval failure is not telemetry. If we cannot
            # establish that someone said yes, the answer is no.
            return "error", None, (time.monotonic() - start) * 1000.0

        decision = "timeout"
        while time.monotonic() - start < Config.APPROVAL_TIMEOUT_S:
            time.sleep(Config.APPROVAL_POLL_S)
            try:
                status = requests.get(
                    f"{Config.APPROVALS_URL}/status/{approval_id}", timeout=15).json()
            except Exception:
                continue
            if status.get("status") in ("approved", "denied"):
                decision = status["status"]
                span.set_attribute("approval.decided_by", status.get("decided_by", ""))
                break

        waited_ms = (time.monotonic() - start) * 1000.0
        span.set_attribute("approval.decision", decision)
        span.set_attribute("approval.waited_ms", round(waited_ms, 1))
        o11y.approval_waited(tool=tool, decision=decision, ms=waited_ms)
        return decision, approval_id, waited_ms
