"""MCP client for the CRM server, over Streamable HTTP.

TRACE CONTEXT IS INJECTED BY HAND HERE, and that is the interesting part.

The obvious approach — add opentelemetry-instrumentation-httpx and let it
propagate — does not work with this SDK. MCP 2.x makes its HTTP calls through
`httpx2`, a different package from `httpx`, so the httpx instrumentation never
sees them. The symptom would be quietly wrong rather than broken: every MCP call
would still succeed, the server would still be instrumented, and you would get
two unrelated traces per ticket with nothing to say why.

So the client below carries an event hook that writes the current W3C context
onto every outgoing request. The MCP server reads it back via its ASGI
middleware, and the tool call lands in the same trace as the ticket.

None of this is needed for a stdio MCP server — because none of it is possible
there. stdio has no headers; propagation has to go in the JSON-RPC `_meta` field
instead, which both ends must agree to implement.

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
