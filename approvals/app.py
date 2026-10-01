"""o11yag approval gate — human-in-the-loop before a consequential tool call.

The action worker asks here before calling anything on the CONSEQUENTIAL list,
then polls until a decision exists. Every decision becomes an audit record,
because "who approved this refund" is a record-keeping question, not a tracing
one, and it has to survive long after the trace has expired.
"""
import time

from flask import Flask, jsonify, render_template_string, request

import o11y
import store
from config import Config

app = Flask(__name__)

o11y.init(Config.SERVICE_NAME)

PAGE = """<!doctype html><meta charset=utf-8><title>o11yag approvals</title>
<style>
 body{font:14px system-ui;margin:2rem;max-width:60rem}
 table{border-collapse:collapse;width:100%}
 td,th{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;vertical-align:top}
 button{padding:.3rem .8rem;margin-right:.3rem;cursor:pointer}
 .none{color:#666}
</style>
<h1>Pending approvals</h1>
{% if not items %}<p class=none>Nothing waiting.</p>{% endif %}
{% if items %}<table>
<tr><th>Tool</th><th>Arguments</th><th>Ticket</th><th>Waiting</th><th></th></tr>
{% for a in items %}<tr>
 <td><code>{{a.tool}}</code></td>
 <td><code>{{a.args}}</code></td>
 <td>{{a.ticket_id}}<br><small>{{a.customer_id}}</small></td>
 <td>{{a.waited_s}}s</td>
 <td>
  <form method=post action="/decide/{{a.approval_id}}" style="display:inline">
   <input type=hidden name=decision value=approved><button>Approve</button></form>
  <form method=post action="/decide/{{a.approval_id}}" style="display:inline">
   <input type=hidden name=decision value=denied><button>Deny</button></form>
 </td></tr>{% endfor %}
</table>{% endif %}
<p class=none>Auto-approve is {{ 'ON' if auto else 'OFF' }}.</p>
"""


def _maybe_auto_decide(approval: dict):
    """Stand in for a reviewer, so an unattended demo doesn't deadlock.

    Evaluated lazily when the status is polled rather than by a background
    thread: fewer moving parts, and the delay still shows up as real wait time
    on the caller's span. Large refunds are excluded so one path always stops.
    """
    if not Config.AUTO_APPROVE or approval["status"] != "pending":
        return approval
    amount = approval["args"].get("amount_eur")
    if isinstance(amount, (int, float)) and amount > Config.AUTO_APPROVE_MAX_EUR:
        return approval
    if time.time() - approval["requested_at"] < Config.AUTO_APPROVE_AFTER_S:
        return approval
    return _record(approval["approval_id"], "approved", "auto-approver")


def _record(approval_id: str, decision: str, by: str):
    approval = store.decide(approval_id, decision, by)
    if approval:
        o11y.audit("o11yag.approval.decided",
                   approval_id=approval_id, tool=approval["tool"],
                   args=approval["args"], ticket_id=approval["ticket_id"],
                   customer_id=approval["customer_id"], decision=decision,
                   decided_by=by, waited_s=approval["waited_s"])
    return approval


@app.post("/request")
def request_route():
    body = request.get_json(force=True, silent=True) or {}
    approval = store.create(
        tool=str(body.get("tool", "")), args=body.get("args") or {},
        ticket_id=str(body.get("ticket_id", "")),
        customer_id=str(body.get("customer_id", "")),
        reason=str(body.get("reason", "")),
    )
    o11y.audit("o11yag.approval.requested",
               approval_id=approval["approval_id"], tool=approval["tool"],
               args=approval["args"], ticket_id=approval["ticket_id"],
               customer_id=approval["customer_id"], reason=approval["reason"])
    return jsonify(approval), 201


@app.get("/status/<approval_id>")
def status_route(approval_id):
    approval = store.get(approval_id)
    if not approval:
        return jsonify({"error": "not_found"}), 404
    return jsonify(_maybe_auto_decide(approval))


@app.post("/decide/<approval_id>")
def decide_route(approval_id):
    decision = (request.form.get("decision")
                or (request.get_json(force=True, silent=True) or {}).get("decision"))
    if decision not in ("approved", "denied"):
        return jsonify({"error": "decision must be approved or denied"}), 400
    by = request.form.get("by") or "human"
    approval = _record(approval_id, decision, by)
    if not approval:
        return jsonify({"error": "not_found"}), 404
    # Form posts come from the page; send the reviewer back to it.
    if request.form.get("decision"):
        return page_route()
    return jsonify(approval)


@app.get("/")
def page_route():
    return render_template_string(PAGE, items=store.pending(), auto=Config.AUTO_APPROVE)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": Config.SERVICE_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT)
