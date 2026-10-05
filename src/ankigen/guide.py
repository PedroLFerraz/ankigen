"""Guide stage: a study guide for the day's kept cards, as HTML and a PDF.

A card is short on purpose, which leaves the why somewhere else: a search, a
man page, a blog post. The guide is where it goes instead. One chapter per
request (a topic, usually five cards): an opening with the mental model the
cards rest on, then a section per card with why its answer is what it is, an
example, the usual mistakes, and what to learn next.

Written by the writer's model, checked by the checker's, one call each per
chapter. A section the checker disputes stays in with the dispute printed on
it: the cards themselves were already checked, and an explanation with a
flagged sentence is still more use than none.

Section numbers are settled before any model is asked, from the kept cards
alone, so the reference each card carries (`Guide 2026-10-03 · §2.3`) is valid
even when a chapter could not be written. Export and push put it on the note.

Nothing here fails the run. A chapter the model could not write says so in the
guide, and the cards go out as they would have without it.
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path
from string import Template

from ankigen import llm
from ankigen.config import settings
from ankigen.generate import html_text
from ankigen.profile import Profile
from ankigen.targeting import LEVELS

logger = logging.getLogger(__name__)

SECTION_COLUMNS = ("run_date", "card_uid", "request_id", "ref", "kind", "markdown",
                   "status", "issue", "model")
# A chapter for a request with no topic is headed by why it was asked for.
REASON_TITLES = {
    "weak_card": "Revisiting a card you keep missing",
    "gap": "Filling a gap in the deck",
}
CARD_PARTS = (("why", "Why"), ("example", "Example"),
              ("mistakes", "Common mistakes"), ("related", "Related"))


@dataclass
class Chapter:
    number: int
    request_id: str
    deck: str
    title: str
    level: int | None
    cards: list[dict] = field(default_factory=list)
    primer: str = ""
    status: str = "missing"
    issue: str = ""
    model: str | None = None


def pdf_path(out_root: str | Path, run_date: date) -> Path:
    return Path(out_root) / str(run_date) / f"guide_{run_date}.pdf"


def html_path(out_root: str | Path, run_date: date) -> Path:
    return Path(out_root) / str(run_date) / f"guide_{run_date}.html"


# ------------------------------------------------------------ the outline

def outline(wh, run_date: date, profile: Profile) -> list[Chapter]:
    """The guide's chapters and their cards, numbered, before anything is
    written: decks in the profile's order, topics in the curriculum's."""
    rows = wh.query(
        """SELECT c.card_uid, c.request_id, c.deck, c.card_type, c.front, c.back,
                  c.fields_json, c.topic, c.request_reason, c.refill, c.visual_html,
                  r.kind, r.level, r.focus
           FROM card_outcomes c JOIN requests r USING (run_date, request_id)
           WHERE c.run_date = ? AND c.outcome = 'kept'""",
        [run_date],
    )
    decks = [d.deck for d in profile.decks]
    topics = {d.deck: d.topics for d in profile.decks}

    def order(row: dict) -> tuple:
        deck = row["deck"]
        mine = topics.get(deck, [])
        topic = " ".join((row["topic"] or "").split())
        return (decks.index(deck) if deck in decks else len(decks), deck,
                mine.index(topic) if topic in mine else len(mine),
                row["topic"] or "", row["request_id"],
                bool(row["refill"]), row["front"].lower(), row["card_uid"])

    chapters: list[Chapter] = []
    by_request: dict[str, Chapter] = {}
    for row in sorted(rows, key=order):
        chapter = by_request.get(row["request_id"])
        if chapter is None:
            title = row["topic"] or REASON_TITLES.get(row["request_reason"], "More cards")
            chapter = Chapter(len(chapters) + 1, row["request_id"], row["deck"], title,
                              row["level"])
            chapters.append(chapter)
            by_request[row["request_id"]] = chapter
        row["ref"] = f"{chapter.number}.{len(chapter.cards) + 1}"
        row["markdown"], row["status"], row["issue"] = "", "missing", ""
        chapter.cards.append(row)
    return chapters


# ------------------------------------------------------------ writing

def _prompt(name: str, **values) -> str:
    text = resources.files("ankigen.prompts").joinpath(name).read_text(encoding="utf-8")
    return Template(text).substitute(**values)


def _listed(i: int, card: dict) -> str:
    entry = f"{i}. Q: {card['front']}\n   A: {card['back']}"
    parts = json.loads(card.get("fields_json") or "{}").get("_parts")
    if parts:
        entry += f"\n   Parts: {parts}"
    if card.get("focus"):
        entry += "\n   (The learner keeps forgetting a card on this; explain it from the ground up.)"
    return entry


def write_prompt(chapter: Chapter, profile: Profile) -> str:
    target = next((d for d in profile.decks if d.deck == chapter.deck), None)
    instructions = target.instructions.strip() if target else ""
    return _prompt(
        "guide.txt",
        level=profile.learner.level,
        language=profile.learner.language,
        level_note=f"- {LEVELS[chapter.level]}" if chapter.level in LEVELS else "",
        deck=chapter.deck,
        subject=chapter.title,
        deck_instructions=f"\nABOUT THIS DECK\n{instructions}\n" if instructions else "",
        cards="\n".join(_listed(i, c) for i, c in enumerate(chapter.cards, start=1)),
    )


def check_prompt(chapter: Chapter, profile: Profile) -> str:
    sections = "\n\n".join(
        f"{i}. Card: {c['front']} -> {c['back']}\n{c['markdown']}"
        for i, c in enumerate(chapter.cards, start=1) if c["markdown"])
    return _prompt("guide_check.txt", level=profile.learner.level, deck=chapter.deck,
                   primer=chapter.primer or "(none)", sections=sections or "(none)")


def _by_index(data) -> dict:
    found = data if isinstance(data, list) else (data or {}).get("results",
                                                                 (data or {}).get("cards", []))
    return {r.get("index"): r for r in found if isinstance(r, dict)}


def _text(value) -> str:
    if isinstance(value, list):
        value = "\n".join(f"- {v}" for v in value)
    return str(value or "").strip()


# Markdown that has to start a line of its own: a code fence, a list, a table.
_BLOCK_START = re.compile(r"^(```|[-*+] |\d+\. |\|)")


def card_markdown(entry: dict) -> str:
    """A card's section: each part under its label, which runs into the
    paragraph unless the part opens with a block that needs its own line."""
    out = []
    for key, label in CARD_PARTS:
        text = _text(entry.get(key))
        if text:
            sep = "\n\n" if _BLOCK_START.match(text) else " "
            out.append(f"**{label}.**{sep}{text}")
    return "\n\n".join(out)


def apply_written(chapter: Chapter, data, model: str) -> None:
    """The writer's answer onto the chapter: its opening and each card's section."""
    chapter.primer = _text((data or {}).get("primer") if isinstance(data, dict) else "")
    chapter.model = model
    chapter.status, chapter.issue = ("unchecked", "") if chapter.primer else \
        ("missing", "the model wrote no opening")
    written = _by_index(data)
    for i, card in enumerate(chapter.cards, start=1):
        card["markdown"] = card_markdown(written.get(i, {}))
        card["status"], card["issue"] = ("unchecked", "") if card["markdown"] else \
            ("missing", "the model skipped this card")


def apply_checked(chapter: Chapter, data) -> None:
    """The checker's verdicts: checked, or disputed with what it disputes.
    A section it said nothing about stays unchecked."""
    data = data if isinstance(data, dict) else {"results": data}
    if chapter.primer and data.get("primer_ok") is not None:
        ok = data["primer_ok"] is True
        chapter.status = "checked" if ok else "disputed"
        chapter.issue = "" if ok else _text(data.get("primer_issue")) or "no reason given"
    verdicts = _by_index(data)
    for i, card in enumerate(chapter.cards, start=1):
        verdict = verdicts.get(i)
        if not card["markdown"] or verdict is None:
            continue
        ok = verdict.get("correct") is True
        card["status"] = "checked" if ok else "disputed"
        card["issue"] = "" if ok else _text(verdict.get("issue")) or "no reason given"


def _not_written(chapter: Chapter, why: str) -> None:
    chapter.status, chapter.issue = "missing", why
    for card in chapter.cards:
        card["status"], card["issue"] = "missing", why


def _unchecked(chapter: Chapter, why: str) -> None:
    """Say why the written sections went unchecked."""
    if chapter.status == "unchecked":
        chapter.issue = why
    for card in chapter.cards:
        if card["status"] == "unchecked":
            card["issue"] = why


def write(chapters: list[Chapter], profile: Profile) -> dict:
    """Write and check every chapter, as far as the day's allowance goes."""
    checker = settings.resolve_verify()
    tokens = {"prompt_tokens": 0, "completion_tokens": 0}
    out_of_writer = out_of_checker = ""
    for chapter in chapters:
        if out_of_writer:
            _not_written(chapter, f"not written today: {out_of_writer}")
            continue
        try:
            result = llm.call_json(write_prompt(chapter, profile))
        except llm.QuotaExhausted as e:
            # The rest would each fail the same way after the same backoff.
            logger.warning("Writer quota exhausted; the guide stops at §%d: %s", chapter.number, e)
            out_of_writer = str(e)
            _not_written(chapter, f"not written today: {e}")
            continue
        except Exception as e:
            logger.warning("Could not write §%d (%s): %s", chapter.number, chapter.title, e)
            _not_written(chapter, f"not written: {e}")
            continue
        tokens["prompt_tokens"] += result.prompt_tokens
        tokens["completion_tokens"] += result.completion_tokens
        apply_written(chapter, result.data, result.model)
        if not profile.verify:
            _unchecked(chapter, "checking is switched off")
            continue
        if out_of_checker:
            _unchecked(chapter, f"unchecked: {out_of_checker}")
            continue
        try:
            verdicts = llm.call_json(check_prompt(chapter, profile), cfg=checker)
        except llm.QuotaExhausted as e:
            logger.warning("Checker quota exhausted; the guide is unchecked from §%d: %s",
                           chapter.number, e)
            out_of_checker = str(e)
            _unchecked(chapter, f"unchecked: {e}")
            continue
        except Exception as e:
            logger.warning("Could not check §%d (%s): %s", chapter.number, chapter.title, e)
            _unchecked(chapter, f"unchecked: {e}")
            continue
        apply_checked(chapter, verdicts.data)
    return tokens


