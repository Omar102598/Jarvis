"""The brain's prompt, assembled from files in config/ instead of code.

    config/SOUL.md                     who Jarvis is (personality, voice)
    config/prompts/brain/core.md       operating rules + the skills index slot
    config/skills/<name>/SKILL.md      playbooks, loaded on demand (load_skill)
    config/prompts/surfaces/<s>.md     per-surface style (voice / app / glasses)

config/ is volume-mounted at /config, so editing any of these changes the next
turn -- no rebuild, no restart. The static prompt is rebuilt only when a file's
mtime changes, so the 1h prompt cache isn't invalidated by an unchanged read.

Skills follow the SKILL.md convention (Claude Code, Hermes Agent): a YAML-ish
front matter with `name` and `description`; only those two lines go into every
request, the body is fetched when the model decides it needs it.
"""

from __future__ import annotations

import glob
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATES = [os.environ.get("JARVIS_CONFIG_DIR", "/config"),
               os.path.normpath(os.path.join(_HERE, "..", "..", "config"))]
_FALLBACK = ("You are JARVIS, an advanced AI assistant inspired by the AI from Iron Man. "
             "You are British, witty, efficient, and loyal.")
_SOUL_MARKER = "<!-- loaded verbatim below this line -->"


def _config_dir() -> str:
    for d in _CANDIDATES:
        if os.path.isfile(os.path.join(d, "prompts", "brain", "core.md")):
            return d
    return _CANDIDATES[0]


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    meta = {}
    for line in text[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[end + 4:].lstrip("\n")


def skills() -> dict[str, dict]:
    """name -> {"description", "body", "path"} for every SKILL.md."""
    out = {}
    for path in sorted(glob.glob(os.path.join(_config_dir(), "skills", "*", "SKILL.md"))):
        meta, body = _front_matter(_read(path))
        name = meta.get("name") or os.path.basename(os.path.dirname(path))
        out[name] = {"description": meta.get("description", ""), "body": body.strip(),
                     "path": path}
    return out


def _signature() -> tuple:
    d = _config_dir()
    files = [os.path.join(d, "SOUL.md"), os.path.join(d, "prompts", "brain", "core.md")]
    files += glob.glob(os.path.join(d, "skills", "*", "SKILL.md"))
    sig = []
    for f in sorted(files):
        try:
            sig.append((f, os.path.getmtime(f)))
        except OSError:
            pass
    return tuple(sig)


_cache: tuple[tuple, str] | None = None


def base_prompt() -> str:
    """SOUL + core rules with the skills index filled in. Cached by mtime."""
    global _cache
    sig = _signature()
    if _cache and _cache[0] == sig:
        return _cache[1]
    d = _config_dir()
    soul = _read(os.path.join(d, "SOUL.md"))
    if _SOUL_MARKER in soul:
        soul = soul.split(_SOUL_MARKER, 1)[1]
    core = _read(os.path.join(d, "prompts", "brain", "core.md"))
    index = "\n".join(f"- {n}: {s['description']}" for n, s in skills().items())
    core = core.replace("{skills_index}", index or "(none installed)")
    text = "\n\n".join(p.strip() for p in (soul, core) if p.strip()) or _FALLBACK
    _cache = (sig, text)
    return text


def surface_note(surface: str) -> str:
    """Per-surface style note ('voice' | 'app' | 'glasses'), or ''."""
    return _read(os.path.join(_config_dir(), "prompts", "surfaces", f"{surface}.md")).strip()
