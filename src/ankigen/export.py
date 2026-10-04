"""Export stage: write the day's kept cards as an .apkg, plus a run report.

Cards go straight into their target deck, tagged `ankigen::run_<date>`, so
nothing has to be moved afterwards. Set `inbox:` in the profile to park them in
a subdeck instead. A whole batch can be found (or deleted) later by its tag.

Note GUIDs derive from the card's uid, so importing the same day's package
twice updates the notes instead of duplicating them.
"""
from __future__ import annotations

import hashlib
import json
import logging
import warnings
from datetime import date
from pathlib import Path

import genanki

from ankigen.card_types import CARD_TYPES
from ankigen.dedup import NEAR_DUP
from ankigen.ingest import AD_HOC_TAG
from ankigen.verify import UNVERIFIED

logger = logging.getLogger(__name__)


def _model(card_type: str) -> genanki.Model:
    spec = CARD_TYPES[card_type]
    kwargs = {"model_type": spec["model_type"]} if spec.get("model_type") else {}
    return genanki.Model(
        spec["model_id"], spec["name"],
        fields=[{"name": f} for f in spec["fields"]],
        templates=[{"name": name, "qfmt": front, "afmt": back} for name, front, back in templates(spec)],
        css=spec["css"],
        **kwargs,
    )


def templates(spec: dict) -> list[tuple[str, str, str]]:
    """A card type's (name, front, back) templates: one card per note unless
    the type asks for more, as `command` does."""
    return spec.get("templates") or [("Card 1", spec["template_front"], spec["template_back"])]


def _deck_id(name: str) -> int:
    # md5 rather than hash(): stable across processes regardless of PYTHONHASHSEED.
    return int(hashlib.md5(name.encode()).hexdigest(), 16) % (10**10)


def _tag(text: str) -> str:
    return "_".join(text.split())


def _with_image(card_type: str, values: dict, filename: str | None) -> dict:
    """Put the illustration where the note type can show it."""
    return with_picture(card_type, values, f'<img src="{filename}">' if filename else "")


def with_picture(card_type: str, values: dict, picture: str) -> dict:
    """Put a picture's HTML where the note type shows it: the answer side.

    `detailed` has a dedicated Image field. `basic` and `cloze` don't, so the
    picture is appended to the answer or the hint, which Anki renders anyway.
    """
    if not picture:
        return values
    values = dict(values)
    # An <img> sits inline and needs a line break; a drawn visual is a block.
    sep = "<br>" if picture.startswith("<img") else ""
    if card_type == "detailed":
        values["Image"] = picture
    elif card_type == "command":
        values["Note"] = f"{values.get('Note', '')}{sep}{picture}"
    elif card_type == "cloze":
        # Not .lstrip("<br>"): that strips *characters*, and ate the first
        # letter of any hint beginning with b or r.
        extra = values.get("Extra", "")
        values["Extra"] = f"{extra}{sep}{picture}" if extra else picture
    else:
        values["Answer"] = f"{values.get('Answer', '')}{sep}{picture}"
    return values


# The field a card's guide reference goes in: the answer side, next to the
# picture but never in the detailed type's Image field.
GUIDE_FIELD = {"detailed": "Explanation", "command": "Note", "cloze": "Extra"}


def guide_line(run_date, ref: str) -> str:
    """Where the day's study guide explains this card. Styled inline, like the
    command breakdown, so notes already in a collection need no new CSS."""
    return ('<div class="ankigen-guide" style="margin-top:10px;font-size:13px;color:#8a93a6">'
            f"Guide {run_date} · §{ref}</div>")


def with_guide_ref(card_type: str, values: dict, run_date, ref: str | None) -> dict:
    """Append the guide reference to the answer side. An edit to a field's
    content, not a new field, so the note type stays as it is."""
    if not ref:
        return values
    values = dict(values)
    name = GUIDE_FIELD.get(card_type, "Answer")
    values[name] = f"{values.get(name, '')}{guide_line(run_date, ref)}"
    return values


def tags_for(card: dict, run_date, pipeline_id: str = "") -> list[str]:
    """A note's tags: the batch it came in, why it was asked for, and which
    pipeline made it, so a day or a whole pipeline can be found in Anki."""
    tags = ["ankigen", f"ankigen::run_{run_date}", f"ankigen::{card['request_reason']}"]
    if pipeline_id:
        tags.append(f"ankigen::pipeline::{pipeline_id}")
    if card.get("ad_hoc"):
        tags.append(AD_HOC_TAG)
    if (card.get("verify_reason") or "").startswith(UNVERIFIED):
        tags.append("ankigen::unverified")
    if (card.get("dup_reason") or "").startswith(NEAR_DUP):
        # Close to something you already have, but not close enough to bin
        # unseen. Search `tag:ankigen::near-dup` in Anki to judge them.
        tags.append("ankigen::near-dup")
    return [_tag(t) for t in tags]