def section_rows(run_date: date, chapters: list[Chapter]) -> list[tuple]:
    rows = []
    for ch in chapters:
        rows.append((run_date, None, ch.request_id, str(ch.number), "primer", ch.primer,
                     ch.status, ch.issue, ch.model))
        rows += [(run_date, c["card_uid"], ch.request_id, c["ref"], "card", c["markdown"],
                  c["status"], c["issue"], ch.model) for c in ch.cards]
    return rows


# ------------------------------------------------------------ rendering

def _md(text: str) -> str:
    """Markdown to HTML. Without the `markdown` package (it comes with the
    [guide] extra) paragraphs and `code` still come out readable."""
    if not text:
        return ""
    try:
        import markdown
    except ImportError:
        blocks = re.split(r"\n\s*\n", text.strip())
        out = []
        for block in blocks:
            fenced = re.match(r"^```\w*\n(.*?)\n?```$", block, flags=re.S)
            out.append(f"<pre><code>{html.escape(fenced.group(1))}</code></pre>" if fenced
                       else f"<p>{html_text(block).replace(chr(10), '<br>')}</p>")
        return "\n".join(out)
    return markdown.markdown(text, extensions=["fenced_code", "tables", "sane_lists"],
                             output_format="html")


def _plain(text: str) -> str:
    """A card's plain-text side as HTML. A build card's front is a line of
    context and then its code, so the first line stays text."""
    text = str(text or "")
    if "\n" not in text:
        return html_text(text)
    first, code = text.split("\n", 1)
    return f"{html_text(first)}<pre><code>{html.escape(code, quote=False)}</code></pre>"


