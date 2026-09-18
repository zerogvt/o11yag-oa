"""o11yag action worker — the tool-using agent.

Runs the loop the whole project exists to make visible: decide, call an MCP
tool, observe, decide again, bounded by MAX_STEPS. Consequential tools go
through the approval gate first.

Three signals here have no equivalent in a normal service and are the reason
this is not just APM with different span names:

  agent.loop.steps       how many iterations this ticket actually took. There is
                         no fixed call graph to compare against, so this series
                         *is* the baseline — point anomaly detection at it.
  agent.loop.repeated    the agent calling the same tool with the same arguments
                         twice. Nothing errors; it just costs money and time.
  agent.loop.terminated  whether the loop finished or hit its budget. "max_steps"
                         is a silent failure — the customer gets a partial answer
                         and every RED metric stays green.
"""
import json

from flask import Flask, jsonify, request

import o11y
from config import Config

app = Flask(__name__)

o11y.init(Config.SERVICE_NAME, Config.OTEL_EXPORTER_OTLP_ENDPOINT,
          enabled=Config.OTEL_ENABLED, flask_app=app)

try:
    from traceloop.sdk.decorators import agent, tool as tool_span
except ImportError:
    def agent(*a, **k):
        return lambda f: f

    tool_span = agent

from opentelemetry import trace  # noqa: E402

import approvals   # noqa: E402
import llm         # noqa: E402
import mcp_client  # noqa: E402
import planner     # noqa: E402

tracer = trace.get_tracer("o11yag")

SUMMARY = ("Tell the customer what you did, in two sentences, based only on the "
           "tool results below. Do not promise anything the results don't show.")


@tool_span(name="mcp_tool_call")
def invoke(name: str, args: dict, ticket_id: str, customer_id: str):
    """One MCP tool call, with the approval gate in front of the risky ones."""
    span = trace.get_current_span()
    span.set_attribute("mcp.server", "o11yag-crm")
    span.set_attribute("mcp.transport", "streamable_http")
    span.set_attribute("gen_ai.tool.name", name)
    span.set_attribute("gen_ai.tool.call.arguments", json.dumps(args))

    approved = "not_required"
    if name in Config.CONSEQUENTIAL_TOOLS:
        decision, approval_id, _ = approvals.request_and_wait(
            name, args, ticket_id, customer_id, reason="consequential tool")
        approved = decision
        span.set_attribute("approval.required", True)
        span.set_attribute("approval.decision", decision)
        if approval_id:
            span.set_attribute("approval.id", approval_id)
        if decision != "approved":
            o11y.tool_called(name, outcome="blocked", approved=decision)
            o11y.audit("o11yag.tool.blocked", tool=name, args=args,
                       ticket_id=ticket_id, customer_id=customer_id, decision=decision)
            return {"blocked": True, "reason": f"approval_{decision}"}

    try:
        result = mcp_client.call_tool(name, args)
        outcome = "ok"
    except Exception as exc:
        span.set_attribute("error", True)
        span.set_attribute("error.kind", type(exc).__name__)
        o11y.tool_called(name, outcome="error", approved=approved)
        return {"error": str(exc)}

    o11y.tool_called(name, outcome=outcome, approved=approved)
    o11y.audit("o11yag.tool.called", tool=name, args=args, result=result,
               ticket_id=ticket_id, customer_id=customer_id, approved=approved)
    return result


@agent(name="action_worker")
def act(body: dict):
    text = str(body.get("text", ""))
    ticket_id = str(body.get("ticket_id", ""))
    customer_id = str(body.get("customer_id", ""))

    span = trace.get_current_span()
    tokens, llm_calls, repeated = 0, 0, 0
    history, seen = [], set()
    terminated = "done"

    try:
        tools = mcp_client.list_tools()
        span.set_attribute("mcp.tools.available", [t[0] for t in tools])
    except Exception as exc:
        app.logger.exception("MCP server unreachable")
        span.set_attribute("error.kind", "mcp_unavailable")
        return {"answer": "I can't reach our order system right now.",
                "outcome": "error", "tokens": 0, "llm_calls": 0}

    for step in range(Config.MAX_STEPS):
        decision, step_tokens, decided_by, why = planner.next_step(
            text, tools, history, customer_id)
        tokens += step_tokens
        llm_calls += 1

        if decision.get("done") or not decision.get("tool"):
            break

        name, args = decision["tool"], decision.get("args") or {}

        # The oscillation check. Identical call, identical arguments, twice: no
        # error anywhere, just wasted tokens and a longer wait for the customer.
        signature = f"{name}:{json.dumps(args, sort_keys=True)}"
        if signature in seen:
            repeated += 1
        seen.add(signature)

        with tracer.start_as_current_span(f"step_{step}") as step_span:
            step_span.set_attribute("step.index", step)
            step_span.set_attribute("step.decided_by", decided_by)
            if why:
                # Why the model's answer was not used — "unparseable",
                # "unknown_tool", "missing:order_id", "unknown:id", or
                # "mode:rules". Without this the trace shows that the rules
                # decided but not what the model got wrong.
                step_span.set_attribute("step.fallback_reason", why)
            result = invoke(name, args, ticket_id, customer_id)

        history.append({"tool": name, "args": args, "result": result})
    else:
        terminated = "max_steps"

    span.set_attribute("agent.loop.steps", len(history))
    span.set_attribute("agent.loop.max_steps", Config.MAX_STEPS)
    span.set_attribute("agent.loop.repeated", repeated)
    span.set_attribute("agent.loop.terminated", terminated)

    answer, summary_tokens = _summarise(text, history)
    tokens += summary_tokens
    llm_calls += 1

    blocked = any(h["result"].get("blocked") for h in history if isinstance(h["result"], dict))
    outcome = "blocked" if blocked else ("incomplete" if terminated == "max_steps" else "ok")

    return {"answer": answer, "outcome": outcome, "tokens": tokens,
            "llm_calls": llm_calls, "steps": len(history),
            "repeated_calls": repeated, "terminated": terminated}


def _summarise(text: str, history):
    transcript = "\n".join(
        f"{h['tool']}({json.dumps(h['args'])}) -> {json.dumps(h['result'])[:300]}"
        for h in history) or "no tools were called"
    try:
        r = llm.chat([
            {"role": "system", "content": SUMMARY},
            {"role": "user", "content": f"Customer said: {text}\n\nTool results:\n{transcript}"},
        ], max_tokens=120)
        return (r.choices[0].message.content or "").strip(), (r.usage.total_tokens if r.usage else 0)
    except Exception:
        return "I've looked into it and passed the details to our team.", 0


@app.post("/act")
def act_route():
    return jsonify(act(request.get_json(force=True, silent=True) or {}))


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": Config.SERVICE_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT)
