"""Generate stage: turn each request's prompt into cards.

This is the only non-deterministic stage. Everything downstream reads its
output from the warehouse, so retrying verify/dedup/export never re-calls the
model or changes which cards exist.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

from ankigen import llm, visuals

logger = logging.getLogger(__name__)


class GenerationFailed(RuntimeError):
    """A request failed or produced nothing, so the day is not carried forward."""

CLOZE_PLAIN = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}")


@dataclass
class Card:
    front: str          # plain text, used for dedup and verification
    back: str
    fields: dict = field(default_factory=dict)  # note fields, keyed by card_types field name
    image_query: str = ""   # empty unless the model judged a picture would help
    visual: dict | None = None  # a table or diagram to draw, see visuals.py


def clean_field(text: str) -> str:
    """Strip stray formatting characters that small models produce."""
    text = re.sub(r"^[>=#@*•\-]+\s*", "", str(text).strip())
    return text.strip("*").strip()


def html_text(text: str) -> str:
    """Text as it goes into a note field: escaped, with `backticks` as code.

    Anki renders fields as HTML. Unescaped, `journalctl -u <service-name> -f`
    reached phones as `journalctl -u -f`: the placeholder was read as a tag.
    """
    return re.sub(r"`([^`\n]+)`", r"<code>\1</code>", html.escape(str(text), quote=False))


# What each piece of a command does, shown under the answer. The first time a
# card comes up is when its flags are being learned, and `du -sh * | sort -h`
# is four new things at once: a breakdown there beats searching mid-review.
MAX_PARTS = 10


def parse_parts(raw) -> list[tuple[str, str]]:
    """[(piece, what it does)], from [{"part": .., "means": ..}] or [[.., ..]]."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            piece, means = item.get("part", ""), item.get("means", "")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            piece, means = item
        else:
            continue
        piece, means = str(piece).strip().strip("`").strip(), clean_field(means)
        if piece and means and len(piece) <= 60 and len(means) <= 100:
            out.append((piece, means))
    return out[:MAX_PARTS]


def parts_html(parts: list[tuple[str, str]]) -> str:
    """The breakdown as a small table under the answer. Styled inline, so the
    notes already in a collection need no change to their note type."""
    if not parts:
        return ""
    rows = "".join(
        f'<tr><td style="padding:1px 12px 1px 0;white-space:nowrap">'
        f'<code>{html.escape(piece, quote=False)}</code></td>'
        f'<td style="padding:1px 0">{html_text(means)}</td></tr>'
        for piece, means in parts)
    return ('<table class="ankigen-parts" style="margin:12px auto 0;text-align:left;'
            f'font-size:15px;color:#9aa5b8;border-collapse:collapse">{rows}</table>')


def parts_text(parts: list[tuple[str, str]]) -> str:
    """The breakdown as the checker reads it."""
    return "; ".join(f"`{piece}` = {means}" for piece, means in parts)


def fix_cloze_syntax(text: str) -> str:
    """Repair the cloze markup models commonly get wrong."""
    text = re.sub(r"(?<!\{)\{(c\d+::.*?)\}(?!\})", r"{{\1}}", text)   # {c1::x}   -> {{c1::x}}
    text = re.sub(r"\{{3,}(c\d+::.*?)\}{3,}", r"{{\1}}", text)        # {{{c1::x}}} -> {{c1::x}}
    text = re.sub(r"\{\{(?!c\d+::)(.*?)\}\}", r"{{c1::\1}}", text)     # {{x}}     -> {{c1::x}}
    return text


def image_query(item: dict) -> str:
    """The model leaves this empty for cards a picture wouldn't help.

    Long queries are truncated rather than dropped: image search matches fewer
    and worse results the more words it is given, and the model occasionally
    answers with most of the card's question.
    """
    q = clean_field(item.get("image_query", ""))
    words = q.split()
    if len(words) > 6:
        q = " ".join(words[:6])
    return q if 3 <= len(q) <= 80 else ""


def _hideable(hide: str, code: str) -> bool:
    """Whether `hide` can become a cloze in `code`: present, and free of the
    `::` and `}}` that would end Anki's cloze markup early."""
    return bool(hide.strip()) and hide in code and "::" not in hide and "}}" not in hide


