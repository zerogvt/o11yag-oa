"""Grading an answer against the extracts it was supposed to be grounded on.

This is the half of gap 1 the retrieval floor in app.py cannot reach. That floor
catches a wrong answer *caused by* a bad retrieval — nothing came back, so don't
answer. It says nothing about the larger failure: the right document was
retrieved, the score was excellent, and the model still wrote something the
document does not say. Every signal in this stack looks healthy for that ticket.
`rag.top_score` is high, no span has error=true, the customer gets a confident
paragraph, and the policy it quotes does not exist.

TWO GRADERS, AND THE DETERMINISTIC ONE IS NOT THE FALLBACK.

  heuristic   free, deterministic, runs on every answer. Two checks, both aimed
              at what actually goes wrong in a policy KB rather than at
              groundedness in general:

                numbers   an amount, a deadline or a duration in the answer that
                          appears nowhere in the extracts. For a corpus made of
                          "30 days", "4.99 EUR", "24-month" this is the failure
                          mode almost verbatim: the model keeps the shape of the
                          policy and invents the figure, which is the version a
                          customer acts on and a regulator asks about.
                overlap   an answer whose content words are largely absent from
                          the extracts it cites. Crude, and it is the check that
                          will produce the false positives, which is why the
                          threshold is configuration and the verdict records
                          which check fired.

  model       a second LLM call, asked for a yes/no on whether the extracts
              support the answer. This is the one every vendor diagram draws and
              the one to be most careful about here: the judge is the same
              qwen:0.5b that could not reliably emit a tool call, so its verdict
              is worth roughly what that suggests. JUDGE_MODEL is a separate
              gateway alias precisely so a real model can be put behind it
              without touching this file.

The heuristic runs first and can settle the verdict on its own; the model is
asked only when the heuristic found nothing, and can then overrule the pass.
`quality.decided_by` records which grader produced the verdict actually stored,
so "the judge model never once disagreed" stays a visible fact rather than an
assumption. Same reasoning as `step.decided_by` in the action worker: a demo
must never be able to pass a deterministic rule off as model judgement.

WHAT THIS COSTS. One extra model call on every answered question, on the ticket's
own latency, counted into the per-ticket roll-up like any other. A judge you do
not pay for is a judge that did not run.
"""
import json
import re

import llm
from config import Config

RUBRIC = (
    "You check whether a support answer is supported by policy extracts.\n"
    "Reply with JSON only, no prose: {\"supported\": true|false, \"reason\": \"<8 words\"}\n"
    "supported=false if the answer states any fact, number or deadline the "
    "extracts do not contain, even if it sounds reasonable.\n"
    "supported=true if everything in the answer is in the extracts, or the "
    "answer declines to answer."
)

# List markers are stripped before numbers are read: an enumerated answer
# ("1. Return it within 30 days") otherwise reports "1" as an invented figure.
_LIST_MARKER_RE = re.compile(r"^\s*\d+[.)]\s+", re.M)
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_WORD_RE = re.compile(r"[a-zA-Z]{4,}")

# An answer that declines is the behaviour the answer prompt asks for when the
# extracts fall short, and it is by construction made of words the extracts do
# not contain — so the overlap check reads a correct refusal as an ungrounded
# claim. Left unhandled this is the judge's most common false positive on this
# corpus, and the expensive kind: it marks the one honest failure mode as the
# dishonest one, and under JUDGE_ACTION=withhold it suppresses a refusal in
# favour of the same refusal while recording the answer as unsupported.
_DECLINE_RE = re.compile(
    r"(don'?t|do not|cannot|can'?t)\s+(have|know|find|answer|help)"
    r"|not\s+(sure|able to)"
    r"|pass (you )?(to|on to) (a|our|my) colleague"
    r"|hand(ing)? (you )?(over )?to (a|our) (human|colleague|agent)",
    re.I)

# Words that carry no claim, so their presence or absence says nothing about
# whether the answer stayed inside the extracts.
_STOPWORDS = {
    "that", "this", "with", "from", "your", "you", "will", "have", "been",
    "they", "their", "them", "then", "than", "there", "here", "what", "when",
    "which", "while", "would", "could", "should", "about", "into", "also",
    "please", "thanks", "hello", "sorry", "help", "like", "just", "because",
    "does", "done", "make", "made", "take", "takes", "want", "need", "know",
    "colleague", "customer", "support", "team", "sure", "happy", "hi",
}