def _callout(status: str, issue: str) -> str:
    label = {"disputed": "The checker disputes this section",
             "unchecked": "Not checked",
             "missing": "Not written"}.get(status)
    if not label:
        return ""
    detail = f": {html_text(issue)}" if issue else ""
    return f'<p class="callout {status}"><strong>{label}</strong>{detail}</p>'


def _card_box(card: dict) -> str:
    parts = json.loads(card.get("fields_json") or "{}").get("_parts")
    rows = [f'<div class="q">{_plain(card["front"])}</div>',
            f'<div class="a">{_plain(card["back"])}</div>']
    if parts:
        rows.append(f'<div class="parts">{html_text(parts)}</div>')
    if card.get("visual_html"):
        rows.append(f'<div class="visual">{card["visual_html"]}</div>')
    return f'<div class="card">{"".join(rows)}</div>'


CSS = """
@page { size: A4; margin: 2cm 2cm 2.2cm;
        @bottom-center { content: counter(page); font-size: 9pt; color: #777; } }
@page :first { @bottom-center { content: none; } }
body { font-family: "DejaVu Sans", Arial, sans-serif; font-size: 10.5pt; line-height: 1.5;
       color: #1d2330; background: #fff; }
h1 { font-size: 24pt; margin: 0 0 4pt; }
h2.deck { font-size: 16pt; border-bottom: 2px solid #1d2330; padding-bottom: 3pt;
          margin-top: 0; page-break-before: always; }
h3 { font-size: 13.5pt; margin: 18pt 0 6pt; color: #24408e; }
h4 { font-size: 11pt; margin: 16pt 0 4pt; page-break-after: avoid; }
.cover p.sub { color: #555; margin: 0 0 18pt; }
.toc ol { list-style: none; padding-left: 0; }
.toc li { margin: 2pt 0; }
.toc li.deck { font-weight: bold; margin-top: 8pt; }
.toc a { color: inherit; text-decoration: none; }
.toc a::after { content: leader('.') target-counter(attr(href), page); }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 9.5pt;
       background: #f1f3f7; padding: 0 2pt; border-radius: 2pt; }
pre { background: #f1f3f7; padding: 6pt 8pt; border-radius: 3pt; white-space: pre-wrap;
      page-break-inside: avoid; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; margin: 6pt 0; }
th, td { border: 1px solid #cdd3de; padding: 2pt 6pt; text-align: left; }
.card { border-left: 3px solid #24408e; background: #f7f8fb; padding: 6pt 10pt;
        margin: 4pt 0 8pt; page-break-inside: avoid; }
.card .q { font-weight: bold; }
.card .a { margin-top: 3pt; }
.card .parts { margin-top: 3pt; color: #555; font-size: 9.5pt; }
.card .visual { margin-top: 6pt; }
.card .visual svg { max-width: 100%; height: auto; }
.callout { padding: 4pt 8pt; border-radius: 3pt; font-size: 9.5pt; }
.callout.disputed { background: #fdecea; border-left: 3px solid #c0392b; }
.callout.unchecked { background: #fff6e0; border-left: 3px solid #d39e00; }
.callout.missing { background: #eef0f4; border-left: 3px solid #8a93a6; }
"""


