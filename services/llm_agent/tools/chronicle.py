"""recall_journal — search JARVIS's daily memory (written nightly by Chronicle).

Chronicle (in agent_runner) writes a concise journal entry per day to Redis.
This tool makes those entries answerable:

  • exact date  ("2026-07-04", "yesterday", "today") → that day's entry,
  • free text    ("when did the plumber come?")       → semantic search,
  • empty                                             → the last week.

Semantic search is powered by Chroma, indexed lazily here (the brain already
has Chroma + embeddings; agent_runner deliberately does not). Entries are
indexed on first use and tracked in a Redis set so re-indexing is cheap.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "chroma")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
USER_ID = os.environ.get("JARVIS_USER_ID", "default")
USER_TZ = os.environ.get("USER_TZ", "America/Chicago")

_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_collection = None
_collection_tried = False


def _get_collection():
    global _collection, _collection_tried
    if _collection is not None or _collection_tried:
        return _collection
    _collection_tried = True
    try:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        client.heartbeat()
        ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        _collection = client.get_or_create_collection(
            name=f"jarvis_chronicle_{USER_ID}",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as e:
        print(f"[Chronicle] Chroma unavailable ({e}); date lookups still work.")
        _collection = None
    return _collection


def _index_new_entries() -> None:
    """Push any not-yet-indexed journal entries into Chroma. Cheap & idempotent."""
    col = _get_collection()
    if col is None:
        return
    try:
        entries = _r.lrange("chronicle:entries", 0, 179) or []
    except Exception:
        return
    for raw in entries:
        try:
            e = json.loads(raw)
            date = e.get("date")
            if not date or _r.sismember("chronicle:indexed", date):
                continue
            col.upsert(
                ids=[date],
                documents=[e.get("summary", "")],
                metadatas=[{"date": date, "ts": e.get("ts", "")}],
            )
            _r.sadd("chronicle:indexed", date)
        except Exception:
            continue


def _resolve_date(q: str) -> str | None:
    """Map a query to an ISO date if it names one, else None."""
    q = q.strip().lower()
    tz = ZoneInfo(USER_TZ)
    today = datetime.now(tz).date()
    if q in ("today",):
        return today.isoformat()
    if q in ("yesterday",):
        return (today - timedelta(days=1)).isoformat()
    # bare ISO date
    try:
        return datetime.strptime(q[:10], "%Y-%m-%d").date().isoformat()
    except Exception:
        return None


@tool
def recall_journal(query: str = "") -> str:
    """Search your daily journal (Chronicle's nightly memory of what happened).

    Use this to answer "what did I do on/around <when>?", "when did <event>
    happen?", "how has my week been?", or to recall past days generally.

    Args:
        query: A date ("2026-07-04", "yesterday", "today"), a description to
            search for ("plumber visit", "gym days"), or empty for the last week.
    """
    _index_new_entries()

    # 1) Exact-day lookup.
    date = _resolve_date(query)
    if date:
        raw = _r.get(f"chronicle:day:{date}")
        if not raw:
            return f"No journal entry for {date} — it may have been a day before Chronicle started, or the Mac was off."
        try:
            e = json.loads(raw)
            return f"📓 {date}\n{e.get('summary','')}"
        except Exception:
            return f"No readable entry for {date}."

    # 2) Empty → last week from Redis (newest first).
    if not query.strip():
        try:
            entries = _r.lrange("chronicle:entries", 0, 6) or []
        except Exception:
            entries = []
        if not entries:
            return "No journal entries yet — Chronicle writes one each night."
        out = []
        for raw in entries:
            try:
                e = json.loads(raw)
                out.append(f"📓 {e.get('date','')}: {e.get('summary','')}")
            except Exception:
                continue
        return "\n\n".join(out)

    # 3) Free-text → semantic search.
    col = _get_collection()
    if col is None:
        return "Journal search needs the memory store, which is unavailable right now. Try asking by date."
    try:
        res = col.query(query_texts=[query], n_results=5)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        if not docs:
            return f"Nothing in the journal matches “{query}”."
        out = []
        for doc, meta in zip(docs, metas):
            d = (meta or {}).get("date", "")
            out.append(f"📓 {d}: {doc}")
        return "\n\n".join(out)
    except Exception as e:
        return f"Journal search failed: {e}"
