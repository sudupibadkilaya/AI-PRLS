"""The team's e-book as a retrieval source, split by chapter and section.

Built once on the server with scripts/import_ebook.py (the book never goes
into git: ebook/ and data/book_* are git-ignored). Each chunk keeps where it
came from (chapter, chapter title, section, pages), so every question written
from it can record its source, and the end-of-session feedback can tell the
student exactly which chapter and section to review.

Questions are written FROM a retrieved passage (retrieve first, then
generate), never from the model's memory alone, and never by copying the
book's text or its own practice questions.
"""
from __future__ import annotations

import json
import random
import re

import numpy as np

from . import config

_chunks: list[dict] | None = None
_vectors: np.ndarray | None = None


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _load() -> list[dict]:
    global _chunks, _vectors
    if _chunks is not None:
        return _chunks
    _chunks = []
    if config.BOOK_CHUNKS_PATH.exists():
        with open(config.BOOK_CHUNKS_PATH, encoding="utf-8") as f:
            _chunks = [json.loads(line) for line in f if line.strip()]
    if config.BOOK_INDEX_PATH.exists() and _chunks:
        try:
            data = np.load(config.BOOK_INDEX_PATH)
            if len(data["vectors"]) == len(_chunks):
                _vectors = data["vectors"]
        except Exception:
            _vectors = None
    return _chunks


def available() -> bool:
    return bool(_load())


def chapters() -> list[int]:
    return sorted({c["chapter"] for c in _load() if c.get("chapter") is not None})


def chapter_title(chapter: int) -> str | None:
    for c in _load():
        if c.get("chapter") == chapter and c.get("chapter_title"):
            return c["chapter_title"]
    return None


# ---------------------------------------------------------------------------
# Choosing a passage for a new question
# ---------------------------------------------------------------------------

def pick_passage(chapter: int | None, avoid_ids: set[str] | None = None,
                 min_chapter: int | None = None) -> dict | None:
    """A passage to base the next question on. Spreads a session over
    different sections: sections already used are avoided while others remain."""
    avoid_ids = avoid_ids or set()
    pool = [c for c in _load() if c.get("usable", True)]
    if chapter is not None:
        pool = [c for c in pool if c.get("chapter") == chapter]
    elif min_chapter is not None:
        pool = [c for c in pool if (c.get("chapter") or 0) >= min_chapter]
    if not pool:
        return None
    used_sections = {_section_key(c) for c in _load() if c["id"] in avoid_ids}
    fresh = [c for c in pool if c["id"] not in avoid_ids and _section_key(c) not in used_sections]
    if not fresh:
        fresh = [c for c in pool if c["id"] not in avoid_ids] or pool
    if chapter is None:
        # mixed session: first pick a chapter evenly, then a passage in it
        used_ch = [c.get("chapter") for c in _load() if c["id"] in avoid_ids]
        by_ch: dict[int, list[dict]] = {}
        for c in fresh:
            by_ch.setdefault(c.get("chapter"), []).append(c)
        least = min(used_ch.count(ch) for ch in by_ch)
        ch = random.choice([ch for ch in by_ch if used_ch.count(ch) == least])
        fresh = by_ch[ch]
    return random.choice(fresh)


def neighbours(passage: dict, k: int = 2) -> list[dict]:
    """Other passages from the same section, for extra context."""
    same = [c for c in _load() if c["id"] != passage["id"] and _section_key(c) == _section_key(passage)]
    return same[:k]


def _section_key(c: dict) -> tuple:
    return (c.get("chapter"), c.get("section") or "")


def source_of(passage: dict) -> dict:
    """What a question records as its source."""
    return {
        "kind": "ebook",
        "chunk_id": passage["id"],
        "chapter": passage.get("chapter"),
        "chapter_title": passage.get("chapter_title") or "",
        "section": passage.get("section") or "",
        "pages": passage.get("pages") or "",
    }


def source_label(src: dict | None) -> str:
    """'Chapter 5 (Hand Injuries) - Flexor tendon repair, pp. 212-214'"""
    if not src:
        return ""
    parts = []
    if src.get("chapter") is not None:
        ch = f"Chapter {src['chapter']}"
        if src.get("chapter_title"):
            ch += f" ({src['chapter_title']})"
        parts.append(ch)
    if src.get("section"):
        parts.append(src["section"])
    label = " — ".join(parts)
    if src.get("pages"):
        pg = str(src["pages"])
        label += f", pp. {pg}" if ("-" in pg or "," in pg) else f", p. {pg}"
    return label or (src.get("doc") or "")


# ---------------------------------------------------------------------------
# Guarding against copying the book
# ---------------------------------------------------------------------------

def copied_run(text: str, passage_text: str, n: int = 10) -> str | None:
    """Return the first run of n+ words that `text` copies from the passage."""
    a = re.findall(r"[a-z0-9']+", (text or "").lower())
    b = " " + " ".join(re.findall(r"[a-z0-9']+", (passage_text or "").lower())) + " "
    for i in range(0, max(0, len(a) - n + 1)):
        run = " ".join(a[i:i + n])
        if f" {run} " in b:
            return run
    return None


_QUESTION_LINE = re.compile(r"^\s*(?:\(?[A-Da-d][\).:]|[1-4][\).])\s+\S")


def looks_like_practice_question(text: str) -> bool:
    """Book passages that ARE practice questions / answer keys are not used
    as sources (the app must not re-serve the book's own questions)."""
    lines = text.splitlines()
    options = sum(1 for ln in lines if _QUESTION_LINE.match(ln))
    if options >= 3:
        return True
    low = text.lower()
    return bool(re.search(r"\b(correct answer|answer key|rationale)s?\s*[:\-]", low)) or \
        low.count("which of the following") >= 2
