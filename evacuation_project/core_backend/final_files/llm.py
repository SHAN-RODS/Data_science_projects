#This uses both the LLM provider anthropic and mistral ai as fallback using Langchain

import os
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mistralai import ChatMistralAI

load_dotenv()

NO_SAMPLING_PARAMETER= ("opus-4-8", "opus-4-7", "sonnet-5", "fable-5", "mythos-5")


def _accepts_temperature(model_name):
    name = (model_name or "").lower()
    return not any(tag in name for tag in NO_SAMPLING_PARAMETER)


def select_llm(max_tokens=1024, timeout=None):
    # The single generation call returns occupancy + distance for EVERY space plus the scenarios — a large,
    # slow structured response. The client defaults (Mistral: timeout=120s, retries=5) ReadTimeout on it and
    # then retry-storm for ~10 min. Give a generous read timeout (default 300s, override LLM_TIMEOUT) and
    # fewer retries (default 2, override LLM_MAX_RETRIES) so a slow call completes instead of failing.
    req_timeout = timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT", "300"))
    max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))

    if os.getenv("ANTHROPIC_API_KEY"):
        model = os.getenv("ANTHROPIC_MODEL")
        kwargs = {"model_name": model, "max_tokens": max_tokens,
                  "default_request_timeout": req_timeout, "max_retries": max_retries}
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
                timeout=int(req_timeout),
                max_retries=max_retries,
            ),
            f"{model} (Mistral AI)",
        )
    raise RuntimeError(
        "No LLM provider configured: set ANTHROPIC_API_KEY or MISTRAL_API_KEY in .env"
    )
