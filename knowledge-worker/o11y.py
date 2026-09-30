"""Observability hooks, shared verbatim by every service — OneAgent variant.

This is o11yag-oa, the copy of o11yag-otel that Dynatrace OneAgent observes
instead of OpenLLMetry/OpenTelemetry. Everything OTel is gone from this repo on
purpose: with the OTel API still being called, OneAgent's Python module can
pick those calls up as spans, and it is then unclear what OneAgent found on its
own. Traces, LLM spans and HTTP spans are therefore whatever OneAgent injects.

The function names are kept so the services read the same as upstream and
merges stay small. What each one does now:

  init              logging setup only. There is no pipeline to bring up.
  task_finished ... the business metrics. NO-OPS. Upstream emits twelve
                    o11yag.* series from these; nothing replaces them here, and
                    that loss is one of the things this repo exists to measure.
                    Their docstrings upstream say what each series was for.
  audit             one JSON line on stdout per record, for OneAgent log
                    monitoring to find. Upstream sends the same fields as OTLP
                    log attributes and puts only the event name on stdout. No
                    trace or span id: without the OTel API there is none to read.
  timed             unchanged.
"""
import json
import logging
import sys
import time
from contextlib import contextmanager

log = logging.getLogger("o11yag")

_audit_log = logging.getLogger("o11yag.audit")

AUDIT_SCHEMA_VERSION = "1"


def init(service_name: str):
    """Set up logging. Safe to call once per process."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # The audit records get a handler of their own that writes the bare JSON,
    # so each stdout line parses as one object. They must not also go through
    # the root handler, or every record appears twice, once with a prefix.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _audit_log.addHandler(handler)
    _audit_log.setLevel(logging.INFO)
    _audit_log.propagate = False

    log.info("telemetry: none in-process, observed by OneAgent (service=%s)", service_name)


# --------------------------------------------------------------------------
# Business metrics — no-ops, see the module docstring.
# --------------------------------------------------------------------------
def task_finished(intent: str, tenant: str, outcome: str, ms: float,
                  llm_calls: int, tokens: int, cost_usd: float):
    pass


def approval_waited(tool: str, decision: str, ms: float):
    pass


def tool_called(tool: str, outcome: str, approved: str):
    pass


def retrieval_scored(collection: str, top_score: float, hits: int):
    pass


def answer_judged(verdict: str, decided_by: str):
    pass


def feedback_received(rating: str, intent: str, tenant: str):
    pass


def security_event(kind: str, action: str, source: str):
    pass


# --------------------------------------------------------------------------
# Audit records
# --------------------------------------------------------------------------
def audit(event_type: str, **fields):
    """Write one audit record as a JSON line on stdout.

    Fail-open, as upstream: a broken audit path must not turn a good answer into
    an error. Upstream also flags the failure on the active span; there is no
    span to flag here, so the warning in the log is all that is left of it.
    """
    try:
        record = {
            "audit.event.type": event_type,
            "audit.schema.version": AUDIT_SCHEMA_VERSION,
        }
        for k, v in fields.items():
            if v is None:
                continue
            record[f"audit.{k}"] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)
        _audit_log.info(json.dumps(record))
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("audit sink failed: %s", exc)


@contextmanager
def timed():
    """Yield a callable returning elapsed milliseconds."""
    start = time.monotonic()
    yield lambda: (time.monotonic() - start) * 1000.0
