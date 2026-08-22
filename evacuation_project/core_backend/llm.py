#This uses both the LLM provider anthropic and mistral ai as fallback using Langchain

import os
import sys
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mistralai import ChatMistralAI

load_dotenv()

NO_SAMPLING_PARAMETER= ("opus-5", "opus-4-8", "opus-4-7", "sonnet-5", "fable-5", "mythos-5")

def accepts_temperature(model_name):
    name = (model_name or "").lower()
    return not any(tag in name for tag in NO_SAMPLING_PARAMETER)


def setting(name):
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def fallback_warning():
    if setting("ANTHROPIC_API_KEY"):
        return None
    if not setting("ANTHROPIC_MODEL"):
        return None
    return (f"ANTHROPIC_MODEL is set to {setting('ANTHROPIC_MODEL')} but ANTHROPIC_API_KEY is "
            f"empty or missing, so that model is NOT being used")

#It selects the LLM model
def select_llm(max_tokens=1024, timeout=None):
    req_timeout = timeout if timeout is not None else float(os.getenv("LLM_TIMEOUT", "300"))
    max_retries = int(os.getenv("LLM_MAX_RETRIES", "2"))

    if setting("ANTHROPIC_API_KEY"):
        model = os.getenv("ANTHROPIC_MODEL")
        kwargs = {"model_name": model, "max_tokens": max_tokens,
                  "default_request_timeout": req_timeout, "max_retries": max_retries}
        if accepts_temperature(model):
            temperature = os.getenv("ANTHROPIC_TEMPERATURE")
            if temperature is not None:
                kwargs["temperature"] = float(temperature)
        return ChatAnthropic(**kwargs), f"{model} (Anthropic API)"

    mistral_key = setting("MISTRAL_API_KEY") or setting("mistral")
    if mistral_key:
        model = os.getenv("MISTRAL_MODEL")
        temperature = os.getenv("MISTRAL_TEMPERATURE")
        label = f"{model} (Mistral AI)"
        warning = fallback_warning()
        if warning:
            print(f"[llm] {warning} — falling back to {label}", file=sys.stderr)
        return (
            ChatMistralAI(
                model=model,
                api_key=mistral_key,
                temperature=float(temperature) if temperature is not None else 0,
                max_tokens=max_tokens,
                timeout=int(req_timeout),
                max_retries=max_retries,
            ),
            label,
        )
    raise RuntimeError(
        "No LLM provider configured: set ANTHROPIC_API_KEY or MISTRAL_API_KEY in .env"
    )


def structured_output(llm, schema, include_raw=True):
    method = "function_calling" if isinstance(llm, ChatAnthropic) else "json_schema"
    return llm.with_structured_output(schema, method=method, include_raw=include_raw)
