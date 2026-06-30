"""Persistent memory tools for JARVIS.

Storage backend: Chroma (semantic vector search) + Redis (exact key-value backup).

    remember("I take the 8:05 train to work")
    recall("commute")  →  "I take the 8:05 train to work"
    forget("train")    →  removes matching facts

Chroma handles embeddings internally via sentence-transformers so there is no
numpy cosine-sim code here. If Chroma is unreachable at startup, the module
falls back to the original Redis-only implementation — Jarvis keeps working,
just without semantic search.
"""

import json
import os
import time
import uuid

import redis
from langchain_core.tools import tool

REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
CHROMA_HOST = os.environ.get("CHROMA_HOST", "chroma")
CHROMA_PORT = int(os.environ.get("CHROMA_PORT", "8000"))
USER_ID = os.environ.get("JARVIS_USER_ID", "default")

_r = redis.Redis(host=REDIS_HOST, decode_responses=True)
_MEM_KEY = f"{USER_ID}:jarvis:memory"  # Redis hash: fact_id → json({text, ts})

# ---------------------------------------------------------------------------
# Chroma setup — lazy, with graceful fallback
# ---------------------------------------------------------------------------

_chroma_collection = None
_chroma_ok = False


def _get_collection():
    """Return the Chroma collection, initialising on first call."""
    global _chroma_collection, _chroma_ok
    if _chroma_collection is not None:
        return _chroma_collection

    try:
        import chromadb
        from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

        client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
        client.heartbeat()  # raises if unreachable

        ef = SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        _chroma_collection = client.get_or_create_collection(
            name=f"jarvis_memory_{USER_ID}",
            embedding_function=ef,
            metadata={"hnsw:space": "cosine"},
        )
        _chroma_ok = True
        print(f"[Memory] Chroma connected — collection: jarvis_memory_{USER_ID}")
    except Exception as e:
        print(f"[Memory] Chroma unavailable ({e}), using Redis fallback.")
        _chroma_ok = False

    return _chroma_collection


# Attempt connection at import time (non-blocking — errors are caught above)
try:
    _get_collection()
except Exception:
    pass


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def remember(fact: str) -> str:
    """Store a fact about the user for future conversations.

    Use this whenever the user tells you something worth remembering long
    term: preferences, names, schedules, account details, habits, etc.
    Examples: "Remember my wife's name is Sarah", "I'm allergic to peanuts".

    Args:
        fact: The fact to remember, phrased as a standalone statement.
    """
    fact = fact.strip()
    if not fact:
        return "Nothing to remember — the fact was empty."

    fact_id = uuid.uuid4().hex[:12]
    ts = time.time()

    # --- Chroma (primary) ---
    col = _get_collection()
    if col is not None:
        try:
            col.add(
                documents=[fact],
                ids=[fact_id],
                metadatas=[{"ts": ts, "user": USER_ID}],
            )
        except Exception as e:
            print(f"[Memory] Chroma write error: {e}")

    # --- Redis (backup / fallback) ---
    _r.hset(_MEM_KEY, fact_id, json.dumps({"text": fact, "ts": ts}))

    return f'Noted. I\'ll remember that: "{fact}"'


@tool
def recall(topic: str = "") -> str:
    """Recall facts the user previously asked you to remember.

    Use this when the user references something they told you before, or asks
    "what do you know about X?". Leave topic empty to list everything.

    Args:
        topic: Keyword(s) to search remembered facts. Empty = return all.
    """
    topic = topic.strip()

    col = _get_collection()
    if col is not None:
        try:
            if not topic:
                # Return all facts, newest first
                result = col.get(include=["documents", "metadatas"])
                docs = result.get("documents") or []
                metas = result.get("metadatas") or []
                if not docs:
                    return "I don't have anything remembered yet."
                paired = sorted(
                    zip(metas, docs),
                    key=lambda x: x[0].get("ts", 0),
                    reverse=True,
                )
                return "\n".join(f"• {doc}" for _, doc in paired[:25])

            # Semantic search
            results = col.query(
                query_texts=[topic],
                n_results=min(25, col.count() or 1),
                include=["documents", "distances"],
            )
            docs = (results.get("documents") or [[]])[0]
            distances = (results.get("distances") or [[]])[0]

            # Chroma cosine distance: 0 = identical, 2 = opposite; keep <0.7
            filtered = [doc for doc, dist in zip(docs, distances) if dist < 0.7]
            if not filtered:
                return f"I don't have anything remembered about '{topic}'."
            return "\n".join(f"• {doc}" for doc in filtered)

        except Exception as e:
            print(f"[Memory] Chroma recall error ({e}), falling back to Redis.")

    # --- Redis fallback (substring search) ---
    raw = _r.hgetall(_MEM_KEY)
    if not raw:
        return "I don't have anything remembered yet."

    facts = []
    for _, payload in raw.items():
        try:
            facts.append(json.loads(payload))
        except json.JSONDecodeError:
            continue

    if not topic:
        facts.sort(key=lambda f: f.get("ts", 0), reverse=True)
        return "\n".join(f"• {f['text']}" for f in facts[:25])

    terms = topic.lower().split()
    matches = [f for f in facts if any(t in f["text"].lower() for t in terms)]
    matches.sort(key=lambda f: f.get("ts", 0), reverse=True)
    if not matches:
        return f"I don't have anything remembered about '{topic}'."
    return "\n".join(f"• {f['text']}" for f in matches[:25])


@tool
def forget(topic: str) -> str:
    """Forget remembered facts matching a topic.

    Args:
        topic: Keyword(s); any remembered fact containing them is deleted.
    """
    topic = topic.strip().lower()
    if not topic:
        return "Please specify what to forget."

    removed = 0
    terms = topic.split()

    # --- Redis: find matching IDs ---
    raw = _r.hgetall(_MEM_KEY)
    ids_to_delete = []
    for fact_id, payload in raw.items():
        try:
            text = json.loads(payload)["text"].lower()
        except (json.JSONDecodeError, KeyError):
            continue
        if any(t in text for t in terms):
            ids_to_delete.append(fact_id)

    if ids_to_delete:
        _r.hdel(_MEM_KEY, *ids_to_delete)
        removed += len(ids_to_delete)

    # --- Chroma: delete by same IDs ---
    col = _get_collection()
    if col is not None and ids_to_delete:
        try:
            col.delete(ids=ids_to_delete)
        except Exception as e:
            print(f"[Memory] Chroma delete error: {e}")

    return (
        f"Forgot {removed} fact(s) about '{topic}'."
        if removed
        else f"Nothing remembered about '{topic}'."
    )
