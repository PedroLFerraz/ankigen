"""Cards from one book: its chapters, in its order, with its own figures.

Cards written from a topic name assume whatever background the model thinks
is usual, and a learner without it is left with answers that do not connect
to anything. Written from the text of a book they are reading, each card
leans on what the book has explained so far, cites its page, and shows the
book's own diagram when one illustrates it.

The PDF never leaves this machine: the text goes to the model, the cards go
to Anki, and the book stays out of the repository.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from datetime import date
from pathlib import Path
from importlib import resources
from string import Template

from ankigen import llm
from ankigen.generate import clean_field, html_text

logger = logging.getLogger(__name__)

FIGURE_CAPTION = re.compile(r"^Figure (\d+-\d+)\. (.+)$", re.MULTILINE)
FOOTER = re.compile(r"^(\d{1,4}) \| |\| (\d{1,4})$")
SKIP_SECTIONS = re.compile(r"additional resources|further reading|references|bibliography", re.I)
CARDS_PER_PAGE = 1.2


@dataclass
class Figure:
    id: str             # "1-1", as the book numbers it
    caption: str
    page: str           # printed page label
    path: Path


@dataclass
class Section:
    title: str
    first: int          # page indexes, inclusive
    last: int


@dataclass
class Chapter:
    number: int         # position among the book's chapters, from 1
    title: str
    first: int
    last: int
    sections: list[Section] = field(default_factory=list)


def title_of(pdf: Path) -> str:
    """"Fundamentals of Data Engineering (Reis, Joe...) (Z-Library)" -> the title."""
    return re.sub(r"\s*\(.*?\)", "", pdf.stem).strip() or pdf.stem


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _outline(reader) -> list[tuple[int, int, str]]:
    """(depth, page index, title) for every bookmark, in order."""
    out = []

    def walk(items, depth):
        for it in items:
            if isinstance(it, list):
                walk(it, depth + 1)
            else:
                out.append((depth, reader.get_destination_page_number(it), str(it.title).strip()))

    walk(reader.outline, 0)
    return out


def chapters(reader) -> list[Chapter]:
    """The book's chapters and appendices, each split at its next level of
    bookmarks. A chapter runs until the next bookmark at its depth or above."""
    marks = _outline(reader)
    found = []
    for i, (depth, page, title) in enumerate(marks):
        if not re.match(r"(chapter|appendix)\b", title, re.I):
            continue
        end = next((p for d, p, _ in marks[i + 1:] if d <= depth), len(reader.pages))
        ch = Chapter(len(found) + 1, title, page, max(page, end - 1))
        subs = [(p, t) for d, p, t in marks[i + 1:] if d == depth + 1 and page <= p <= ch.last]
        for j, (p, t) in enumerate(subs):
            # ponytail: page-granular, so a page shared by two sections is read
            # by both; the prompt's "already written" list keeps the cards apart.
            nxt = subs[j + 1][0] if j + 1 < len(subs) else ch.last + 1
            if not SKIP_SECTIONS.search(t):
                ch.sections.append(Section(t, p, max(p, nxt - 1)))
        if not ch.sections:
            ch.sections.append(Section(title, ch.first, ch.last))
        found.append(ch)
    return found


@lru_cache(maxsize=None)
def page_text(reader, index: int) -> str:
    return reader.pages[index].extract_text() or ""


def label(reader, index: int) -> str:
    """The printed page number, which is what the reader's copy shows.

    Read from the running footer ("4 | Chapter 1: ..." or "... | 5"), or a
    neighbour's on a page without one (a chapter's opening page): the PDF's
    own page labels can be off by one from the print.
    """
    for i, back in ((index, 0), (index + 1, 1), (index - 1, -1)):
        lines = page_text(reader, i).strip().splitlines() if 0 <= i < len(reader.pages) else []
        m = FOOTER.search(lines[-1].strip()) if lines else None
        if m:
            return str(int(m.group(1) or m.group(2)) - back)
    try:
        return reader.page_labels[index]
    except (IndexError, AttributeError):
        return str(index + 1)


def text_of(reader, first: int, last: int) -> str:
    return "\n".join(f"[page {label(reader, i)}]\n{page_text(reader, i)}"
                     for i in range(first, last + 1))


def figures(reader, first: int, last: int, out_dir: Path) -> list[Figure]:
    """Captioned figures, saved as PNGs. A page's images are paired with its
    "Figure N-M." captions in order.

    ponytail: pairing by order only works when the counts match; a page where
    they do not (a sidebar drawing, a vector figure with no image) gives none.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    found = []
    for i in range(first, last + 1):
        page = reader.pages[i]
        captions = FIGURE_CAPTION.findall(page_text(reader, i))
        if not captions:
            continue
        try:
            images = list(page.images)
        except Exception as e:  # a malformed image stream must not sink the chapter
            logger.warning("Could not read the images on page %s: %s", label(reader, i), e)
            continue
        if len(images) != len(captions):
            logger.info("Page %s: %d image(s) for %d caption(s); skipping its figures",
                        label(reader, i), len(images), len(captions))
            continue
        for (fid, caption), img in zip(captions, images):
            path = out_dir / f"figure-{fid}.png"
            if not path.exists():
                img.image.save(path)
            found.append(Figure(fid, caption.strip(), label(reader, i), path))
    return found