def parse_kind(kind: str, data: dict) -> list[Card]:
    """Cards of one kind (see prompts/kind_*.txt), as the note types they are
    filed under. `front` and `back` stay plain text for the checker and dedup;
    the fields are HTML."""
    raw = data.get("cards", []) if isinstance(data, dict) else []
    cards: list[Card] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if kind in ("command", "shortcut"):
            task, answer = clean_field(item.get("task", "")), clean_field(item.get("answer", ""))
            note = clean_field(item.get("note", ""))
            if len(task) < 5 or not answer:
                logger.warning("Skipping %s card without a task and answer: %r", kind, task[:60])
                continue
            if "`" not in answer:
                answer = f"`{answer}`"
            parts = parse_parts(item.get("parts"))
            fields = {"Task": html_text(task), "Command": html_text(answer),
                      "Note": html_text(note) + parts_html(parts)}
            if parts:
                # Not a field of the note type, so neither the export nor the
                # push writes it: it is here for the checker.
                fields["_parts"] = parts_text(parts)
            cards.append(Card(task, f"{answer} {note}".strip(), fields))
        elif kind == "concept":
            q, a = clean_field(item.get("question", "")), clean_field(item.get("answer", ""))
            if len(q) < 5 or len(a) < 2:
                logger.warning("Skipping concept card: Q=%r A=%r", q[:60], a[:40])
                continue
            cards.append(Card(q, a, {"Question": html_text(q), "Answer": html_text(a)},
                              image_query(item), visuals.parse(item.get("visual"))))
        elif kind == "scenario":
            q, a = clean_field(item.get("question", "")), clean_field(item.get("answer", ""))
            why = clean_field(item.get("why", ""))
            if len(q) < 5 or len(a) < 2:
                logger.warning("Skipping scenario card: Q=%r A=%r", q[:60], a[:40])
                continue
            cards.append(Card(q, a, {"Question": html_text(q), "Summary": html_text(a),
                                     "Explanation": html_text(why), "Image": "", "Reference": ""},
                              image_query(item), visuals.parse(item.get("visual"))))
        elif kind == "build":
            card = _build_card(item)
            if card:
                cards.append(card)
    return cards


def _build_card(item: dict) -> Card | None:
    """One snippet as one cloze note, a gap per hidden part: Anki makes a card
    of each gap and spaces them out. Written as separate notes, the same file
    five times over read to dedup as five copies of one card, and it kept one.
    """
    context = clean_field(item.get("context", ""))
    code = re.sub(r"^```\w*\n|\n?```$", "", str(item.get("code", "")).strip("\n"))
    hides = item.get("hides")
    if not isinstance(hides, list):          # the older shape: one part per card
        hides = [{"hide": item.get("hide", ""), "hint": item.get("hint", "")}]
    text, front, answers, pos = [], [], [], 0
    for h in hides:
        hide = str(h.get("hide", "")).strip() if isinstance(h, dict) else ""
        at = code.find(hide, pos) if hide else -1
        if at < 0 or not _hideable(hide, code):
            logger.warning("Skipping a hidden part that is not in its code: %r", hide[:60])
            continue
        hint = clean_field(h.get("hint", "")).replace("::", ":").replace("}}", "")
        n = len(answers) + 1
        text += [html.escape(code[pos:at], quote=False),
                 f"{{{{c{n}::{html.escape(hide, quote=False)}"
                 + (f"::{html.escape(hint, quote=False)}" if hint else "") + "}}"]
        front += [code[pos:at], f"[{n}]"]
        answers.append(f"[{n}] {hide}")
        pos = at + len(hide)
    if not answers:
        logger.warning("Skipping build card with nothing hidden: %r", context[:60])
        return None
    text.append(html.escape(code[pos:], quote=False))
    front.append(code[pos:])
    return Card(f"{context}\n{''.join(front)}", "; ".join(answers), {
        "Text": f"{html_text(context)}<pre><code>{''.join(text)}</code></pre>", "Extra": ""})


def parse_cards(card_type: str, data: dict, kind: str | None = None) -> list[Card]:
    if kind:
        return parse_kind(kind, data)
    raw = data.get("cards", []) if isinstance(data, dict) else []
    cards: list[Card] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue

        if card_type == "cloze":
            text = fix_cloze_syntax(clean_field(item.get("text", "")))
            extra = clean_field(item.get("extra", ""))
            if not CLOZE_PLAIN.search(text) or len(text) < 10:
                logger.warning("Skipping cloze without a valid deletion: %r", text[:60])
                continue
            plain = CLOZE_PLAIN.sub(r"\1", text)
            cards.append(Card(plain, text, {"Text": html_text(text), "Extra": html_text(extra)},
                              image_query(item),
                              visuals.parse(item.get("visual"))))

        elif card_type == "detailed":
            q = clean_field(item.get("question", ""))
            summary = clean_field(item.get("summary", ""))
            explanation = clean_field(item.get("explanation", ""))
            summary = summary or (explanation.split(". ")[0].rstrip(".") + "." if explanation else "")
            if len(q) < 5 or len(summary) < 3:
                logger.warning("Skipping low-quality detailed card: %r", q[:60])
                continue
            cards.append(Card(q, summary, {
                "Question": html_text(q), "Summary": html_text(summary),
                "Explanation": html_text(explanation or summary),
                "Image": "", "Reference": "",
            }, image_query(item), visuals.parse(item.get("visual"))))

        else:
            q = clean_field(item.get("question", ""))
            a = clean_field(item.get("answer", ""))
            if len(q) < 5 or len(a) < 2:
                logger.warning("Skipping low-quality card: Q=%r A=%r", q[:60], a[:40])
                continue
            cards.append(Card(q, a, {"Question": html_text(q), "Answer": html_text(a)},
                              image_query(item),
                              visuals.parse(item.get("visual"))))
    return cards


