"""o11yag CRM — an MCP server over Streamable HTTP.

This is the tool side of the architecture: the systems of record the agent is
allowed to touch, exposed as MCP tools rather than as a bespoke HTTP API.

Two things here are the observability point of the whole service:

1. The ASGI middleware. Without it the MCP call arrives with a traceparent
   header nobody reads, and the server's work becomes a second, unrelated trace.
   With it, orchestrator -> action worker -> MCP server is one waterfall. The
   client side has to cooperate: see the long note in action-worker/mcp_client.py
   about why the trace header is injected by hand there. A stdio MCP server has
   no header at all and would need the id smuggled into the JSON-RPC _meta field.

2. issue_refund writes an audit record. A tool that changes a system of record is
   a business event, and the span alone is the wrong home for it — spans are
   sampled and expire on trace retention, and "did we refund this customer" has
   to be answerable a year later.
"""
import logging

import uvicorn
# mcp — the official Model Context Protocol Python SDK (2.x). MCPServer is its
# decorator API for defining a server; it was called FastMCP in 1.x.
# https://github.com/modelcontextprotocol/python-sdk
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import data
import o11y
from config import Config

log = logging.getLogger("o11yag.mcp_crm")

o11y.init(Config.SERVICE_NAME, Config.OTEL_EXPORTER_OTLP_ENDPOINT, enabled=Config.OTEL_ENABLED)

# host/port are not constructor arguments in MCP 2.x — uvicorn binds the ASGI
# app at the bottom of this file instead.
mcp = MCPServer("o11yag-crm")

# Tools whose effect a customer would notice. The action worker reads this same
# list to decide what needs human approval — it is stated once, here, next to
# the tools themselves, rather than duplicated in the agent's prompt where a
# model could talk its way around it.
CONSEQUENTIAL = {"issue_refund"}


@mcp.tool()
def lookup_order(order_id: str) -> dict:
    """Look up a single order by its id (for example ORD-1001)."""
    order = data.ORDERS.get(order_id)
    if not order:
        return {"found": False, "order_id": order_id}
    return {"found": True, "order_id": order_id, **order}


@mcp.tool()
def list_customer_orders(customer_id: str) -> dict:
    """List every order belonging to a customer."""
    found = [{"order_id": oid, **o} for oid, o in data.ORDERS.items()
             if o["customer_id"] == customer_id]
    return {"customer_id": customer_id, "orders": found, "count": len(found)}


@mcp.tool()
def issue_refund(order_id: str, amount_eur: float, reason: str = "") -> dict:
    """Refund an order. Moves real money and requires human approval."""
    order = data.ORDERS.get(order_id)
    if not order:
        return {"ok": False, "error": "unknown_order", "order_id": order_id}
    # The status this function sets on its way out is also a guard on the way
    # in. An agent loop can retry a step, a customer can open a second ticket
    # about the same order, and the load generator replays one ticket forever —
    # all of which reached this line and moved the money again. An approval is
    # permission to refund this order once, not a licence to refund it twice.
    if order["status"] == "refunded":
        return {"ok": False, "error": "already_refunded", "order_id": order_id,
                "customer_id": order["customer_id"]}
    if amount_eur > order["total_eur"]:
        return {"ok": False, "error": "amount_exceeds_order_total",
                "order_id": order_id, "order_total_eur": order["total_eur"]}

    data.REFUNDS.append({"order_id": order_id, "amount_eur": amount_eur, "reason": reason})
    order["status"] = "refunded"
    o11y.audit("o11yag.crm.refund_issued",
               order_id=order_id, customer_id=order["customer_id"],
               amount_eur=amount_eur, reason=reason[:500],
               order_total_eur=order["total_eur"])
    log.info("refund issued: %s %.2f EUR", order_id, amount_eur)
    return {"ok": True, "order_id": order_id, "amount_eur": amount_eur,
            "customer_id": order["customer_id"]}


@mcp.tool()
def update_ticket(ticket_id: str, status: str, note: str = "") -> dict:
    """Set the status of a support ticket and append a note."""
    ticket = data.TICKETS.setdefault(ticket_id, {"notes": []})
    ticket["status"] = status
    if note:
        ticket["notes"].append(note)
    return {"ok": True, "ticket_id": ticket_id, "status": status,
            "notes": len(ticket["notes"])}


def build_app():
    """Streamable-HTTP ASGI app, wrapped so incoming trace context is honoured.

    transport_security is passed explicitly. Leave it None and the SDK allows
    only localhost, which is fine on a laptop and returns 421 for every request
    in Kubernetes, where the Host header is the Service name. See the long note
    in config.py.
    """
    app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=Config.MCP_DNS_REBINDING_PROTECTION,
            allowed_hosts=Config.MCP_ALLOWED_HOSTS,
            allowed_origins=Config.MCP_ALLOWED_ORIGINS,
        )
    )
    log.info("allowed Host headers: %s (dns rebinding protection %s)",
             ", ".join(Config.MCP_ALLOWED_HOSTS) or "<none>",
             "on" if Config.MCP_DNS_REBINDING_PROTECTION else "OFF")
    if Config.OTEL_ENABLED:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        app.add_middleware(OpenTelemetryMiddleware)
    return app


if __name__ == "__main__":
    log.info("mcp-crm listening on :%d/mcp", Config.PORT)
    uvicorn.run(build_app(), host="0.0.0.0", port=Config.PORT, log_level="info")
