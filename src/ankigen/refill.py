"""Refill stage: replace the cards that verify and dedup dropped.

A request asks for N cards, and each one the checker rejects or dedup
recognises leaves the day that much short. This stage asks again for exactly
the missing number, showing the model what was rejected and why, then puts the
new cards through the same verify and dedup as the rest of the day.

One round only. A topic that keeps failing should not spend the free tier's
daily requests on it, and a second round would mostly repeat the first.

Idempotent like every stage: its cards are flagged `refill`, and a rerun
removes them, with their verify and dedup rows, before asking again.
"""
from __future__ import annotations

import logging
import re
from datetime import date

from ankigen import dedup, generate, llm, verify
from ankigen.profile import Profile

logger = logging.getLogger(__name__)


def clear(wh, run_date: date) -> None:
    """Remove what an earlier refill of this day wrote."""
    with wh.transaction() as con:
        for table in ("verified_cards", "dedup_results"):
            con.execute(
                f"DELETE FROM {table} WHERE run_date = ? AND card_uid IN "
                "(SELECT card_uid FROM generated_cards WHERE run_date = ? AND refill)",
                [run_date, run_date],
            )
        con.execute("DELETE FROM generated_cards WHERE run_date = ? AND refill", [run_date])


def prompt_for(prompt: str, kept: list[dict], rejected: list[dict], n: int) -> str:
    """The request's own prompt asking for the missing count, plus what the
    first attempt produced. The count is rewritten in place rather than
    restated at the end: two different counts in one prompt is a coin toss."""
    prompt = re.sub(r"Write exactly \d+", f"Write exactly {n}", prompt, count=1)
    lines = [prompt.rstrip(), "", "A FIRST ATTEMPT AT THIS BATCH"]
    if kept:
        lines += ["These cards were kept. Do not repeat them:"]
        lines += [f"- {c['front']}" for c in kept]
    if rejected:
        lines += ["These were rejected. Do not write them again, and do not make the "
                  "mistake that sank each one:"]
        lines += [f"- Q: {c['front']}\n  rejected: {c['why']}" for c in rejected]
    lines += ["", "The new cards must be on the same subject and different from every "
                  "card above."]
    return "\n".join(lines)


def shortfalls(wh, run_date: date) -> list[dict]:
    """The day's requests that ended with fewer kept cards than they asked
    for, each rewritten to ask for the difference."""
    cards = wh.query(
        """SELECT request_id, front, outcome,
                  CASE outcome WHEN 'dropped_verify' THEN verify_reason ELSE dup_reason END AS why
           FROM card_outcomes WHERE run_date = ? AND NOT refill
           ORDER BY request_id, front""",
        [run_date],
    )
    if any(c["outcome"] == "pending" for c in cards):
        raise RuntimeError(f"Some of {run_date}'s cards have not been through verify and "
                           "dedup yet. Run those stages first.")
    by_request: dict[str, list[dict]] = {}
    for c in cards:
        by_request.setdefault(c["request_id"], []).append(c)

    todo = []
    for req in wh.query("SELECT * FROM requests WHERE run_date = ? ORDER BY deck, request_id",
                        [run_date]):
        mine = by_request.get(req["request_id"], [])
        if not mine:
            # The request failed outright; asking again fails the same way.
            continue
        kept = [c for c in mine if c["outcome"] == "kept"]
        short = req["n"] - len(kept)
        if short <= 0:
            continue
        rejected = [c for c in mine if c["outcome"].startswith("dropped_")]
        todo.append({**req, "n": short,
                     "prompt": prompt_for(req["prompt"], kept, rejected, short)})
    return todo


def run(wh, run_date: date, profile: Profile, use_embeddings: bool = True) -> dict:
    clear(wh, run_date)
    todo = shortfalls(wh, run_date)
    detail = {"short": sum(r["n"] for r in todo), "short_requests": len(todo), "generated": 0,
              "passed": 0, "kept": 0, "failed_requests": [],
              "prompt_tokens": 0, "completion_tokens": 0}
    if not todo:
        return detail

    existing = {r["card_uid"] for r in wh.query(
        "SELECT card_uid FROM generated_cards WHERE run_date = ?", [run_date])}
    rows = []
    for req in todo:
        try:
            cards, result, p_tok, c_tok = generate.generate_for_request(req)
        except llm.QuotaExhausted as e:
            logger.warning("Daily quota exhausted; refill stops here: %s", e)
            detail["failed_requests"].append({"request_id": req["request_id"], "error": str(e)})
            break
        except Exception as e:  # a refill that fails leaves the day as it was
            logger.warning("Refill for %s (%s) failed: %s", req["request_id"], req["deck"], e)
            detail["failed_requests"].append({"request_id": req["request_id"], "error": str(e)})
            continue
        detail["prompt_tokens"] += p_tok
        detail["completion_tokens"] += c_tok
        for row in generate.card_rows(run_date, req, cards, result, p_tok, c_tok):
            # A card identical to one already written today has its id, too.
            if row[1] not in existing:
                existing.add(row[1])
                rows.append(row + (True,))
    wh.insert("generated_cards", generate.WRITTEN_COLUMNS + ("refill",), rows)
    detail["generated"] = len(rows)
    if not rows:
        return detail

    new = wh.query(verify.CARD_QUERY.format(extra="AND refill"), [run_date])
    checked = verify.check(run_date, new, profile)
    wh.insert("verified_cards", verify.VERIFIED_COLUMNS, checked)
    detail["passed"] = verify.summarize(checked)["passed"]
    detail["kept"] = dedup.run(wh, run_date, use_embeddings=use_embeddings, refill=True)["kept"]
    return detail