def build_package(run_date: date, cards: list[dict], profile=None,
                  media_dir: Path | None = None, pipeline_id: str = "") -> genanki.Package | None:
    if not cards:
        return None
    # genanki checks field HTML against a list of tags that predates inline
    # SVG, and warns about every drawn diagram.
    warnings.filterwarnings("ignore", message="Field contained the following invalid HTML tags")
    models = {t: _model(t) for t in {c["card_type"] for c in cards}}
    decks: dict[str, genanki.Deck] = {}
    media: list[str] = []

    for c in cards:
        name = profile.deck_for(c["deck"]) if profile else c["deck"]
        deck = decks.setdefault(name, genanki.Deck(_deck_id(name), name))
        spec_fields = CARD_TYPES[c["card_type"]]["fields"]
        # Before the picture, so a picture refreshed by push later still ends
        # up last, where it always was.
        values = with_guide_ref(c["card_type"], json.loads(c["fields_json"]), run_date,
                                c.get("guide_ref"))

        filename = c.get("image_filename")
        if c.get("visual_html"):
            values = with_picture(c["card_type"], values, c["visual_html"])
        elif filename and media_dir and (media_dir / filename).exists():
            values = _with_image(c["card_type"], values, filename)
            media.append(str(media_dir / filename))
        elif filename:
            logger.warning("Image %s is missing from %s; exporting without it", filename, media_dir)

        deck.add_note(genanki.Note(
            model=models[c["card_type"]],
            fields=[str(values.get(f, "")) for f in spec_fields],
            guid=genanki.guid_for(c["card_uid"]),
            tags=tags_for(c, run_date, pipeline_id),
        ))

    package = genanki.Package(list(decks.values()))
    package.media_files = sorted(set(media))
    return package


def run(wh, run_date: date, out_root: str | Path, profile=None,
        media_dir: Path | None = None, pipeline_id: str = "") -> dict:
    out_dir = Path(out_root) / str(run_date)
    out_dir.mkdir(parents=True, exist_ok=True)

    kept = wh.query(
        "SELECT * FROM card_outcomes WHERE run_date = ? AND outcome = 'kept' ORDER BY deck, card_uid",
        [run_date],
    )
    apkg = out_dir / f"ankigen_{run_date}.apkg"
    apkg.unlink(missing_ok=True)  # a rerun that keeps nothing must not leave a stale package
    package = build_package(run_date, kept, profile, media_dir, pipeline_id)
    if package:
        package.write_to_file(str(apkg))
    write_cards(kept, out_dir / "cards.json", run_date, pipeline_id)
    return {
        "kept": len(kept),
        "apkg": str(apkg) if package else None,
        "images": len(package.media_files) if package else 0,
        "drawn": sum(1 for c in kept if c.get("visual_html")),
    }


