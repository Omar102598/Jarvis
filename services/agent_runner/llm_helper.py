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
        # NO temperature here. anthropic SDK 1.2.0 removed it from
        # messages.create(), so passing it raises
        #     TypeError: AsyncMessages.create() got an unexpected keyword
        #                argument 'temperature'
        # which killed every agent on this path — the failure looked like six
        # unrelated broken agents rather than one SDK change.
        #
        # The parameter stays in this function's signature: callers pass it, and
        # the OpenAI-compatible path below (local models via Ollama) still
        # honours it.
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
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


# ---------------------------------------------------------------------------
# Native web search (Anthropic server-side tool)
#
# Replaces the "one-shot Tavily query → dump snippets into the prompt" pattern.
# The model searches iteratively on Anthropic's infrastructure and only the
# filtered, relevant material reaches the context window (dynamic filtering),
# so results are both better and cheaper in tokens than pasted snippets.
#
# Tool versions: the dynamic-filtering variant needs Opus 4.6+ / Sonnet 4.6+.
# Haiku falls back to the basic variant automatically. Because search quality
# is the whole point for research agents, SEARCH_LLM_MODEL defaults to a
# search-capable model rather than the cheap global default.
#
# Cost: web searches bill separately from tokens ($10 per 1,000 as of 2026-08),
# so every call caps searches via the tool's own max_uses.
# ---------------------------------------------------------------------------

SEARCH_LLM_MODEL = os.environ.get("SEARCH_LLM_MODEL", "claude-sonnet-5").strip()
SEARCH_MAX_USES = int(os.environ.get("SEARCH_MAX_USES", "5"))
# Guard against a runaway server-side loop (each pause_turn is another request).
_SEARCH_MAX_CONTINUATIONS = 5

# Models with the dynamic-filtering search tool. Anything else gets the basic
# variant, which still works — it just pastes more into context.
_MODERN_SEARCH_MODELS = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
    "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable-5", "claude-mythos-5",
)


def _search_tool_type(model: str) -> str:
    m = (model or "").lower()
    return ("web_search_20260209"
            if any(k in m for k in _MODERN_SEARCH_MODELS)
            else "web_search_20250305")


def search_available() -> bool:
    """True when native search can run (Anthropic key + a Claude model)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    return "claude" in (SEARCH_LLM_MODEL or "").lower()


async def complete_with_search(system: str, user: str, max_tokens: int = 1200,
                               model: str = "", max_searches: int = 0,
                               allowed_domains: list | None = None) -> str:
    """Completion with Anthropic's native web-search tool available.

    The model decides what and how often to search, then answers. Returns the
    assistant's text, or "" if search is unavailable or the call fails — the
    empty string is the caller's signal to fall back to tavily_search().

    Args:
        max_searches: cap on searches for this call (0 = SEARCH_MAX_USES).
        allowed_domains: restrict results to these domains, e.g. ["arxiv.org"].
    """
    if not search_available():
        return ""
    model = model or SEARCH_LLM_MODEL
    tool = {
        "type": _search_tool_type(model),
        "name": "web_search",
        "max_uses": max_searches or SEARCH_MAX_USES,
    }
    if allowed_domains:
        tool["allowed_domains"] = list(allowed_domains)

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))
        messages = [{"role": "user", "content": user}]
        searches = 0

        for _ in range(_SEARCH_MAX_CONTINUATIONS + 1):
            resp = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                tools=[tool],
                messages=messages,
            )
            _record_usage(model, getattr(resp, "usage", None), "anthropic")
            searches += _count_searches(getattr(resp, "usage", None))

            # The server-side tool loop hit its iteration cap — re-send with the
            # assistant turn appended and the server resumes. No extra user
            # message: the trailing server_tool_use block is the resume signal.
            if getattr(resp, "stop_reason", "") == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue

            text = "".join(b.text for b in resp.content
                           if getattr(b, "type", "") == "text")
            if searches:
                _record_search_usage(searches)
            return text.strip()

        return ""   # never converged — let the caller fall back
    except Exception as exc:
        print(f"[llm_helper] native web search failed ({exc}); caller should fall back")
        return ""


def _count_searches(usage_obj) -> int:
    """Searches billed on this response (they cost money separately from tokens)."""
    try:
        stu = getattr(usage_obj, "server_tool_use", None)
        return int(getattr(stu, "web_search_requests", 0) or 0)
    except Exception:
        return 0


def _record_search_usage(count: int) -> None:
    """Track search spend alongside token spend so the cost widget sees it."""
    if count <= 0:
        return
    try:
        from datetime import datetime, timezone
        import redis as _redis_mod
        import usage as _usage
        r = _redis_mod.Redis(host=os.environ.get("REDIS_HOST", "redis"),
                             decode_responses=True)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        who = _usage.current_agent() or "unknown"
        # $10 per 1,000 searches.
        cost = count * 0.01
        pipe = r.pipeline()
        pipe.hincrby(f"usage:daily:{day}", f"{who}:searches", count)
        pipe.hincrbyfloat(f"usage:daily:{day}", f"{who}:cost", cost)
        pipe.hincrbyfloat(f"usage:daily:{day}", "_total:cost", cost)
        pipe.execute()
    except Exception:
        pass
