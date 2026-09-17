"""Thin wrapper over the model gateway.

OpenAI protocol to LiteLLM, never Ollama directly — it is what OpenLLMetry
auto-instruments, and it keeps the backend a gateway config change.
"""
from openai import OpenAI

from config import Config

client = OpenAI(base_url=Config.LITELLM_URL, api_key=Config.LITELLM_KEY)


def chat(messages, max_tokens: int = 160, temperature: float = 0.1):
    return client.chat.completions.create(
        model=Config.CHAT_MODEL, messages=messages,
        max_tokens=max_tokens, temperature=temperature,
    )
