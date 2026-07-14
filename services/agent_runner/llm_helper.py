"""Provider-agnostic LLM completion helper for background agents.

Mirrors the llm_agent's provider selection but uses the raw SDKs (the
agent_runner doesn't depend on LangChain). Supports Anthropic and OpenAI.

    LLM_MODEL=claude-haiku-4-5  ->  Anthropic
    LLM_MODEL=gpt-4o-mini       ->  OpenAI
    unset                       ->  Claude haiku if only ANTHROPIC_API_KEY set
"""

import os


def _resolve_model() -> str:
    model = os.environ.get("LLM_MODEL", "").strip()
    if model:
        return model
    if os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("OPENAI_API_KEY"):
        return "claude-haiku-4-5"
    return "gpt-4o-mini"


async def complete(system: str, user: str, max_tokens: int = 800,
                   temperature: float = 0.3, model: str = "") -> str:
    """Single-shot completion. Returns the assistant's text.

    ``model`` overrides the env-resolved model for this call only — used by
    Chronicle to route summarization to a local model (e.g. an Ollama model via
    the OpenAI-compatible path) without changing the global default. For local
    models, set OPENAI_BASE_URL (e.g. http://host.docker.internal:11434/v1).
    """
    model = model or _resolve_model()

    if "claude" in model.lower() or model.startswith("anthropic"):
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        _record_usage(model, getattr(resp, "usage", None), "anthropic")
        return "".join(block.text for block in resp.content if hasattr(block, "text"))

    from openai import AsyncOpenAI

    # base_url lets this path target any OpenAI-compatible server — notably a
    # local Ollama (http://host.docker.internal:11434/v1) for the local tier.
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or None
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "placeholder"),
                         base_url=base_url)
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    _record_usage(model, getattr(resp, "usage", None), "openai")
    return resp.choices[0].message.content or ""


def _record_usage(model: str, usage_obj, provider: str) -> None:
    """Best-effort cost accounting. Attribution comes from usage.current_agent()."""
    if usage_obj is None:
        return
    try:
        import redis as _redis_mod
        import usage as _usage
        if provider == "anthropic":
            in_tok = getattr(usage_obj, "input_tokens", 0) or 0
            out_tok = getattr(usage_obj, "output_tokens", 0) or 0
        else:
            in_tok = getattr(usage_obj, "prompt_tokens", 0) or 0
            out_tok = getattr(usage_obj, "completion_tokens", 0) or 0
        r = _redis_mod.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                             decode_responses=True)
        _usage.record(r, model, in_tok, out_tok)
    except Exception:
        pass


async def tavily_search(query: str, max_results: int = 5, depth: str = "basic") -> list:
    """Run a Tavily web search. Returns a list of {title, content, url}."""
    import aiohttp

    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return []
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": depth,
                "max_results": max_results,
            },
            timeout=aiohttp.ClientTimeout(total=25),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("results", [])
