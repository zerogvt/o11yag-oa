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


def next_step(text: str, tools, history, customer_id: str):
    """Return (decision_dict, tokens, decided_by)."""
    catalogue = "\n".join(f"- {name}: {desc}" for name, desc, _ in tools)
    transcript = "\n".join(
        f"called {h['tool']}({json.dumps(h['args'])}) -> {json.dumps(h['result'])[:300]}"
        for h in history
    ) or "nothing yet"

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

    known = {name for name, _, _ in tools}
    if obj and (obj.get("done") or obj.get("tool") in known):
        return obj, tokens, "model"

    decision = _fallback(text, history)
    if decision.get("tool") == "_needs_customer":
        decision = {"tool": "list_customer_orders", "args": {"customer_id": customer_id}}
    return decision, tokens, "fallback"
