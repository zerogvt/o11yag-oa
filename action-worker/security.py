"""Detecting instructions hiding in things the agent is about to trust.

Copied verbatim into every service that consumes untrusted content, the same way
o11y.py is — each Docker build context is its own directory. Two consumers today:
the knowledge worker screens retrieved documents, the action worker screens the
MCP tool catalogue. They are one file because they are one question asked of two
inputs — "did something that should only supply data just supply an instruction"
— and because a pattern list that exists twice is a pattern list that diverges.

INDIRECT PROMPT INJECTION IS A RETRIEVAL PROBLEM BEFORE IT IS A MODEL PROBLEM.

The direct kind — a customer typing "ignore your instructions" — is the one
everybody pictures and the least interesting, because the text arrives labelled
as untrusted and a support agent ignoring a rude customer is the normal case.
The one that works is indirect: the instruction is written into a document, the
document is retrieved on its merits by a similarity search doing exactly its
job, and it reaches the model inside the context block — the part of the prompt
the model has been told to trust. Nobody typed it during the ticket that fires
it. It can be planted months earlier, by whoever can write to the corpus: a
scraped vendor page, a wiki, a support macro, an uploaded PDF.

What makes it an observability problem rather than only a security one is that
the stack cannot see it happen. Retrieval is healthy — `rag.top_score` is high,
because the poisoned document really is a good match. No span errors, no failed
call, nothing in RED. The whole trace reads as a normal ticket in which the agent
said something nobody asked it to say.

So the detection has to sit between retrieval and the prompt, which is the only
place the poisoned text is identifiable as *retrieved* rather than *said*.

WHAT THIS PATH CAN AND CANNOT DO IN THIS STACK, because the demo document asks
for a refund and will not get one. Retrieval belongs to the knowledge worker, and
the knowledge worker has no tools — it retrieves and writes prose. The agent that
holds the tools never retrieves. So an injection planted in this corpus can
corrupt an *answer* and nothing further; it cannot reach `issue_refund`.

The general attack does end in a tool call, and in an architecture where one
agent both retrieves and acts it would here too — that is the version worth
fearing, and it is a straight line from what this demonstrates. But the line is
not drawn in this repo, and a demo that implies otherwise is doing the thing this
project exists to argue against. For an injection that really does change what
the agent *does*, see the tool-catalogue half below: there the text lands in the
planner's prompt, and that agent has tools.

THE TOOL CATALOGUE IS THE SAME PROBLEM WITH A SHORTER PATH. An MCP tool
description is attacker-controlled text that goes straight into the planner's
system prompt, and the agent is told to act on it. Tool poisoning is an
injection written into a description; a rug pull is the server serving a benign
description until it is trusted and a different one afterwards. Neither is
visible in any signal this stack had before: the tool list is identical, the
call succeeds, and `validate_args` passes it because the arguments really do fit
the advertised schema. Hence the digest below — the only way to notice that what
the agent was told a tool does has changed under it.

BE HONEST ABOUT THE DETECTOR. It is a pattern match over English imperatives. It
will miss an injection written in another language, split across two documents,
base64'd, or simply phrased in a way not listed below. Treat it as a tripwire
that tells you the corpus has been written to by someone who should not have
written to it — a single hit is a serious finding about the *pipeline*, and the
right response is to go and look at how that document got in, not to tune the
regex. The durable controls are upstream (who can write to the corpus, and what
is re-verified on ingest) and downstream (the approval gate, which does not care
who asked for the refund).
"""
import hashlib
import json
import re

# Grouped by what the instruction is trying to do, because the kind of attempt
# is more useful in an alert than the phrase that matched it.
PATTERNS = [
    ("override", r"ignore\s+(all\s+|any\s+)?(previous|prior|earlier|above)\s+(instructions?|prompts?|rules?)"),
    ("override", r"disregard\s+(the\s+)?(above|previous|prior|system)"),
    ("override", r"\b(new|updated|revised)\s+instructions?\b"),
    ("override", r"\bsystem\s+prompt\b"),
    ("impersonate", r"\byou\s+are\s+now\b"),
    ("impersonate", r"\bas\s+an?\s+(ai|assistant|agent)\b,?\s+you\s+must\b"),
    ("tool_use", r"\b(call|invoke|execute|run|use)\s+the\s+\w+\s+tool\b"),
    ("tool_use", r"\b(issue_refund|list_customer_orders|lookup_order|update_ticket)\b"),
    ("tool_use", r"\balways\s+(refund|approve|cancel)\b"),
    ("conceal", r"\bdo\s+not\s+(tell|mention|inform|report)\b"),
    ("conceal", r"\bwithout\s+(telling|informing|asking)\s+(the\s+)?(user|customer|human|anyone)\b"),
    ("exfiltrate", r"\b(send|post|forward|upload)\s+[^.]{0,40}\bto\s+https?://"),
]

_COMPILED = [(kind, re.compile(rx, re.I)) for kind, rx in PATTERNS]


def scan(text: str):
    """Return the first (kind, matched_text) found, or None.

    First rather than all: one hit already means the document is quarantined and
    a person has to look at the corpus, and a list of every phrase that matched
    makes a worse span attribute than the one that decided it.
    """
    for kind, rx in _COMPILED:
        m = rx.search(text or "")
        if m:
            return kind, m.group(0)[:120]
    return None


def screen_hits(hits):
    """Split retrieved hits into (clean, detections).

    `hits` is [(doc_id, score, text), ...] as retrieval.search returns it.
    `detections` is [{"doc_id", "kind", "match", "score"}, ...].

    The caller builds the model's context from `clean` only. Dropping the
    document is the whole control: a detector that logs the finding and hands
    the text to the model anyway has recorded an attack it also carried out.
    """
    clean, detections = [], []
    for doc_id, score, text in hits:
        found = scan(text)
        if found:
            kind, match = found
            detections.append({"doc_id": doc_id, "kind": kind,
                               "match": match, "score": round(score, 4)})
        else:
            clean.append((doc_id, score, text))
    return clean, detections


# --------------------------------------------------------------------------
# The tool catalogue
# --------------------------------------------------------------------------
REDACTED = "[description withheld: flagged by tool-description screening]"


def catalogue_digest(tools) -> str:
    """A stable fingerprint of what the server advertised.

    Covers descriptions and schemas, not just names, because a rug pull changes
    exactly those and leaves the name list untouched. Sorted by name so the
    server's iteration order is not mistaken for a change.

    `tools` is [(name, description, input_schema), ...] as mcp_client returns it.
    """
    canonical = json.dumps(
        sorted(([name, desc or "", schema or {}] for name, desc, schema in tools),
               key=lambda t: t[0]),
        sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def scan_tools(tools):
    """Return [{"tool", "kind", "match"}, ...] for descriptions carrying instructions.

    The schema travels with the description because parameter descriptions are
    prompt text too, and are the quieter place to hide an instruction.
    """
    out = []
    for name, desc, schema in tools:
        blob = (desc or "") + " " + json.dumps(schema or {})
        found = scan(blob)
        if found:
            kind, match = found
            out.append({"tool": name, "kind": kind, "match": match})
    return out


def redact_tools(tools, detections):
    """Replace flagged descriptions, keeping the tools themselves callable.

    Dropping the tool would be the stricter move and the wrong one: an attacker
    who can write a description could then disable any tool they liked by
    poisoning it, which turns the control into the denial of service. The
    description is what reaches the model, so the description is what is removed.
    """
    flagged = {d["tool"] for d in detections}
    return [(name, REDACTED if name in flagged else desc, schema)
            for name, desc, schema in tools]
