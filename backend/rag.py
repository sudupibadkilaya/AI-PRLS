"""Retrieval over the team's OWN companion documents (never the textbook).

Simple by design: markdown files -> paragraph chunks -> sentence-transformer
embeddings -> cosine search with numpy. Build the index once with
scripts/build_index.py; the app loads it at startup.

If the embedding model cannot load (no network / offline laptop), retrieve()
falls back to keyword overlap so the chatbot stays runnable in mock demos.
"""
from __future__ import annotations

import re

import numpy as np

from . import config

_model = None            # lazy-loaded SentenceTransformer
_model_failed = False    # True after a failed load attempt (avoid retry spam)
_chunks: list[dict] = []  # [{"text":..., "source":...}]
_vectors: np.ndarray | None = None


def _split_chunks(text: str, source: str) -> list[dict]:
    """Split a markdown file into ~CHUNK_CHARS chunks on blank lines,
    carrying the nearest heading as context."""
    chunks, buf, heading = [], "", ""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            heading = block.lstrip("# ").strip()
        candidate = (buf + "\n\n" + block).strip()
        if len(candidate) > config.CHUNK_CHARS and buf:
            chunks.append({"text": f"[{heading}] {buf}".strip(), "source": source})
            buf = block
        else:
            buf = candidate
    if buf:
        chunks.append({"text": f"[{heading}] {buf}".strip(), "source": source})
    return chunks


def _get_model():
    """Load the embedding model; prefer local cache so demos work offline."""
    global _model, _model_failed
    if _model is not None:
        return _model
    if _model_failed:
        return None
    try:
        from sentence_transformers import SentenceTransformer
        try:
            _model = SentenceTransformer(
                config.EMBED_MODEL, device=config.EMBED_DEVICE, local_files_only=True
            )
        except Exception:
            _model = SentenceTransformer(
                config.EMBED_MODEL, device=config.EMBED_DEVICE
            )
        return _model
    except Exception as exc:
        _model_failed = True
        print(f"[AI-PRLS] Embedding model unavailable ({exc}); "
              "using keyword retrieval fallback.")
        return None


def build_index() -> int:
    """Read companion_docs/*.md, embed, save to data/. Returns chunk count."""
    docs = sorted(config.COMPANION_DIR.glob("*.md"))
    all_chunks: list[dict] = []
    for path in docs:
        if path.name.lower() == "readme.md":
            continue
        all_chunks += _split_chunks(path.read_text(encoding="utf-8"), path.name)
    if not all_chunks:
        raise SystemExit(f"No companion documents found in {config.COMPANION_DIR}")
    model = _get_model()
    if model is None:
        raise SystemExit(
            "Cannot build the embedding index — install sentence-transformers "
            "and ensure the model can download (or is already cached)."
        )
    vecs = model.encode(
        [c["text"] for c in all_chunks], normalize_embeddings=True, show_progress_bar=True
    )
    config.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    np.savez(
        config.INDEX_PATH,
        vectors=vecs.astype(np.float32),
        texts=np.array([c["text"] for c in all_chunks], dtype=object),
        sources=np.array([c["source"] for c in all_chunks], dtype=object),
    )
    return len(all_chunks)


def load_index() -> bool:
    global _chunks, _vectors
    if not config.INDEX_PATH.exists():
        return False
    data = np.load(config.INDEX_PATH, allow_pickle=True)
    _vectors = data["vectors"]
    _chunks = [
        {"text": t, "source": s} for t, s in zip(data["texts"], data["sources"])
    ]
    return True


def _keyword_retrieve(query: str, k: int) -> list[dict]:
    """Simple token-overlap fallback when embeddings are unavailable."""
    if not _chunks and not load_index():
        # Last resort: load raw companion markdown without an index file.
        docs = sorted(config.COMPANION_DIR.glob("*.md"))
        loaded: list[dict] = []
        for path in docs:
            if path.name.lower() == "readme.md":
                continue
            loaded += _split_chunks(path.read_text(encoding="utf-8"), path.name)
        if not loaded:
            return []
        # Temporarily use in-memory chunks for this request only.
        tokens = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2}
        scored = []
        for c in loaded:
            words = set(re.findall(r"[a-z0-9]+", c["text"].lower()))
            score = len(tokens & words) / max(len(tokens), 1)
            if score > 0:
                scored.append({**c, "score": float(score)})
        scored.sort(key=lambda h: -h["score"])
        return scored[:k]

    tokens = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2}
    scored = []
    for c in _chunks:
        words = set(re.findall(r"[a-z0-9]+", c["text"].lower()))
        score = len(tokens & words) / max(len(tokens), 1)
        if score > 0:
            scored.append({**c, "score": float(score)})
    scored.sort(key=lambda h: -h["score"])
    return scored[:k]


def retrieve(query: str, k: int | None = None) -> list[dict]:
    """Return top-k companion-doc chunks for the query (empty if no index)."""
    if _vectors is None and not load_index():
        return _keyword_retrieve(query, k or config.TOP_K)
    k = k or config.TOP_K
    model = _get_model()
    if model is None:
        return _keyword_retrieve(query, k)
    try:
        qv = model.encode([query], normalize_embeddings=True)[0]
    except Exception as exc:
        print(f"[AI-PRLS] Embed encode failed ({exc}); keyword fallback.")
        return _keyword_retrieve(query, k)
    scores = _vectors @ qv
    top = np.argsort(-scores)[:k]
    return [
        {**_chunks[i], "score": float(scores[i])} for i in top if scores[i] > 0.2
    ]


_source_chapter_cache: dict[str, int | None] = {}


def source_chapter(source: str) -> int | None:
    """Which TherapyEd chapter a companion doc is about, read from its first
    heading (e.g. "# Chapter map — TherapyEd Chapter 14: ..."). None = general
    doc that applies to every chapter (exam structure, Bloom's ladder, ...)."""
    if source not in _source_chapter_cache:
        chapter = None
        path = config.COMPANION_DIR / source
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("#"):
                    m = re.search(r"chapter\s*(\d+)", line, re.IGNORECASE)
                    chapter = int(m.group(1)) if m else None
                    break
        _source_chapter_cache[source] = chapter
    return _source_chapter_cache[source]


def chapter_title(chapter: int) -> str | None:
    """Title from a chapter-map heading like "TherapyEd Chapter 14: Title"."""
    for path in sorted(config.COMPANION_DIR.glob("*.md")):
        if source_chapter(path.name) != chapter:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("#"):
                m = re.search(r"chapter\s*\d+\s*[:\u2014-]\s*(.+?)(?:\(|$)", line, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
                break
    return None


def has_chapter_notes(chapter: int) -> bool:
    return any(source_chapter(p.name) == chapter
               for p in config.COMPANION_DIR.glob("*.md"))


def context_block(query: str, chapter: int | None = None) -> str:
    """Format retrieved chunks for insertion into an agent prompt.

    With `chapter`, notes written for a DIFFERENT chapter are dropped, so a
    Chapter 14 question is never steered by the Chapter 1 notes."""
    k = config.TOP_K
    hits = retrieve(query, k * 4 if chapter else k)
    if chapter:
        hits = [h for h in hits if source_chapter(h["source"]) in (None, chapter)][:k]
    if not hits:
        return "(no companion notes retrieved — rely on entry-level OT knowledge)"
    lines = [f"--- from {h['source']} ---\n{h['text']}" for h in hits]
    return "\n\n".join(lines)