def generate_for_request(req: dict) -> tuple[list[Card], llm.LLMResult | None, int, int]:
    """Cards for one request. Tops up once if the model returns too few."""
    want = req["n"]
    cards: list[Card] = []
    result = None
    p_tok = c_tok = 0
    prompt = req["prompt"]

    for attempt in range(2):
        result = llm.call_json(prompt)
        p_tok += result.prompt_tokens
        c_tok += result.completion_tokens
        for card in parse_cards(req["card_type"], result.data, req.get("kind")):
            if card.front.lower() not in {c.front.lower() for c in cards}:
                cards.append(card)
        if len(cards) >= want:
            break
        already = "\n".join(f"- {c.front}" for c in cards)
        prompt = (
            f"{req['prompt']}\n\nYou already wrote these in this batch; do not repeat them:\n"
            f"{already}\nWrite exactly {want - len(cards)} more."
        )
    return cards[:want], result, p_tok, c_tok


GENERATED_COLUMNS = (
    "run_date", "card_uid", "request_id", "deck", "card_type", "front", "back",
    "fields_json", "image_query", "model", "prompt_tokens", "completion_tokens",
)
# What this stage writes: the columns above, and the drawn picture if any.
# Kept apart so rows built without one still fit GENERATED_COLUMNS.
WRITTEN_COLUMNS = GENERATED_COLUMNS + ("visual_json",)


def card_uid(run_date: date, request_id: str, front: str) -> str:
    return hashlib.sha1(f"{run_date}|{request_id}|{front.lower()}".encode()).hexdigest()[:16]


def card_rows(run_date: date, req: dict, cards: list[Card], result: llm.LLMResult | None,
              p_tok: int, c_tok: int) -> list[tuple]:
    """One request's cards as WRITTEN_COLUMNS rows. Tokens are attributed to
    the first card so per-request totals stay summable."""
    return [(
        run_date, card_uid(run_date, req["request_id"], card.front), req["request_id"],
        req["deck"], req["card_type"], card.front, card.back,
        json.dumps(card.fields, ensure_ascii=False), card.image_query,
        result.model if result else None,
        p_tok if i == 0 else 0, c_tok if i == 0 else 0,
        json.dumps(card.visual, ensure_ascii=False) if card.visual else None,
    ) for i, card in enumerate(cards)]


def run(wh, run_date: date, requests: list[dict]) -> dict:
    """Write the day's cards, one model request per entry of `requests`.

    One request that fails fails the stage, and nothing is written: a day that
    ran short is not a finished one, and a half-written day must not reach
    Anki. The next run writes the whole day again.
    """
    rows = []
    total_prompt = total_completion = 0
    for req in requests:
        try:
            cards, result, p_tok, c_tok = generate_for_request(req)
        except Exception as e:
            raise GenerationFailed(
                f"Request {req['request_id']} ({req['deck']}) failed, so nothing was "
                f"written: {str(e)[:400]}"
            ) from e
        total_prompt += p_tok
        total_completion += c_tok
        rows.extend(card_rows(run_date, req, cards, result, p_tok, c_tok))
    # Two requests can land on the same card; keep the first.
    seen, unique = set(), []
    for r in rows:
        if r[1] not in seen:
            seen.add(r[1])
            unique.append(r)
    wh.replace_partition("generated_cards", run_date, WRITTEN_COLUMNS, unique)
    if requests and not unique:
        # Downstream stages treat "no cards" as "nothing to do", so without this
        # a run whose answers held no card finishes green with an empty
        # package — the one outcome you would want an alert for.
        raise GenerationFailed(
            f"All {len(requests)} request(s) produced nothing: "
            "the model returned no usable cards"
        )
    return {
        "cards": len(unique),
        "requested": sum(r["n"] for r in requests),
        "with_image_query": sum(1 for r in unique if r[8]),
        "with_visual": sum(1 for r in unique if r[12]),
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
    }
