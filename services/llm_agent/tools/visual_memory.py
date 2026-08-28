"""Visual memory log — JARVIS remembers what it has seen.

The brain already receives camera/glasses photos as real image blocks, so it can
describe a scene itself. log_sighting persists that description; recall_visual
searches it — turning the glasses into a searchable visual memory:
"where did I leave my keys?", "what wine did I like at that restaurant?",
"when did I last see the toolbox?".

Backed by Redis (durable) + Chroma (semantic search, lazily indexed here, like
the journal). The heavy embedding deps already live in this container.

Redis:
    visual:log        list of sightings (newest-first), capped
    visual:indexed    set of indexed sighting ids
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "chroma")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
USER_ID = os.environ.get("JARVIS_USER_ID", "default")
_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_LOG_KEY = "visual:log"
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
            name=f"jarvis_visual_{USER_ID}",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
    except Exception as e:
        print(f"[VisualMemory] Chroma unavailable ({e}); recent-list still works.")
        _collection = None
    return _collection


def _index_new() -> None:
    col = _get_collection()
    if col is None:
        return
    try:
        rows = _r.lrange(_LOG_KEY, 0, 499) or []
    except Exception:
        return
    for raw in rows:
        try:
            s = json.loads(raw)
            sid = s.get("id")
            if not sid or _r.sismember("visual:indexed", sid):
                continue
            doc = s.get("caption", "")
            if s.get("location"):
                doc += f" (at {s['location']})"
            col.upsert(ids=[sid], documents=[doc],
                       metadatas=[{"ts": s.get("ts", ""), "location": s.get("location", "")}])
            _r.sadd("visual:indexed", sid)
        except Exception:
            continue


@tool
def log_sighting(caption: str, location: str = "") -> str:
    """Remember something you can currently see (from a glasses/camera photo).

    When the user shares a photo and wants it remembered, or asks you to note
    where something is ("remember my keys are on the shelf"), describe what you
    see and call this. Later, recall_visual finds it.

    Args:
        caption: a specific description of the scene/object and any notable
            detail (e.g. "car keys on the kitchen counter next to the fruit bowl").
        location: optional place label ("kitchen", "office", a restaurant name).
    """
    caption = caption.strip()
    if not caption:
        return "What should I remember seeing? Give me a description."
    entry = {
        "id": uuid.uuid4().hex[:12],
        "caption": caption,
        "location": location.strip(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _r.lpush(_LOG_KEY, json.dumps(entry))
        _r.ltrim(_LOG_KEY, 0, 499)
    except Exception as exc:
        return f"Couldn't save that sighting: {exc}"
    return f"Noted — I'll remember: {caption}" + (f" (in the {location})" if location else "") + "."


@tool
def recall_visual(query: str = "") -> str:
    """Search your visual memory for something you saw before.

    Use for "where did I leave my <X>?", "have you seen my <X>?", "what did that
    <thing> look like?", or empty for the most recent sightings.

    Args:
        query: what to look for (an object, place, or detail). Empty = recent.
    """
    _index_new()

    if not query.strip():
        try:
            rows = _r.lrange(_LOG_KEY, 0, 7) or []
        except Exception:
            rows = []
        if not rows:
            return "I haven't logged anything I've seen yet."
        out = []
        for raw in rows:
            try:
                s = json.loads(raw)
                when = s.get("ts", "")[:10]
                loc = f" ({s['location']})" if s.get("location") else ""
                out.append(f"• {s.get('caption','')}{loc} — {when}")
            except Exception:
                continue
        return "Recently seen:\n" + "\n".join(out)

    col = _get_collection()
    if col is None:
        return "Visual search needs the memory store, which is unavailable right now."
    try:
        res = col.query(query_texts=[query], n_results=5)
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        if not docs:
            return f"I don't recall seeing anything matching “{query}”."
        out = []
        for doc, meta in zip(docs, metas):
            when = (meta or {}).get("ts", "")[:10]
            out.append(f"• {doc} — {when}")
        return f"Here's what I recall seeing related to “{query}”:\n" + "\n".join(out)
    except Exception as e:
        return f"Visual search failed: {e}"