def build_prompt(book: str, chapter: Chapter, section: Section, text: str,
                 figs: list[Figure], avoid: list[str], level: str, language: str) -> str:
    pages = section.last - section.first + 1
    return Template(resources.files("ankigen.prompts").joinpath("book.txt").read_text(encoding="utf-8")).substitute(
        book=book, chapter=chapter.title, section=section.title, text=text,
        level=level, language=language,
        max=max(2, round(pages * CARDS_PER_PAGE)),
        figures="\n".join(f"- {f.id}: {f.caption} (page {f.page})" for f in figs) or "(none)",
        avoid="\n".join(f"- {a}" for a in avoid) or "(none yet)",
    )


def source_line(book: str, chapter: Chapter, section: Section, page: str) -> str:
    """Where in the book the card comes from, styled inline like the guide
    line so the note type needs no new CSS."""
    where = f"{book} · ch. {chapter.number} · {section.title}" + (f" · p. {page}" if page else "")
    return ('<div class="ankigen-source" style="margin-top:10px;font-size:13px;color:#8a93a6">'
            f"{html.escape(where, quote=False)}</div>")


def card_uid(book: str, question: str) -> str:
    """Stable across runs, so writing a chapter again updates its notes in
    Anki rather than adding copies."""
    return hashlib.sha1(f"book|{book}|{question.lower()}".encode()).hexdigest()[:16]


def parse(data, book: str, chapter: Chapter, section: Section, figs: dict[str, Figure],
          deck: str, run_date: date) -> list[dict]:
    """The model's cards as the rows export and push read."""
    from ankigen.images import inline_src

    rows = []
    for item in (data or {}).get("cards", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        q, a = clean_field(item.get("question", "")), clean_field(item.get("answer", ""))
        ctx, page = clean_field(item.get("context", "")), clean_field(item.get("page", ""))
        if len(q) < 5 or len(a) < 2:
            logger.warning("Skipping card: Q=%r A=%r", q[:60], a[:40])
            continue
        fig = figs.get(clean_field(item.get("figure", "")))
        rows.append({
            "card_uid": card_uid(book, q), "card_type": "detailed", "deck": deck,
            "front": q, "back": a, "run_date": run_date,
            "fields_json": json.dumps({
                "Question": html_text(q), "Summary": html_text(a),
                "Explanation": html_text(ctx) + source_line(book, chapter, section, page),
                "Image": "", "Reference": "",
            }, ensure_ascii=False),
            "visual_html": f'<img src="{inline_src(fig.path)}">' if fig else None,
            "figure": fig.id if fig else "",
            "request_reason": f"book::{slug(book)}::ch{chapter.number:02}",
        })
    return rows


def write_chapter(reader, book: str, chapter: Chapter, deck: str, work: Path,
                  level: str, language: str, run_date: date) -> list[dict]:
    """Every card for one chapter, a model call per section, in the book's order."""
    figs = {f.id: f for f in figures(reader, chapter.first, chapter.last, work / "figures")}
    cards: list[dict] = []
    for section in chapter.sections:
        in_section = [f for f in figs.values()
                      if any(label(reader, i) == f.page for i in range(section.first, section.last + 1))]
        prompt = build_prompt(book, chapter, section, text_of(reader, section.first, section.last),
                              in_section, [c["front"] for c in cards], level, language)
        try:
            result = llm.call_json(prompt)
        except llm.QuotaExhausted:
            raise
        except Exception as e:  # one bad section must not lose the others
            logger.error("Section %r failed: %s", section.title, e)
            continue
        seen = {c["card_uid"] for c in cards}
        new = [c for c in parse(result.data, book, chapter, section, figs, deck, run_date)
               if c["card_uid"] not in seen]
        logger.info("%-50s %2d card(s) [%s]", section.title[:50], len(new), result.model)
        cards += new
    return cards