def _numbers(text: str) -> set:
    """Every figure in the text, normalised so 4,99 and 4.99 are one number."""
    stripped = _LIST_MARKER_RE.sub("", text)
    out = set()
    for raw in _NUMBER_RE.findall(stripped):
        try:
            out.add(float(raw.replace(",", ".")))
        except ValueError:
            continue
    return out


def _content_words(text: str) -> set:
    return {w for w in (m.lower() for m in _WORD_RE.findall(text)) if w not in _STOPWORDS}


def heuristic(answer: str, context: str):
    """Return (supported, reason). Deterministic, free, no model involved."""
    invented = sorted(_numbers(answer) - _numbers(context))
    if invented:
        # Report one, not all: the first invented figure is what an on-call
        # engineer needs, and the whole list makes a poor span attribute.
        shown = invented[0]
        pretty = int(shown) if shown.is_integer() else shown
        return False, f"unsupported_number:{pretty}"

    if _DECLINE_RE.search(answer or ""):
        # Nothing was claimed, so there is nothing to be ungrounded about.
        return True, "declined"

    words = _content_words(answer)
    if not words:
        return True, "no_claims"
    grounded_words = words & _content_words(context)
    overlap = len(grounded_words) / len(words)
    if overlap < Config.JUDGE_MIN_OVERLAP:
        return False, f"low_overlap:{overlap:.2f}"
    return True, f"overlap:{overlap:.2f}"


def _ask_model(question: str, answer: str, context: str):
    """Return (supported, reason, tokens), or (None, why, tokens) if unusable."""
    try:
        r = llm.chat(
            [{"role": "system", "content": RUBRIC},
             {"role": "user",
              "content": f"Policy extracts:\n{context}\n\nQuestion: {question}\n"
                         f"Answer: {answer}\n\nVerdict as JSON:"}],
            max_tokens=Config.JUDGE_MAX_TOKENS,
            temperature=0.0,
            model=Config.JUDGE_MODEL,
        )
    except Exception:
        return None, "model_error", 0

    tokens = r.usage.total_tokens if r.usage else 0
    raw = r.choices[0].message.content or ""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None, "model_unparseable", tokens
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None, "model_unparseable", tokens
    if not isinstance(obj, dict) or not isinstance(obj.get("supported"), bool):
        # A judge that answers "maybe" has not answered. Anything but a real
        # boolean is treated as no verdict rather than coerced into one.
        return None, "model_no_verdict", tokens
    reason = str(obj.get("reason") or "")[:120]
    return obj["supported"], reason, tokens


def grade(question: str, answer: str, hits):
    """Grade one answer.

    Returns {verdict, decided_by, reason, tokens, llm_calls} where verdict is
    "supported", "unsupported" or "skipped".
    """
    if Config.JUDGE_MODE == "off":
        return {"verdict": "skipped", "decided_by": "none", "reason": "mode:off",
                "tokens": 0, "llm_calls": 0}

    context = "\n\n".join(text for _, _, text in hits)

    supported, reason = heuristic(answer, context)
    if not supported:
        # The deterministic check found something specific. Nothing a model
        # could say makes an invented figure supported, so it is not asked.
        return {"verdict": "unsupported", "decided_by": "heuristic", "reason": reason,
                "tokens": 0, "llm_calls": 0}

    if Config.JUDGE_MODE != "model":
        return {"verdict": "supported", "decided_by": "heuristic", "reason": reason,
                "tokens": 0, "llm_calls": 0}

    verdict, model_reason, tokens = _ask_model(question, answer, context)
    if verdict is None:
        # No usable verdict. The heuristic's pass stands, and says so — this is
        # the case that would otherwise look like a clean bill of health from a
        # judge that never actually ran.
        return {"verdict": "supported", "decided_by": "heuristic", "reason": model_reason,
                "tokens": tokens, "llm_calls": 1}

    return {"verdict": "supported" if verdict else "unsupported", "decided_by": "model",
            "reason": model_reason or ("ok" if verdict else "model_rejected"),
            "tokens": tokens, "llm_calls": 1}
