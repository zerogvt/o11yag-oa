"""Deciding the next tool call.

Model first, deterministic fallback second. qwen:0.5b will not reliably emit
parseable JSON tool calls, and an agent that stalls produces no trace worth
looking at — so when the model's answer can't be used, a small rule set keeps
the loop moving. The telemetry records which one decided (`step.decided_by`),
so a demo never silently passes off rules as model reasoning.

Swap in a bigger model behind the gateway and the fallback stops firing; the
fallback rate is itself a useful thing to watch.
"""
import json
import re

import llm
from config import Config

ORDER_RE = re.compile(r"\bORD-\d+\b", re.I)

SYSTEM = (
    "You are a support agent choosing one tool call at a time.\n"
    "Reply with JSON only, no prose. Either:\n"
    '  {"tool": "<name>", "args": {...}}\n'
    "or, when you have everything you need:\n"
    '  {"done": true}\n'
    "Available tools:\n"
)


def _parse(raw: str):
    """Pull the first JSON object out of a model reply."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _fallback(text: str, history):
    """Rules: find the order, then refund it if that is what was asked."""
    called = {h["tool"] for h in history}
    order_ids = ORDER_RE.findall(text)
    order_id = order_ids[0].upper() if order_ids else None
    low = text.lower()

    if order_id and "lookup_order" not in called:
        return {"tool": "lookup_order", "args": {"order_id": order_id}}

    if order_id and "refund" in low and "issue_refund" not in called:
        # Refund the order total the lookup reported, never an amount the
        # customer's own message asserted — the message is untrusted input.
        total = None
        for h in history:
            if h["tool"] == "lookup_order" and isinstance(h.get("result"), dict):
                total = h["result"].get("total_eur")
        if total:
            return {"tool": "issue_refund",
                    "args": {"order_id": order_id, "amount_eur": float(total),
                             "reason": "customer request"}}

    if not order_id and "list_customer_orders" not in called:
        return {"tool": "_needs_customer"}     # handled by the caller

    return {"done": True}


def _schema_for(name: str, tools):
    for tool_name, _, tool_schema in tools:
        if tool_name == name:
            return tool_schema or {}
    return {}


def validate_args(name: str, args, tools):
    """Check the model's proposed arguments against the tool's own JSON schema.

    Returns None when they are usable, otherwise a short reason string that ends
    up on the span as `step.fallback_reason`.

    This exists because a model's answer can be perfectly well-formed and still
    nonsense. qwen:0.5b routinely proposes lookup_order({"id": "C-7"}) — valid
    JSON, real tool name, but `id` is not a parameter and C-7 is a customer, not
    an order. Accepting it produced a tool error and a wasted round trip. The MCP
    server already advertises the parameter names in `input_schema`, so the
    cheapest guard is to hold the model to the contract the server published.
    """
    schema = _schema_for(name, tools)
    properties = schema.get("properties") or {}
    if not isinstance(args, dict):
        return "args_not_object"
    if not properties:
        return None          # the server advertised nothing to check against

    required = schema.get("required") or []
    missing = []
    for field in required:
        if field not in args:
            missing.append(field)
    if missing:
        return "missing:" + ",".join(sorted(missing))

    unknown = []
    for field in args:
        if field not in properties:
            unknown.append(field)
    if unknown:
        return "unknown:" + ",".join(sorted(unknown))

    return None


def _rules_decision(text: str, history, customer_id: str):
    decision = _fallback(text, history)
    if decision.get("tool") == "_needs_customer":
        decision = {"tool": "list_customer_orders", "args": {"customer_id": customer_id}}
    return decision


def next_step(text: str, tools, history, customer_id: str):
    """Return (decision_dict, tokens, decided_by, fallback_reason).

    `fallback_reason` is None when the model's own answer was used, and otherwise
    says why it was rejected — so a trace shows not just that the rules decided,
    but what the model got wrong.
    """
    if Config.PLANNER_MODE == "rules":
        # The model is not consulted at all. Deterministic, and honest about it.
        return _rules_decision(text, history, customer_id), 0, "fallback", "mode:rules"

    catalogue = "\n".join(f"- {name}: {desc}" for name, desc, _ in tools)
    steps = []
    for h in history:
        steps.append(f"called {h['tool']}({json.dumps(h['args'])}) -> {json.dumps(h['result'])[:300]}")
    transcript = "\n".join(steps) or "nothing yet"

    tokens = 0
    try:
        r = llm.chat([
            {"role": "system", "content": SYSTEM + catalogue},
            {"role": "user",
             "content": f"Customer message: {text}\nCustomer id: {customer_id}\n"
                        f"Steps so far:\n{transcript}\n\nNext step as JSON:"},
        ])
        tokens = r.usage.total_tokens if r.usage else 0
        obj = _parse(r.choices[0].message.content or "")
    except Exception:
        obj = None

    known = set()
    for name, _, _ in tools:
        known.add(name)

    reason = None
    if not obj:
        reason = "unparseable"
    elif obj.get("done"):
        return obj, tokens, "model", None
    elif obj.get("tool") not in known:
        reason = "unknown_tool"
    else:
        reason = validate_args(obj["tool"], obj.get("args") or {}, tools)

    if reason is None:
        return obj, tokens, "model", None
    return _rules_decision(text, history, customer_id), tokens, "fallback", reason
