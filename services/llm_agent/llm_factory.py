"""Shared LLM factory for JARVIS.

Centralizes provider selection so the main agent, sub-agents, and any tool
that needs an LLM all build models the same way.

    LLM_MODEL=claude-sonnet-4-6 | claude-opus-4-8 | gpt-4.1 | gpt-4o-mini ...

Tiers (Anthropic only):
  haiku  → claude-haiku-4-5-20251001   fast/cheap  — simple commands & device control
  sonnet → claude-sonnet-4-6           default     — conversational & moderate tool use
  opus   → claude-opus-4-8             powerful    — complex reasoning, code, research

Prompt caching is enabled for Anthropic models via the beta header.
"""

import os

# Canonical model IDs per tier
TIER_MODELS = {
    "haiku":  "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-5",
    "opus":   "claude-opus-4-8",
}

# Header that enables Anthropic prompt caching (beta)
_CACHE_HEADER = {"anthropic-beta": "prompt-caching-2024-07-31"}


def resolve_model(default_override: str = "") -> str:
    """Return the model name to use, applying defaults."""
    model_name = (default_override or os.environ.get("LLM_MODEL", "")).strip()
    if model_name:
        return TIER_MODELS.get(model_name, model_name)

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key    = os.environ.get("OPENAI_API_KEY", "")
    if anthropic_key and not openai_key:
        return TIER_MODELS["sonnet"]
    return "gpt-4.1-mini"


def build_llm(model: str = "", temperature: float = 0.3, enable_cache: bool = True):
    """Build a LangChain chat model.

    Args:
        model: explicit model name OR tier key ("haiku" / "sonnet" / "opus")
        temperature: sampling temperature
        enable_cache: attach Anthropic prompt-caching beta header (no-op for OpenAI)
    """
    model_name = TIER_MODELS.get(model, model) if model else resolve_model()

    if "claude" in model_name.lower() or model_name.startswith("anthropic"):
        from langchain_anthropic import ChatAnthropic

        # Opus 4.7/4.8 and the Claude 5 family (Sonnet 5, Fable 5) reject the
        # temperature param with a 400. Forgetting a new model here breaks
        # EVERY request on its tier — symptom: "temperature is deprecated".
        _no_temp_models = ("opus-4-7", "opus-4-8", "fable-5", "sonnet-5", "haiku-5")
        supports_temperature = not any(m in model_name for m in _no_temp_models)

        print(f"[LLM] Using Anthropic: {model_name}")
        kwargs = dict(
            model=model_name,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            default_headers=_CACHE_HEADER if enable_cache else {},
        )
        if supports_temperature:
            kwargs["temperature"] = temperature
        return ChatAnthropic(**kwargs)

    from langchain_openai import ChatOpenAI

    print(f"[LLM] Using OpenAI: {model_name}")
    return ChatOpenAI(model=model_name, temperature=temperature)
