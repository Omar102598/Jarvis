"""Shared LLM factory for JARVIS.

Centralizes provider selection so the main agent, sub-agents, and any tool
that needs an LLM all build models the same way.

    LLM_MODEL=claude-opus-4-5 | claude-sonnet-4-5 | gpt-4.1 | gpt-4o-mini ...

Selection rules:
  - explicit "claude*"/"anthropic*" → Anthropic
  - explicit "gpt*"/"o*"            → OpenAI
  - unset → Claude haiku if only ANTHROPIC_API_KEY present, else gpt-4.1-mini
"""

import os


def resolve_model(default_override: str = "") -> str:
    """Return the model name to use, applying defaults."""
    model_name = (default_override or os.environ.get("LLM_MODEL", "")).strip()
    if model_name:
        return model_name

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if anthropic_key and not openai_key:
        return "claude-haiku-4-5"
    return "gpt-4.1-mini"


def build_llm(model: str = "", temperature: float = 0.3):
    """Build a LangChain chat model for the given (or default) model name."""
    model_name = resolve_model(model)

    if "claude" in model_name.lower() or model_name.startswith("anthropic"):
        from langchain_anthropic import ChatAnthropic

        print(f"[LLM] Using Anthropic: {model_name}")
        return ChatAnthropic(
            model=model_name,
            temperature=temperature,
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        )

    from langchain_openai import ChatOpenAI

    print(f"[LLM] Using OpenAI: {model_name}")
    return ChatOpenAI(model=model_name, temperature=temperature)
