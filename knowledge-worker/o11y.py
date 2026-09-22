"""OpenLLMetry wiring, shared verbatim by every instrumented service.

Deliberately one file copied per service (each Docker build context is its own
directory), mirroring how observAI duplicates tracing.py.

Three pipelines, set up separately on purpose:

  traces   OpenLLMetry / Traceloop. It auto-instruments the OpenAI client and
           the Qdrant client, so LLM and vector calls become spans with model,
           token and score attributes without us writing any of it. The
           @workflow/@agent/@task/@tool decorators in the services are also its.
           (traceloop-sdk — https://github.com/traceloop/openllmetry)
  metrics  plain OpenTelemetry, ours. Traceloop's spans answer "what happened in
           this request"; these answer "what does a resolved ticket cost, and is
           that changing" — the business roll-up nothing emits for you.
  logs     plain OpenTelemetry, ours. Carries the audit records: conversation
           text, tool arguments, approval decisions.

The split between spans and logs is the same call observAI made and is worth
restating, because OpenLLMetry's default is the opposite: TRACELOOP_TRACE_CONTENT
defaults to capturing prompts and completions onto spans. In a customer-support
flow that is customer PII landing in a trace store, sampled and retained under
trace rules. So the ConfigMaps set it to false and the content goes to the audit
sink, which can be routed to a bucket with its own retention.
"""
import json
import logging
import os
import time
from contextlib import contextmanager

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

log = logging.getLogger("o11yag")

_METRICS = {}
_audit_log = logging.getLogger("o11yag.audit")

AUDIT_SCHEMA_VERSION = "1"


