#This uses both the LLM provider anthropic and mistral ai as fallback using Langchain

import os
import sys
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_mistralai import ChatMistralAI

load_dotenv()

# Anthropic removed temperature/top_p/top_k on these models — sending one is a 400, not a warning.
# "opus-5" must stay ahead of nothing in particular but must BE here: it is the model this project
# runs, and the omission only shows up if ANTHROPIC_TEMPERATURE is ever set.
NO_SAMPLING_PARAMETER= ("opus-5", "opus-4-8", "opus-4-7", "sonnet-5", "fable-5", "mythos-5")

def accepts_temperature(model_name):
    name = (model_name or "").lower()
    return not any(tag in name for tag in NO_SAMPLING_PARAMETER)


def setting(name):
    # a key left blank in .env reads as "" and is falsy, so an ANTHROPIC_API_KEY= line with nothing
    # after it silently drops the run onto the fallback provider — treat blank as absent, out loud
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def fallback_warning():
    """Why the configured provider is not the one running, or None when nothing is off."""
    if setting("ANTHROPIC_API_KEY"):
        return None
    if not setting("ANTHROPIC_MODEL"):
        return None
    return (f"ANTHROPIC_MODEL is set to {setting('ANTHROPIC_MODEL')} but ANTHROPIC_API_KEY is "
            f"empty or missing, so that model is NOT being used")


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
    """Bind a schema the way the provider in hand actually honours it.

    langchain_mistralai binds tool calling as ``tool_choice="any"`` — it cannot force a named tool
    (its own TODO says so) — so a reply that skips the tool parses to None and the whole run is
    lost. Its json_schema method puts the schema in ``response_format`` where the API enforces it.

    Anthropic cannot take json_schema for BuildingAnalysis. That path compiles the schema into a
    server-side grammar, and this one is too big for it — the API rejects the request outright with
    "The compiled grammar is too large". Claude forces a tool BY NAME, though, so tool calling holds
    the shape just as firmly and skips the grammar step; the reply still arrives schema-validated by
    the parser. This is the asymmetry the two providers force: Mistral needs json_schema because it
    cannot force a named tool, Anthropic needs function_calling because it cannot compile this one.
    """
    method = "function_calling" if isinstance(llm, ChatAnthropic) else "json_schema"
    return llm.with_structured_output(schema, method=method, include_raw=include_raw)
