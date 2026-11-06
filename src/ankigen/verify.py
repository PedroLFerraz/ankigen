"""Verify stage: a second LLM pass that fact-checks each generated card.

Cards are checked in batches per request, so a run costs one extra call per
request rather than per card.

If the checker itself fails (rate limit, outage) cards pass through marked
`unverified` instead of being dropped: they reach the deck tagged
`ankigen::unverified` for human triage, and a flaky checker shouldn't zero out
a day's run. The run report and the note tags make the difference visible.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from importlib import resources
from string import Template

from ankigen import llm, visuals
from ankigen.config import settings
from ankigen.profile import Profile

logger = logging.getLogger(__name__)

PASS_SCORE = 0.7
UNVERIFIED = "unverified"


def build_prompt(deck: str, level: str, cards: list[dict]) -> str:
    """The checker's prompt for one batch, cards numbered from 1 so its
    verdicts can be matched back by index."""
    template = Template(resources.files("ankigen.prompts").joinpath("verify.txt").read_text(encoding="utf-8"))
    listing = "\n".join(_listed(i, c) for i, c in enumerate(cards, start=1))
    return template.substitute(level=level, deck=deck, cards=listing)


def _listed(i: int, card: dict) -> str:
    entry = f"{i}. Q: {card['front']}\n   A: {card['back']}"
    parts = json.loads(card.get("fields_json") or "{}").get("_parts")
    if parts:
        entry += f"\n   Parts: {parts}"
    visual = visuals.parse(card.get("visual_json"))
    return entry + (f"\n   Visual ({visuals.describe(visual)})" if visual else "")


def visual_verdicts(results: list, cards: list[dict]) -> list[bool | None]:
    """Whether each card's visual survived the check: False only when the
    checker said it was wrong, so a skipped answer does not cost a picture."""
    by_index = {r.get("index"): r for r in results if isinstance(r, dict)} \
        if isinstance(results, list) else {}
    verdicts = []
    for i, card in enumerate(cards, start=1):
        said = by_index.get(i, {}).get("visual_ok")
        verdicts.append(None if not card.get("visual_json") or said is None else bool(said))
    return verdicts


def judge(results: list, cards: list[dict]) -> list[tuple[bool, float | None, str]]:
    """Map the checker's verdicts back onto cards by index.

    A card is judged on its question and answer. Told that, the checker still
    failed two good cards over a scrambled table and nothing else, so a card
    whose only complaint is its visual passes, and loses the visual instead.
    """
    by_index = {}
    for r in results if isinstance(results, list) else []:
        if isinstance(r, dict) and isinstance(r.get("index"), int):
            by_index[r["index"]] = r

    verdicts = []
    for i, card in enumerate(cards, start=1):
        r = by_index.get(i)
        if r is None:
            verdicts.append((True, None, f"{UNVERIFIED}: checker skipped this card"))
            continue
        correct = bool(r.get("correct", False))
        answerable = bool(r.get("answerable", True))
        try:
            score = float(r.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        issue = str(r.get("issue") or "").strip()
        visual_issue = str(r.get("visual_issue") or "").strip()
        picture_only = (card.get("visual_json") and r.get("visual_ok") is False
                        and not issue and answerable)
        if picture_only:
            correct, score = True, max(score, PASS_SCORE)
        passed = correct and answerable and score >= PASS_SCORE
        if passed:
            dropped_visual = r.get("visual_ok") is False and card.get("visual_json")
            reason = f"visual dropped: {visual_issue}".rstrip(": ") if dropped_visual else ""
        elif not correct:
            reason = f"incorrect: {issue or 'factual error'}"
        elif not answerable:
            reason = f"unanswerable: {issue or 'ambiguous question'}"
        else:
            reason = f"low quality ({score:.2f}): {issue}".rstrip(": ")
        verdicts.append((passed, score, reason))
    return verdicts


VERIFIED_COLUMNS = ("run_date", "card_uid", "passed", "score", "reason", "visual_ok")
CARD_QUERY = ("SELECT card_uid, request_id, deck, front, back, visual_json, fields_json "
              "FROM generated_cards "
              "WHERE run_date = ? {extra} ORDER BY request_id, card_uid")


def run(wh, run_date: date, profile: Profile) -> dict:
    """Check every card generated for the day, and replace its verified_cards partition."""
    cards = wh.query(CARD_QUERY.format(extra=""), [run_date])
    rows, checker_errors = check(run_date, cards, profile)
    wh.replace_partition("verified_cards", run_date, VERIFIED_COLUMNS, rows)
    return summarize(rows, checker_errors)


def check(run_date: date, cards: list[dict], profile: Profile) -> tuple[list[tuple], int]:
    """VERIFIED_COLUMNS rows for these cards, one checker call per request."""
    rows = []
    checker_errors = 0
    checker = settings.resolve_verify()

    if not profile.verify:
        rows = [(run_date, c["card_uid"], True, None, "verification disabled", None)
                for c in cards]
    else:
        batches: dict[str, list[dict]] = {}
        for c in cards:
            batches.setdefault(c["request_id"], []).append(c)
        exhausted = False
        for batch in batches.values():
            try:
                if exhausted:
                    raise llm.QuotaExhausted("daily quota already exhausted this run")
                result = llm.call_json(
                    build_prompt(batch[0]["deck"], profile.learner.level, batch),
                    cfg=checker,
                )
                data = result.data
                # Asked for {"results": [...]}; a bare list is the same answer.
                found = data if isinstance(data, list) else data.get("results", [])
                verdicts = judge(found, batch)
                pictures = visual_verdicts(found, batch)
            except llm.QuotaExhausted as e:
                # Cards pass through unverified rather than being dropped, but
                # the later batches do not queue up behind a cap that will not
                # lift until tomorrow.
                exhausted = True
                logger.warning("Checker quota exhausted; %d card(s) unverified: %s", len(batch), e)
                checker_errors += 1
                verdicts = [(True, None, f"{UNVERIFIED}: {e}")] * len(batch)
                pictures = [None] * len(batch)
            except Exception as e:
                logger.warning("Verification failed for %d card(s): %s", len(batch), e)
                checker_errors += 1
                verdicts = [(True, None, f"{UNVERIFIED}: {e}")] * len(batch)
                pictures = [None] * len(batch)
            rows.extend(
                (run_date, c["card_uid"], passed, score, reason, picture)
                for c, (passed, score, reason), picture in zip(batch, verdicts, pictures)
            )

    return rows, checker_errors


def summarize(rows: list[tuple], checker_errors: int) -> dict:
    """The stage's detail for pipeline_runs, counted from VERIFIED_COLUMNS rows."""
    dropped = [r for r in rows if not r[2]]
    return {
        "checked": len(rows),
        "passed": len(rows) - len(dropped),
        "dropped": len(dropped),
        "unverified": sum(1 for r in rows if (r[4] or "").startswith(UNVERIFIED)),
        "checker_errors": checker_errors,
        "visuals_rejected": sum(1 for r in rows if r[5] is False),
    }
