"""MCP client for the CRM server, over Streamable HTTP.

TRACE CONTEXT. Upstream carries it across this hop by two routes: the MCP SDK's
own OTel instrumentation writes it into each JSON-RPC request's `_meta` field,
and an httpx2 event hook here writes a `traceparent` header for the server's
ASGI middleware. The hook and the middleware were OTel code and are gone. The
SDK's own instrumentation is not ours to remove: it ships inside `mcp` and calls
the OTel API, which stays installed as a dependency of `mcp`. With no OTel SDK
configured those calls record nothing — unless OneAgent picks them up, which is
one of the questions this repo exists to answer.

Whether the MCP hop still appears as one trace is therefore down to OneAgent
seeing httpx2 on this side and uvicorn on the server.

SIMPLIFICATION: a session is opened and torn down per call. A real agent holds
one session for a whole conversation, so this adds an initialize round trip to
every tool call and inflates the tool latency. It is done this way because
Flask is synchronous and the MCP SDK is async — keeping a session alive across
requests means an event loop with a lifetime of its own.
"""
import asyncio
import json

# httpx2 — the HTTP client the MCP SDK uses internally. Pulled in as a direct
# dependency here only so we can hand the transport a client with our timeouts.
import httpx2
# mcp — official Model Context Protocol Python SDK (2.x).
# https://github.com/modelcontextprotocol/python-sdk
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from config import Config


def _http_client() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        timeout=httpx2.Timeout(Config.MCP_TIMEOUT_S, read=Config.MCP_TIMEOUT_S),
    )


class MCPUnavailable(Exception):
    """The MCP server could not be reached: refused, timed out, or answered 5xx."""


async def _with_session(fn):
    """Run fn inside a fresh session, with transport failures made catchable.

    The SDK runs the transport in an anyio TaskGroup, so a refused connection
    does not arrive as httpx2.ConnectError but as an ExceptionGroup wrapping it.
    `except httpx2.ConnectError` never matches that. Both shapes become
    MCPUnavailable here; anything else in a group (a bug, a protocol error) is
    re-raised untouched, so it is not mistaken for an outage.
    """
    client = _http_client()
    try:
        async with streamable_http_client(Config.MCP_URL, http_client=client) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)
    except httpx2.HTTPError as exc:
        raise MCPUnavailable(str(exc)) from exc
    except ExceptionGroup as group:
        transport, rest = group.split(httpx2.HTTPError)
        if transport is None or rest is not None:
            raise
        raise MCPUnavailable(str(transport.exceptions[0])) from group
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
