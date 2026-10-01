"""Thin wrapper over the model gateway.

The agents speak the OpenAI API to LiteLLM, never Ollama's native API. Two
reasons: it is the interface LLM instrumentation targets (OpenLLMetry upstream;
here, OneAgent's OpenAI-SDK capture, if the tenant's OneAgent supports it), and
the backend becomes a gateway config change rather than a code change.
"""
from openai import OpenAI

from config import Config

_client = OpenAI(base_url=Config.LITELLM_URL, api_key=Config.LITELLM_KEY)


def chat(messages, max_tokens: int = 256, temperature: float = 0.2):
    """Return (text, total_tokens). Token count feeds the per-ticket roll-up."""
    r = _client.chat.completions.create(
        model=Config.CHAT_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    text = (r.choices[0].message.content or "").strip()
    return text, (r.usage.total_tokens if r.usage else 0)
