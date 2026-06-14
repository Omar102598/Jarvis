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
                   temperature: float = 0.3) -> str:
    """Single-shot completion. Returns the assistant's text."""
    model = _resolve_model()

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
        return "".join(block.text for block in resp.content if hasattr(block, "text"))

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", "placeholder"))
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content or ""


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
