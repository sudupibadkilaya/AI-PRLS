"""Split the team's e-book into chapters and sections and build its index.

Run ON THE SERVER only (the book never goes into git):

    python scripts/import_ebook.py ebook/<your-book>.pdf      (or .epub)

What it does
  1. Reads the book (PDF via its bookmarks/outline, or EPUB via its chapters).
  2. Splits it chapter by chapter and section by section, keeping page numbers.
  3. Cuts each section into passages and drops passages that are the book's
     own practice questions or answer keys (the app must not re-serve them).
  4. Saves data/book_chunks.jsonl and embeds the passages into
     data/book_index.npz (both git-ignored).

Check the printed chapter list afterwards. If chapters were not detected, use
--chapter-pattern to match how the book writes its chapter headings.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import book, config  # noqa: E402

DEFAULT_CHAPTER_RE = r"^\s*(?:chapter|ch\.?|unit)\s*(\d{1,2})\b[\s:.\-–—]*(.*)$"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _pdf_outline(reader) -> list[tuple[int, str, int]]:
    """Flatten the PDF bookmarks into (depth, title, page_index)."""
    out = []

    def walk(items, depth):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
                continue
            try:
                page = reader.get_destination_page_number(it)
            except Exception:
                continue
            out.append((depth, str(it.title).strip(), page))
    try:
        walk(reader.outline, 0)
    except Exception:
        return []
    return out


def read_pdf(path: Path, chapter_re: re.Pattern) -> list[dict]:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    n = len(reader.pages)
    texts = []
    for i, p in enumerate(reader.pages):
        try:
            texts.append(p.extract_text() or "")
        except Exception:
            texts.append("")
        if (i + 1) % 100 == 0:
            print(f"  read {i + 1}/{n} pages")

    marks = _pdf_outline(reader)
    # chapters: outline entries whose title looks like "Chapter N ..."
    chapter_marks = []
    for depth, title, page in marks:
        m = chapter_re.match(title)
        if m:
            chapter_marks.append((page, int(m.group(1)), (m.group(2) or "").strip(), depth))
    if not chapter_marks:
        # no usable bookmarks: find "CHAPTER N" headings on the pages
        for i, t in enumerate(texts):
            for line in t.splitlines()[:6]:
                m = chapter_re.match(line)
                if m:
                    chapter_marks.append((i, int(m.group(1)), (m.group(2) or "").strip(), 0))
                    break
        # keep the first page of each chapter number only
        seen, uniq = set(), []
        for cm in sorted(chapter_marks):
            if cm[1] not in seen:
                seen.add(cm[1]); uniq.append(cm)
        chapter_marks = uniq
    chapter_marks.sort()
    if not chapter_marks:
        raise SystemExit("No chapters found. Try --chapter-pattern (see --help).")

    sections_out = []
    for idx, (start, ch, ch_title, depth) in enumerate(chapter_marks):
        end = chapter_marks[idx + 1][0] if idx + 1 < len(chapter_marks) else n
        # sections: bookmarks one level deeper inside this chapter's pages
        subs = sorted((p, t) for d, t, p in marks if d == depth + 1 and start <= p < end)
        bounds = [(start, "")] + [(p, t) for p, t in subs if p > start or t]
        merged = []
        for p, t in bounds:
            if merged and merged[-1][0] == p:
                merged[-1] = (p, t or merged[-1][1])
            else:
                merged.append((p, t))
        for j, (s_start, s_title) in enumerate(merged):
            s_end = merged[j + 1][0] if j + 1 < len(merged) else end
            s_end = max(s_end, s_start + 1)
            pages = list(range(s_start, min(s_end, n)))
            sections_out.append({
                "chapter": ch, "chapter_title": ch_title, "section": s_title,
                "pages": [(pg + 1, texts[pg]) for pg in pages],
            })
    return sections_out


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------

def _html_to_blocks(markup: str) -> list[tuple[str, str]]:
    """(kind, text) blocks: kind is h1/h2/h3 or p."""
    markup = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup)
    blocks = []
    for m in re.finditer(r"(?is)<(h[1-3]|p|li|td)[^>]*>(.*?)</\1>", markup):
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", m.group(2)))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            blocks.append((m.group(1).lower(), text))
    return blocks


def read_epub(path: Path, chapter_re: re.Pattern) -> list[dict]:
    z = zipfile.ZipFile(str(path))
    container = z.read("META-INF/container.xml").decode("utf-8", "ignore")
    opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
    opf = z.read(opf_path).decode("utf-8", "ignore")
    base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
    items = dict(re.findall(r'<item[^>]+id="([^"]+)"[^>]+href="([^"]+)"', opf))
    items.update({k: v for v, k in re.findall(r'<item[^>]+href="([^"]+)"[^>]+id="([^"]+)"', opf)})
    spine = re.findall(r'<itemref[^>]+idref="([^"]+)"', opf)

    sections_out, cur = [], None
    ch, ch_title = None, ""
    for idref in spine:
        href = items.get(idref)
        if not href:
            continue
        try:
            markup = z.read(base + href).decode("utf-8", "ignore")
        except KeyError:
            continue
        for kind, text in _html_to_blocks(markup):
            m = chapter_re.match(text) if kind in ("h1", "h2") else None
            if m:
                ch, ch_title = int(m.group(1)), (m.group(2) or "").strip()
                cur = {"chapter": ch, "chapter_title": ch_title, "section": "", "pages": [("", "")]}
                sections_out.append(cur)
            elif kind in ("h2", "h3") and ch is not None:
                cur = {"chapter": ch, "chapter_title": ch_title, "section": text, "pages": [("", "")]}
                sections_out.append(cur)
            elif cur is not None:
                pg, t = cur["pages"][-1]
                cur["pages"][-1] = (pg, t + "\n" + text)
    if not sections_out:
        raise SystemExit("No chapters found. Try --chapter-pattern (see --help).")
    return sections_out


# ---------------------------------------------------------------------------
# Passages
# ---------------------------------------------------------------------------

def _clean(text: str) -> str:
    text = text.replace("­", "")
    text = re.sub(r"-\n(?=[a-z])", "", text)          # re-join hyphenated words
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def make_chunks(sections: list[dict]) -> list[dict]:
    chunks = []
    for s in sections:
        buf, buf_pages = "", []

        def flush():
            nonlocal buf, buf_pages
            text = buf.strip()
            if len(text) > 200:
                pg = [p for p in buf_pages if p != ""]
                pages = (f"{pg[0]}" if pg and pg[0] == pg[-1] else f"{pg[0]}-{pg[-1]}") if pg else ""
                chunks.append({
                    "id": f"c{s['chapter']}-{len(chunks)}",
                    "chapter": s["chapter"], "chapter_title": s["chapter_title"],
                    "section": s["section"], "pages": pages, "text": text,
                    "usable": not book.looks_like_practice_question(text),
                })
            buf, buf_pages = "", []

        for page_no, raw in s["pages"]:
            for para in re.split(r"\n\s*\n|(?<=[.:])\n(?=[A-Z])", _clean(raw)):
                para = para.strip()
                if not para:
                    continue
                if len(buf) + len(para) > config.BOOK_CHUNK_CHARS and buf:
                    flush()
                buf += ("\n" if buf else "") + para
                if page_no not in buf_pages:
                    buf_pages.append(page_no)
        flush()
    return chunks


def embed(chunks: list[dict]) -> bool:
    try:
        import numpy as np
        from backend import rag
        model = rag._get_model()
        if model is None:
            return False
        texts = [f"{c['chapter_title']} {c['section']}\n{c['text']}" for c in chunks]
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
        np.savez(config.BOOK_INDEX_PATH, vectors=vecs.astype("float32"))
        return True
    except Exception as exc:
        print(f"  (embedding skipped: {exc}; keyword retrieval will be used)")
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book", type=Path, help="path to the e-book (.pdf or .epub)")
    ap.add_argument("--chapter-pattern", default=DEFAULT_CHAPTER_RE,
                    help="regex for chapter headings; group 1 = chapter number, group 2 = title")
    ap.add_argument("--first-chapter", type=int, default=config.FIRST_PRACTICE_CHAPTER,
                    help="chapters before this are kept but not used for questions")
    args = ap.parse_args()
    chapter_re = re.compile(args.chapter_pattern, re.IGNORECASE)

    print(f"Reading {args.book} ...")
    if args.book.suffix.lower() == ".epub":
        sections = read_epub(args.book, chapter_re)
    else:
        sections = read_pdf(args.book, chapter_re)
    chunks = make_chunks(sections)
    for c in chunks:
        if c["chapter"] < args.first_chapter:
            c["usable"] = False

    config.BOOK_CHUNKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(config.BOOK_CHUNKS_PATH, "w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    embedded = embed(chunks)

    print("\nChapters found:")
    by_ch: dict[int, list[dict]] = {}
    for c in chunks:
        by_ch.setdefault(c["chapter"], []).append(c)
    for ch in sorted(by_ch):
        cs = by_ch[ch]
        secs = len({c["section"] for c in cs})
        usable = sum(c["usable"] for c in cs)
        print(f"  Chapter {ch:>2}: {cs[0]['chapter_title'][:50]:<50} {secs:>3} sections, "
              f"{len(cs):>4} passages ({usable} usable)")
    skipped = sum(not c["usable"] for c in chunks)
    print(f"\n{len(chunks)} passages saved to {config.BOOK_CHUNKS_PATH} "
          f"({skipped} skipped as practice questions or early chapters); "
          f"embeddings: {'yes' if embedded else 'no (keyword fallback)'}.")
    print("Restart the app to use the e-book.")


if __name__ == "__main__":
    main()
