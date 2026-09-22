"""MCP client for the CRM server, over Streamable HTTP.

TRACE CONTEXT TRAVELS BY TWO ROUTES HERE, and the difference between them is the
interesting part.

The MCP layer propagates itself, and costs us nothing. The SDK's dispatcher opens
a CLIENT span per outbound request — these are the `MCP send <method>` spans in
the waterfall — and injects the W3C context into that request's JSON-RPC `_meta`
field (SEP-414). The server's own OpenTelemetryMiddleware, which ships enabled,
reads it back. Because the carrier is `_meta` and not a header, this works over
stdio as well; trace continuity across MCP is not a property of the transport.

The HTTP layer underneath does not propagate itself. The obvious approach — add
opentelemetry-instrumentation-httpx and let it handle it — does nothing with this
SDK, because MCP 2.x makes its HTTP calls through `httpx2`, a different package
from `httpx`, so the httpx instrumentation never sees them. Hence the event hook
below, which writes the current W3C context onto every outgoing request for the
MCP server's ASGI middleware to pick up.

Be exact about what that second route buys, because it is narrower than it looks:
the tool call itself would land in the ticket's trace either way, over `_meta`.
The hook is what keeps the transport spans — `POST /mcp`, and the session's
`DELETE /mcp` — inside that trace instead of each rooting a trace of its own. The
symptom of dropping it is quietly wrong rather than broken: every call succeeds
and nothing reports an error.

SIMPLIFICATION: a session is opened and torn down per call. A real agent holds
one session for a whole conversation, so this adds an initialize round trip to
every tool call and inflates the tool latency you see in the trace. It is done
this way because Flask is synchronous and the MCP SDK is async — keeping a
session alive across requests means an event loop with a lifetime of its own.
"""
import asyncio
import json

# httpx2 — the HTTP client the MCP SDK uses internally. Pulled in as a direct
# dependency here only so we can hand the transport a client of our own.
import httpx2
# mcp — official Model Context Protocol Python SDK (2.x).
# https://github.com/modelcontextprotocol/python-sdk
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from opentelemetry.propagate import inject

from config import Config


def _traced_http_client() -> httpx2.AsyncClient:
    async def _inject_context(request):
        carrier = {}
        inject(carrier)          # writes traceparent (and tracestate if present)
        request.headers.update(carrier)

    return httpx2.AsyncClient(
        event_hooks={"request": [_inject_context]},
        timeout=httpx2.Timeout(Config.MCP_TIMEOUT_S, read=Config.MCP_TIMEOUT_S),
    )


async def _with_session(fn):
    client = _traced_http_client()
    try:
        async with streamable_http_client(Config.MCP_URL, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)
    finally:
        await client.aclose()


def list_tools():
    """[(name, description, input_schema), ...] as advertised by the server."""
    async def go(session):
        res = await session.list_tools()
        return [(t.name, t.description or "", t.input_schema or {}) for t in res.tools]

    return asyncio.run(_with_session(go))


def call_tool(name: str, args: dict):
    """Return the tool's result as a dict.

    Callers (planner.py in particular) read fields straight off this — the
    refund amount is taken from the order the lookup returned, not from anything
    the customer claimed — so it has to be a real dict, not a blob of text.
    Servers vary: some populate structured_content, some only return a JSON text
    block. Both are normalised here, and anything genuinely unstructured is
    wrapped in {"text": ...} so callers can still rely on getting a dict.
    """
    async def go(session):
        res = await session.call_tool(name, args)
        if getattr(res, "structured_content", None):
            return res.structured_content
        parts = [c.text for c in getattr(res, "content", []) if getattr(c, "text", None)]
        text = "\n".join(parts)
        try:
            parsed = json.loads(text)
        except ValueError:
            return {"text": text}
        return parsed if isinstance(parsed, dict) else {"result": parsed}

    return asyncio.run(_with_session(go))