def init(service_name: str, endpoint: str, enabled: bool = True, flask_app=None):
    """Bring up all three pipelines. Safe to call once per process."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not enabled:
        log.info("telemetry disabled (OTEL_ENABLED=false)")
        return

    resource = Resource.create({"service.name": service_name})

    # ORDER MATTERS. OpenTelemetry's set_meter_provider refuses to replace a
    # provider that is already set: it logs a warning and keeps the incumbent.
    # Traceloop installs its own (traceloop/sdk/metrics/metrics.py calls
    # set_meter_provider), so if it ran first our o11yag.* business metrics
    # would be built against its provider instead of the one configured here.
    # Ours therefore go in first and Traceloop finds them already present.
    #
    # Expect a benign "Overriding of current MeterProvider is not allowed"
    # warning in the logs afterwards. That warning is this working as intended.

    # --- metrics: ours ---
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
        export_interval_millis=10_000,
    )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    # --- logs: ours, for the audit records ---
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    set_logger_provider(provider)
    _audit_log.addHandler(LoggingHandler(level=logging.INFO, logger_provider=provider))
    _audit_log.setLevel(logging.INFO)

    # --- traces: OpenLLMetry ---
    # Imported here rather than at module top so that OTEL_ENABLED=false gives a
    # working service even if the SDK is missing or misconfigured.
    from traceloop.sdk import Traceloop

    Traceloop.init(
        app_name=service_name,
        api_endpoint=endpoint,
        disable_batch=False,
    )

    # --- HTTP instrumentation ---
    # Traceloop covers LLM and vector clients, not HTTP. Without these the trace
    # breaks at every service hop and orchestrator -> worker becomes two
    # unrelated traces.
    #
    # Be precise about what each one buys, because this stack is mostly NOT on
    # httpx any more:
    #   requests  -> the service-to-service hops (worker calls, approval polls)
    #   httpx     -> qdrant-client only. It does NOT cover the OpenAI SDK (3.x
    #                moved to httpx2) and it does NOT cover the MCP hop (the MCP
    #                SDK is also httpx2). MCP context is injected by hand in
    #                action-worker/mcp_client.py.
    #   flask     -> the inbound server span
    #
    # Each is attempted separately and a failure is logged, not raised: a
    # missing optional instrumentation should cost telemetry, not the pod.
    def _try(label, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("instrumentation unavailable (%s): %s", label, exc)

    def _requests():
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        RequestsInstrumentor().instrument()

    def _httpx():
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()

    def _flask():
        from opentelemetry.instrumentation.flask import FlaskInstrumentor
        FlaskInstrumentor().instrument_app(flask_app)

    _try("requests", _requests)
    _try("httpx", _httpx)
    if flask_app is not None:
        _try("flask", _flask)

    log.info("telemetry up: service=%s endpoint=%s", service_name, endpoint)


# --------------------------------------------------------------------------
# Business metrics.
#
# Metric KEYS are product-scoped (o11yag.*) while span attributes follow the
# gen_ai.* semantic conventions Traceloop emits. Same split observAI used, same
# reason: renaming a metric key orphans its history, renaming a span attribute
# only affects queries from that point on.
# --------------------------------------------------------------------------
def _meter():
    return metrics.get_meter("o11yag")


def _instrument(kind: str, name: str, unit: str, desc: str):
    if name not in _METRICS:
        make = {"counter": _meter().create_counter, "histogram": _meter().create_histogram}[kind]
        _METRICS[name] = make(name, unit=unit, description=desc)
    return _METRICS[name]


def task_finished(intent: str, tenant: str, outcome: str, ms: float,
                  llm_calls: int, tokens: int, cost_usd: float):
    """The roll-up at the end of a ticket.

    This is the gap OpenLLMetry does not close: it reports per-call cost and
    latency, but nobody budgets per LLM call. They budget per resolved ticket.
    `llm_calls` is here for the same reason — it is the loop-depth signal, and
    because agent work is non-deterministic it is the series you point anomaly
    detection at when there is no fixed call graph to alert on.
    """
    dims = {"intent": intent, "tenant": tenant}
    _instrument("counter", "o11yag.tasks", "1", "Tickets handled").add(
        1, {**dims, "outcome": outcome})
    _instrument("histogram", "o11yag.task.latency", "ms", "Wall clock per ticket").record(ms, dims)
    _instrument("histogram", "o11yag.task.llm_calls", "1", "LLM calls per ticket").record(llm_calls, dims)
    _instrument("counter", "o11yag.task.tokens", "1", "Tokens per ticket").add(tokens, dims)
    _instrument("counter", "o11yag.task.cost.usd", "usd", "Cost per ticket").add(cost_usd, dims)


def approval_waited(tool: str, decision: str, ms: float):
    """Time a human spent deciding.

    Kept as its own series because it otherwise hides inside task latency and
    makes every percentile meaningless — a 40-minute think swamps a 3-second
    agent. Subtract this from o11yag.task.latency to get machine time.
    """
    _instrument("histogram", "o11yag.approval.wait", "ms", "Human decision time").record(
        ms, {"tool": tool, "decision": decision})


def tool_called(tool: str, outcome: str, approved: str):
    _instrument("counter", "o11yag.tool.calls", "1", "MCP tool calls").add(
        1, {"tool": tool, "outcome": outcome, "approved": approved})


def retrieval_scored(collection: str, top_score: float, hits: int):
    """Retrieval quality, not just retrieval latency.

    The vector query taking 12ms is not the interesting fact; that it returned
    nothing relevant is, because that is why the answer was wrong.
    """
    _instrument("histogram", "o11yag.retrieval.top_score", "1", "Best similarity score").record(
        top_score, {"collection": collection})
    _instrument("histogram", "o11yag.retrieval.hits", "1", "Chunks returned").record(
        hits, {"collection": collection})


def answer_judged(verdict: str, decided_by: str):
    """The quality verdict on an answer that was actually given.

    This is the series that closes the half of gap 1 the retrieval floor cannot:
    `o11yag.retrieval.top_score` tells you the model was handed the right
    document, and says nothing about whether the answer it then wrote is
    supported by it. A confidently wrong answer off a correct extract scores
    perfectly on every other signal in this stack.

    `decided_by` is not decoration. A judge is itself a model, and on a small
    local one its verdict is worth little — so the deterministic check is a
    first-class decider rather than a fallback nobody can see. Splitting the
    series by who decided keeps an honest judge and a rubber stamp apart in the
    same chart, and makes "how often does the judge model fail to produce a
    usable verdict" a question you can answer.
    """
    _instrument("counter", "o11yag.answer.quality", "1", "Answers graded for groundedness").add(
        1, {"verdict": verdict, "decided_by": decided_by})


def feedback_received(rating: str, intent: str, tenant: str):
    """A human's verdict on an answer, after the fact.

    The judge is an opinion the stack holds about itself. This is the only
    signal here that comes from outside it, which makes it the one that can
    contradict the rest — a stream of thumbs-down against a healthy
    `o11yag.answer.quality` is the judge being wrong, and there is no internal
    signal that could have told you.

    Arrives on its own trace, long after the ticket closed. `o11yag.feedback`
    is therefore joined to the ticket by id, never by trace context; the audit
    record carries the graded trace id so a pivot is still one query.
    """
    _instrument("counter", "o11yag.feedback", "1", "Human verdicts on an answer").add(
        1, {"rating": rating, "intent": intent, "tenant": tenant})


def security_event(kind: str, action: str, source: str):
    """Adversarial content found in something the agent was about to trust.

    Deliberately one series for the whole security act rather than one per
    attack: the useful alert is "the agent was fed an instruction by something
    that should only have supplied data", and the kind dimension says which
    door it came through (`prompt_injection`, `tool_poisoning`,
    `tool_catalogue_changed`).

    `action` is what was *done* about it — `blocked`, `quarantined`,
    `observed`. A detector that only observes is worth having and worth being
    honest about, and the two cases must not be summed into one number.
    """
    _instrument("counter", "o11yag.security.events", "1",
                "Adversarial content detected in agent input").add(
        1, {"kind": kind, "action": action, "source": source})


# --------------------------------------------------------------------------
# Audit records
# --------------------------------------------------------------------------
def audit(event_type: str, **fields):
    """Write one audit record: an OTLP log, plus stdout for kubectl-logs debugging.

    Fail-open, like observAI's sink — a broken audit path must not turn a good
    answer into an error. The failure surfaces as `o11yag.audit.sink_error` on
    the active span instead. If your posture is the opposite (no record, no
    answer), raise from here.
    """
    try:
        span = trace.get_current_span()
        ctx = span.get_span_context()
        attrs = {
            "audit.event.type": event_type,
            "audit.schema.version": AUDIT_SCHEMA_VERSION,
            "audit.trace_id": format(ctx.trace_id, "032x") if ctx.trace_id else "",
            "audit.span_id": format(ctx.span_id, "016x") if ctx.span_id else "",
        }
        for k, v in fields.items():
            if v is None:
                continue
            attrs[f"audit.{k}"] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v)
        _audit_log.info(event_type, extra=attrs)
    except Exception as exc:  # noqa: BLE001 - deliberately swallowed, see docstring
        log.warning("audit sink failed: %s", exc)
        try:
            trace.get_current_span().set_attribute("o11yag.audit.sink_error", str(exc))
        except Exception:
            pass


@contextmanager
def timed():
    """Yield a callable returning elapsed milliseconds."""
    start = time.monotonic()
    yield lambda: (time.monotonic() - start) * 1000.0
