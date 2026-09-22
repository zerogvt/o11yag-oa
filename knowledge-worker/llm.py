"""Thin wrapper over the model gateway (chat + embeddings).

The agents speak the OpenAI API to LiteLLM, never Ollama's native API: it is the
interface OpenLLMetry auto-instruments, and it makes the backend a gateway config
change rather than a code change.
"""
from openai import OpenAI

from config import Config

client = OpenAI(base_url=Config.LITELLM_URL, api_key=Config.LITELLM_KEY)


def embed(text: str):
    return client.embeddings.create(model=Config.EMBED_MODEL, input=text).data[0].embedding


def chat(messages, max_tokens: int = 200, temperature: float = 0.2, model: str = None):
    """Return the raw response — callers want usage as well as the text.

    `model` is a gateway alias, not a provider model name, and defaults to the
    service's own. The judge passes its own alias so that grading and answering
    can be pointed at different models from the gateway config alone.
    """
    return client.chat.completions.create(
        model=model or Config.CHAT_MODEL, messages=messages,
        max_tokens=max_tokens, temperature=temperature,
    )
