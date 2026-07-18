"""Shared LLM provider selection (LangChain).

Both AI stages (space-use classification and scenario generation) use this. Anthropic is preferred;
Mistral is the fallback. Model, key, and temperature come from ``.env``.

Newer Anthropic models (Opus 4.8/4.7, Sonnet 5, Fable 5) **reject** the ``temperature`` sampling
parameter and return a 400 if it is sent, so it is only passed when the configured model is known to
accept it. Reproducibility on those models comes from grounding + the number fact-check, not sampling.
"""

import os
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mistralai import ChatMistralAI

load_dotenv()

# Anthropic model families that reject temperature/top_p/top_k (would 400 if passed).
_NO_SAMPLING_PARAM = ("opus-4-8", "opus-4-7", "sonnet-5", "fable-5", "mythos-5")


def _accepts_temperature(model_name):
    name = (model_name or "").lower()
    return not any(tag in name for tag in _NO_SAMPLING_PARAM)


def select_llm(max_tokens=1024):
    """Return (llm, model_label). Anthropic if ANTHROPIC_API_KEY is set, else Mistral."""
    if os.getenv("ANTHROPIC_API_KEY"):
        model = os.getenv("ANTHROPIC_MODEL")
        kwargs = {"model_name": model, "max_tokens": max_tokens}
        if _accepts_temperature(model):
            temperature = os.getenv("ANTHROPIC_TEMPERATURE")
            if temperature is not None:
                kwargs["temperature"] = float(temperature)
        return ChatAnthropic(**kwargs), f"{model} (Anthropic API)"

    mistral_key = os.getenv("MISTRAL_API_KEY") or os.getenv("mistral")
    if mistral_key:
        model = os.getenv("MISTRAL_MODEL")
        temperature = os.getenv("MISTRAL_TEMPERATURE")
        return (
            ChatMistralAI(
                model=model,
                api_key=mistral_key,
                temperature=float(temperature) if temperature is not None else 0,
                max_tokens=max_tokens,
            ),
            f"{model} (Mistral AI)",
        )

    raise RuntimeError(
        "No LLM provider configured: set ANTHROPIC_API_KEY (or MISTRAL_API_KEY) in .env"
    )
