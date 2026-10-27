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

# Words too common to narrow anything. A topic made only of these is rejected
# rather than matched, because matching on them deletes the whole store.
_STOPWORDS = frozenset("""
a an and any are as at be been by do does for from had has have he her his i
if in into is it its me my of on or our she that the their them then there
these they this to us was we were what when where which who will with you your
user users fact facts remember remembered forget about
""".split())

# Upper bound on a single forget. Chosen to be clearly above a real request
# ("forget my old address" touches 1-3 facts) and clearly below a sweep.
_FORGET_MAX = int(os.environ.get("MEMORY_FORGET_MAX", "5"))

# Deleted facts are kept here so a mistaken forget is recoverable instead of
# final. Redis-only and capped; this is an undo buffer, not an archive.
_TRASH_KEY = f"{USER_ID}:jarvis:memory:trash"
_TRASH_TTL = int(os.environ.get("MEMORY_TRASH_TTL_S", str(30 * 86400)))

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
# Core store (shared by the remember tool and the reflection drain)
# ---------------------------------------------------------------------------

def store_fact(fact: str, source: str = "user", dedupe: bool = False) -> bool:
    """Write one fact to both stores. Returns False if empty or (with
    ``dedupe``) a near-duplicate of something already remembered.

    ``dedupe`` is used by the nightly reflection drain — Chronicle can distill
    the same life fact on consecutive days, and reflection must not silt up
    memory. A user explicitly saying "remember X" always stores.
    """
    fact = fact.strip()
    if not fact:
        return False

    col = _get_collection()
    if dedupe and col is not None:
        try:
            existing = col.query(query_texts=[fact], n_results=1,
                                 include=["distances"])
            distances = (existing.get("distances") or [[]])[0]
            if distances and distances[0] < 0.15:
                return False
        except Exception:
            pass

    fact_id = uuid.uuid4().hex[:12]
    ts = time.time()
    if col is not None:
        try:
            col.add(
                documents=[fact],
                ids=[fact_id],
                metadatas=[{"ts": ts, "user": USER_ID, "source": source}],
            )
        except Exception as e:
            print(f"[Memory] Chroma write error: {e}")
    _r.hset(_MEM_KEY, fact_id, json.dumps({"text": fact, "ts": ts,
                                           "source": source}))
    return True


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
    if not store_fact(fact, source="user"):
        return "Nothing to remember — the fact was empty."
    return f'Noted. I\'ll remember that: "{fact.strip()}"'


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
    """Forget remembered facts matching a topic. Deletes at most a few at once.

    Args:
        topic: What to forget. Matching requires EVERY meaningful word to
            appear in the fact, so be specific -- "austin apartment" not
            "the user moved".
    """
    topic = topic.strip().lower()
    if not topic:
        return "Please specify what to forget."

    removed = 0
    # Match on MEANINGFUL words only, and require ALL of them.
    #
    # This used to be `any(t in text for t in topic.split())`, which deleted a
    # fact if ANY single word matched -- including stopwords. On 2026-10-27 the
    # topic "the user is planning to move to austin." therefore matched
    # anything containing "the", "to", "is" or "user" and wiped 15 facts in one
    # call: the user's address, job and apartment all went with it. The report
    # said "Forgot 15 fact(s)" and that was the only warning.
    terms = [t.strip(".,!?;:'\"") for t in topic.split()]
    terms = [t for t in terms if t and t not in _STOPWORDS]
    if not terms:
        return (f"'{topic}' is only common words — too vague to forget safely. "
                "Name the specific thing, e.g. 'the old apartment address'.")

    # --- Redis: find matching IDs ---
    raw = _r.hgetall(_MEM_KEY)
    ids_to_delete = []
    matched_texts = []
    for fact_id, payload in raw.items():
        try:
            text = json.loads(payload)["text"].lower()
        except (json.JSONDecodeError, KeyError):
            continue
        if all(t in text for t in terms):
            ids_to_delete.append(fact_id)
            matched_texts.append(json.loads(payload)["text"])

    # A forget that sweeps up a pile of facts is almost always a too-broad
    # topic rather than genuine intent. Refuse and show what it would have
    # taken, so the user can narrow it instead of discovering the loss later.
    if len(ids_to_delete) > _FORGET_MAX:
        preview = "\n".join(f"  - {t[:90]}" for t in matched_texts[:8])
        return (f"Refusing: '{topic}' matches {len(ids_to_delete)} facts, which "
                f"is more than the {_FORGET_MAX} I will delete at once. That "
                f"usually means the topic is too broad. Matches include:\n"
                f"{preview}\nNarrow it and try again.")

    if ids_to_delete:
        # Copy to the trash BEFORE deleting, so an unwanted forget can be undone.
        try:
            for fid in ids_to_delete:
                payload = raw.get(fid)
                if payload:
                    _r.hset(_TRASH_KEY, fid, payload)
            _r.expire(_TRASH_KEY, _TRASH_TTL)
        except Exception as e:
            print(f"[Memory] could not write trash ({e}) — deleting anyway")
        _r.hdel(_MEM_KEY, *ids_to_delete)
        removed += len(ids_to_delete)

    # --- Chroma: delete by same IDs ---
    col = _get_collection()
    if col is not None and ids_to_delete:
        try:
            col.delete(ids=ids_to_delete)
        except Exception as e:
            print(f"[Memory] Chroma delete error: {e}")

    if not removed:
        return f"Nothing remembered about '{topic}'."
    # Name what went, rather than only a count. "Forgot 15 fact(s)" was the only
    # notice given when this wiped the store, and a number tells the user
    # nothing about whether the right things were taken.
    listed = "\n".join(f"  - {t[:90]}" for t in matched_texts[:_FORGET_MAX])
    return (f"Forgot {removed} fact(s) about '{topic}':\n{listed}\n"
            f"Say \"undo that\" (restore_forgotten) if that was wrong.")