def write_cards(kept: list[dict], path: Path, run_date: date, pipeline_id: str = "") -> None:
    """The day's cards as plain JSON, for the app's list of what a run made."""
    path.write_text(json.dumps({
        "pipeline": pipeline_id or None,
        "run_date": str(run_date),
        "cards": [{
            "deck": c["deck"], "card_type": c["card_type"], "front": c["front"],
            "back": c["back"], "topic": c.get("topic"), "guide_ref": c.get("guide_ref"),
            "picture": "drawn" if c.get("visual_html") else
                       "found" if c.get("image_filename") else None,
            "refill": bool(c.get("refill")),
            "near_dup": (c.get("dup_reason") or "").startswith(NEAR_DUP),
            "unverified": (c.get("verify_reason") or "").startswith(UNVERIFIED),
        } for c in kept],
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_report(wh, run_date: date, out_root: str | Path) -> dict:
    """Its own final stage: runs after export has finished, so every earlier
    stage's timing and status is final when the report is written."""
    out_dir = Path(out_root) / str(run_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(wh, run_date)
    apkg = out_dir / f"ankigen_{run_date}.apkg"
    report["apkg"] = str(apkg) if apkg.exists() else None
    path = out_dir / "run_report.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return {"report": str(path), "outcomes": report["outcomes"]}


def build_report(wh, run_date: date) -> dict:
    """Built only from the warehouse, so it works when stages ran as separate tasks."""
    outcomes = wh.query(
        "SELECT outcome, COUNT(*) AS n FROM card_outcomes WHERE run_date = ? GROUP BY outcome",
        [run_date],
    )
    by_deck = wh.query(
        """SELECT deck, COUNT(*) FILTER (WHERE outcome = 'kept') AS kept,
                  COUNT(*) AS generated
           FROM card_outcomes WHERE run_date = ? GROUP BY deck ORDER BY deck""",
        [run_date],
    )
    dropped = wh.query(
        """SELECT deck, front, outcome,
                  CASE outcome WHEN 'dropped_verify' THEN verify_reason ELSE dup_reason END AS reason
           FROM card_outcomes WHERE run_date = ? AND outcome LIKE 'dropped_%'
           ORDER BY outcome, deck""",
        [run_date],
    )
    stages = wh.query(
        """SELECT stage, status, rows_out, detail,
                  date_diff('millisecond', started_at, finished_at) / 1000.0 AS seconds
           FROM pipeline_runs WHERE run_date = ? AND stage != 'report' ORDER BY started_at""",
        [run_date],
    )
    details = {}
    for st in stages:
        details[st["stage"]] = json.loads(st.pop("detail") or "{}")
    generated = details.get("generate", {})
    return {
        "run_date": str(run_date),
        "outcomes": {r["outcome"]: r["n"] for r in outcomes},
        "by_deck": by_deck,
        "dropped": dropped,
        "images": wh.query(
            """SELECT COUNT(*) FILTER (WHERE filename IS NOT NULL) AS found, COUNT(*) AS wanted
               FROM card_images WHERE run_date = ?""", [run_date])[0],
        "tokens": {
            "prompt": generated.get("prompt_tokens", 0),
            "completion": generated.get("completion_tokens", 0),
        },
        "stages": stages,
        "stage_details": details,
    }


def markdown_summary(wh, run_date: date, artifact: str | None = None) -> str:
    """What a run did, for the page GitHub shows for it: read on a phone, so
    the counts first and the cards folded away underneath."""
    cards = wh.query(
        """SELECT deck, front, outcome, verify_reason, dup_reason, visual_kind,
                  image_filename, model, refill
           FROM card_outcomes c
           LEFT JOIN (SELECT card_uid, model FROM generated_cards WHERE run_date = ?) g
                USING (card_uid)
           WHERE c.run_date = ? ORDER BY deck, front""",
        [run_date, run_date],
    )
    if not cards:
        return f"### No cards for {run_date}\n\nNothing was generated. See the log for why.\n"

    kept = [c for c in cards if c["outcome"] == "kept"]
    lines = [f"### {len(kept)} new card(s) for {run_date}", "",
             "| Deck | Kept | Pictures |", "|---|---|---|"]
    for deck in sorted({c["deck"] for c in cards}):
        mine = [c for c in kept if c["deck"] == deck]
        drawn = sum(1 for c in mine if c["visual_kind"])
        found = sum(1 for c in mine if not c["visual_kind"] and c["image_filename"])
        pictures = ", ".join(p for p in (f"{drawn} drawn" if drawn else "",
                                         f"{found} found" if found else "") if p) or "none"
        lines.append(f"| {deck} | {len(mine)} | {pictures} |")

    lines += ["", "<details><summary>The cards</summary>", ""]
    for c in kept:
        notes = [c["visual_kind"] or ("picture" if c["image_filename"] else ""),
                 "refill" if c["refill"] else ""]
        note = ", ".join(n for n in notes if n)
        lines.append(f"- **{c['deck'].split('::')[-1]}** · {_one_line(c['front'])}"
                     + (f" *({note})*" if note else ""))
    lines += ["", "</details>", ""]

    dropped = [c for c in cards if c["outcome"].startswith("dropped_")]
    if dropped:
        refilled = sum(1 for c in kept if c["refill"])
        replaced = f", {refilled} replaced by the refill" if refilled else ""
        lines += [f"**Dropped {len(dropped)}{replaced}:**", ""]
        for c in dropped:
            why = c["verify_reason"] if c["outcome"] == "dropped_verify" else c["dup_reason"]
            lines.append(f"- {_one_line(c['front'])} — *{_one_line(why or '')}*")
        lines.append("")

    sections = wh.query(
        "SELECT status, COUNT(*) AS n FROM guide_sections WHERE run_date = ? GROUP BY status",
        [run_date])
    if sections:
        counts = {r["status"]: r["n"] for r in sections}
        chapters = wh.scalar("SELECT COUNT(*) FROM guide_sections "
                             "WHERE run_date = ? AND kind = 'primer'", [run_date])
        flags = ", ".join(f"{counts[s]} {s}" for s in ("disputed", "unchecked", "missing")
                          if counts.get(s))
        lines += [f"**Study guide:** {chapters} chapter(s), {sum(counts.values())} section(s)"
                  + (f" ({flags})" if flags else "")
                  + f" — `guide_{run_date}.pdf`, in this run's "
                  f"`{artifact or f'cards-{run_date}'}` artifact.", ""]

    models = sorted({c["model"] for c in cards if c["model"]})
    if models:
        lines.append(f"Written by {', '.join(f'`{m}`' for m in models)}.")
    return "\n".join(lines) + "\n"


def _one_line(text: str, limit: int = 140) -> str:
    text = " ".join(str(text).replace("|", "/").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
