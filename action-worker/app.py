"""o11yag action worker — the tool-using agent.

Runs the loop the whole project exists to make visible: decide, call an MCP
tool, observe, decide again, bounded by MAX_STEPS. Consequential tools go
through the approval gate first.

Three signals here have no equivalent in a normal service and are the reason
this is not just APM with different span names. Upstream puts them on the
action_worker span; with no OTel here they only reach the /act response:

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

o11y.init(Config.SERVICE_NAME)

import approvals   # noqa: E402
import llm         # noqa: E402
import mcp_client  # noqa: E402
import planner     # noqa: E402
import security    # noqa: E402

# The catalogue this pod first saw, when nothing is pinned in config. Per
# process, deliberately: it is a weaker control than a pinned digest, and the
# audit record of a change says which one was in force (`baseline`) so the two
# are never read as the same claim. A restart forgets, which is exactly the
# limitation — a server poisoned before the pod booted looks original.
_first_seen_digest = None

SUMMARY = ("Tell the customer what you did, in two sentences, based only on the "
           "tool results below. Do not promise anything the results don't show.")


def invoke(name: str, args: dict, ticket_id: str, customer_id: str):
    """One MCP tool call, with the approval gate in front of the risky ones."""
    approved = "not_required"
    if name in Config.CONSEQUENTIAL_TOOLS:
        decision, _, _ = approvals.request_and_wait(
            name, args, ticket_id, customer_id, reason="consequential tool")
        approved = decision
        if decision != "approved":
            o11y.tool_called(name, outcome="blocked", approved=decision)
            o11y.audit("o11yag.tool.blocked", tool=name, args=args,
                       ticket_id=ticket_id, customer_id=customer_id, decision=decision)
            return {"blocked": True, "reason": f"approval_{decision}"}

    try:
        result = mcp_client.call_tool(name, args)
        outcome = "ok"
    except mcp_client.MCPUnavailable as exc:
        o11y.tool_called(name, outcome="error", approved=approved)
        return {"error": str(exc)}

    o11y.tool_called(name, outcome=outcome, approved=approved)
    o11y.audit("o11yag.tool.called", tool=name, args=args, result=result,
               ticket_id=ticket_id, customer_id=customer_id, approved=approved)
    return result


def _screen_catalogue(tools, ticket_id: str):
    """Check what the server just advertised, before any of it reaches the model.

    Two different attacks, one place to catch both:

      tool poisoning  an instruction written into a tool's description. It goes
                      into the planner's system prompt as capability
                      documentation — the part of the prompt the agent is built
                      to act on — and if it works, the resulting call is
                      well-formed, in-schema and attributed to the model.
                      `validate_args` passes it, because the arguments are
                      genuinely valid; it was the intent that was supplied by an
                      attacker, and no schema check can see that.

      rug pull        the description changing after the server earned trust.
                      Names identical, schemas identical, tool list identical.
                      Nothing in a normal trace moves, which is why the only
                      thing that catches it is a fingerprint taken over the
                      descriptions and compared to one taken earlier.

    Neither is blocked here, and that is a decision rather than an omission: a
    worker that refuses to run because a description changed cannot tell a
    deploy from an attack, and would hand anyone who can edit a description an
    outage. The controls that hold regardless are downstream — the approval gate
    does not care who asked for the refund — so this reports, redacts, and lets
    the loop continue under signals a human can act on.
    """
    digest = security.catalogue_digest(tools)

    global _first_seen_digest
    if Config.MCP_TOOLS_DIGEST:
        baseline, expected = "pinned", Config.MCP_TOOLS_DIGEST
    else:
        baseline = "first_seen"
        if _first_seen_digest is None:
            _first_seen_digest = digest
            app.logger.info("MCP tool catalogue digest %s (unpinned; "
                            "set MCP_TOOLS_DIGEST to this to pin it)", digest)
        expected = _first_seen_digest

    changed = digest != expected
    if changed:
        o11y.security_event(kind="tool_catalogue_changed", action="observed",
                            source="mcp_server")
        o11y.audit("o11yag.security.tool_catalogue_changed",
                   ticket_id=ticket_id, server="o11yag-crm", baseline=baseline,
                   digest=digest, expected=expected, tools=[t[0] for t in tools])

    detections = security.scan_tools(tools)
    if not detections:
        return tools

    redact = Config.TOOL_POISON_ACTION == "redact"
    action = "redacted" if redact else "observed"
    for d in detections:
        o11y.security_event(kind="tool_poisoning", action=action, source="mcp_server")
        o11y.audit("o11yag.security.tool_poisoned", ticket_id=ticket_id,
                   server="o11yag-crm", tool=d["tool"], kind=d["kind"],
                   match=d["match"], action=action)

    return security.redact_tools(tools, detections) if redact else tools


def act(body: dict):
    text = str(body.get("text", ""))
    ticket_id = str(body.get("ticket_id", ""))
    customer_id = str(body.get("customer_id", ""))

    tokens, llm_calls, repeated = 0, 0, 0
    history, seen = [], set()
    terminated = "done"

    try:
        tools = mcp_client.list_tools()
    except mcp_client.MCPUnavailable as exc:
        app.logger.exception("MCP server unreachable: %s", exc)
        return {"answer": "I can't reach our order system right now.",
                "outcome": "error", "tokens": 0, "llm_calls": 0}
    tools = _screen_catalogue(tools, ticket_id)

    # This loop is the agent.
    for step in range(Config.MAX_STEPS):
        # decide what to do next
        # the decision won't be executed untill later in invoke()
        decision, step_tokens, decided_by, why = planner.next_step(
            text, tools, history, customer_id)
        tokens += step_tokens
        llm_calls += 1

        if decision.get("done") or not decision.get("tool"):
            break

        name = decision["tool"]
        # A tool with no parameters may come back as {"tool": "x"} with no args
        # key at all, or as an explicit null. Both mean "call it with nothing".
        args = decision.get("args") or {}

        # The oscillation check. Identical call, identical arguments, twice: no
        # error anywhere, just wasted tokens and a longer wait for the customer.
        signature = f"{name}:{json.dumps(args, sort_keys=True)}"
        if signature in seen:
            repeated += 1
        seen.add(signature)

        # Upstream wraps this in a step_<n> span carrying decided_by and, when
        # the model's answer was not used, why. Nothing records either here.
        # execute the planned decision
        result = invoke(name, args, ticket_id, customer_id)

        history.append({"tool": name, "args": args, "result": result})
    else:
        terminated = "max_steps"

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