@tool
def consolidate_memory() -> str:
    """Tidy long-term memory: merge duplicate remembered facts so recall stays sharp.

    Use occasionally (or when asked to "clean up your memory"). Keeps the oldest
    copy of each duplicated fact and removes the rest from both stores.
    """
    raw = _r.hgetall(_MEM_KEY)
    if not raw:
        return "Nothing remembered yet — nothing to consolidate."
    # Group fact_ids by normalized text; keep the earliest, drop later dupes.
    by_norm: dict[str, list[tuple[float, str]]] = {}
    for fid, payload in raw.items():
        try:
            f = json.loads(payload)
            norm = " ".join(str(f.get("text", "")).lower().split())
            by_norm.setdefault(norm, []).append((float(f.get("ts", 0) or 0), fid))
        except Exception:
            continue
    dup_ids = []
    for _, entries in by_norm.items():
        if len(entries) > 1:
            entries.sort()                 # oldest first
            dup_ids.extend(fid for _, fid in entries[1:])
    if not dup_ids:
        return f"Memory is already tidy — no duplicates among {len(raw)} facts."
    _r.hdel(_MEM_KEY, *dup_ids)
    col = _get_collection()
    if col is not None:
        try:
            col.delete(ids=dup_ids)
        except Exception as e:
            print(f"[Memory] consolidate Chroma delete error: {e}")
    return f"Consolidated memory — merged {len(dup_ids)} duplicate fact(s); {len(by_norm)} unique remain."


@tool
def restore_forgotten(topic: str = "") -> str:
    """Restore facts deleted by a previous `forget`. Use for "undo that".

    Args:
        topic: Optional filter — restore only trashed facts containing this
            text. Empty restores everything still in the undo buffer.
    """
    try:
        trash = _r.hgetall(_TRASH_KEY)
    except Exception as e:
        return f"Could not reach the memory trash: {e}"
    if not trash:
        return ("Nothing to restore — the undo buffer is empty. Deletions older "
                f"than {_TRASH_TTL // 86400} days are gone for good.")

    topic = (topic or "").strip().lower()
    restored, texts = 0, []
    for fid, payload in trash.items():
        try:
            text = json.loads(payload)["text"]
        except (json.JSONDecodeError, KeyError):
            continue
        if topic and topic not in text.lower():
            continue
        _r.hset(_MEM_KEY, fid, payload)
        # Put it back in Chroma too, or it returns for exact reads but stops
        # being findable by meaning -- a half-restore is its own bug.
        col = _get_collection()
        if col is not None:
            try:
                col.upsert(ids=[fid], documents=[text])
            except Exception as e:
                print(f"[Memory] Chroma restore error: {e}")
        _r.hdel(_TRASH_KEY, fid)
        restored += 1
        texts.append(text)

    if not restored:
        return f"Nothing in the undo buffer matches '{topic}'."
    listed = "\n".join(f"  - {t[:90]}" for t in texts[:10])
    return f"Restored {restored} fact(s):\n{listed}"