def render_html(run_date: date, chapters: list[Chapter]) -> str:
    decks: list[str] = []
    for ch in chapters:
        if ch.deck not in decks:
            decks.append(ch.deck)
    cards = sum(len(ch.cards) for ch in chapters)

    toc, body = [], []
    for deck in decks:
        mine = [ch for ch in chapters if ch.deck == deck]
        toc.append(f'<li class="deck">{html.escape(deck)}</li>')
        body.append(f'<h2 class="deck">{html.escape(deck)}</h2>')
        for ch in mine:
            anchor = f"s{ch.number}"
            toc.append(f'<li><a href="#{anchor}">§{ch.number} {html.escape(ch.title)}</a></li>')
            body.append(f'<h3 id="{anchor}">§{ch.number} {html.escape(ch.title)}</h3>')
            body.append(_callout(ch.status, ch.issue))
            body.append(_md(ch.primer))
            for card in ch.cards:
                body.append(f'<h4 id="s{card["ref"].replace(".", "-")}">§{card["ref"]}</h4>')
                body.append(_card_box(card))
                body.append(_callout(card["status"], card["issue"]))
                body.append(_md(card["markdown"]))

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Study guide {run_date}</title>
<style>{CSS}</style></head>
<body>
<section class="cover">
<h1>Study guide</h1>
<p class="sub">{run_date} · {cards} card(s) in {len(chapters)} chapter(s) ·
{html.escape(", ".join(decks))}</p>
<nav class="toc"><ol>{"".join(toc)}</ol></nav>
</section>
{"".join(b for b in body if b)}
</body></html>
"""


def write_pdf(page: str, path: Path) -> bool:
    """The page as a PDF, through WeasyPrint (the [guide] extra, and Pango
    underneath it). False when it is not installed: the HTML is still there."""
    try:
        from weasyprint import HTML
    except Exception as e:          # ImportError, or OSError for missing Pango
        logger.warning("No PDF for the guide, only HTML (pip install 'ankigen[guide]'): %s", e)
        return False
    HTML(string=page).write_pdf(str(path))
    return True


# ------------------------------------------------------------ the stage

def run(wh, run_date: date, profile: Profile, out_root: str | Path) -> dict:
    out_dir = Path(out_root) / str(run_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    page_path, pdf = html_path(out_root, run_date), pdf_path(out_root, run_date)
    # A rerun that keeps nothing must not leave the last guide behind.
    page_path.unlink(missing_ok=True)
    pdf.unlink(missing_ok=True)

    chapters = outline(wh, run_date, profile)
    tokens = write(chapters, profile) if chapters else {"prompt_tokens": 0,
                                                        "completion_tokens": 0}
    rows = section_rows(run_date, chapters)
    wh.replace_partition("guide_sections", run_date, SECTION_COLUMNS, rows)

    made_pdf = False
    if chapters:
        page = render_html(run_date, chapters)
        page_path.write_text(page, encoding="utf-8")
        try:
            made_pdf = write_pdf(page, pdf)
        except Exception as e:      # a rendering bug costs the PDF, not the day
            logger.warning("Could not render the guide as a PDF: %s", e)
    statuses = [r[6] for r in rows]
    return {
        "sections": len(rows),
        "chapters": len(chapters),
        "checked": statuses.count("checked"),
        "disputed": statuses.count("disputed"),
        "unchecked": statuses.count("unchecked"),
        "missing": statuses.count("missing"),
        "html": str(page_path) if chapters else None,
        "pdf": str(pdf) if made_pdf else None,
        **tokens,
    }
