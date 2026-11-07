import json
import sqlite3
import zipfile

import numpy as np
import pytest
from conftest import RUN_DATE

from ankigen import dedup, export, generate
from ankigen.ingest import ingest
from ankigen.prices import cost_usd


def _seed(wh, cards, passed=True, request_reason="topic"):
    """Put generated cards (all passing verification) into the warehouse."""
    wh.replace_partition("requests", RUN_DATE,
                         ("run_date", "request_id", "deck", "topic", "card_type", "n", "reason", "focus", "prompt"),
                         [(RUN_DATE, "r1", cards[0][1], "t", "basic", len(cards), request_reason, None, "p")])
    wh.replace_partition("generated_cards", RUN_DATE, generate.GENERATED_COLUMNS, [
        (RUN_DATE, uid, "r1", deck, "basic", front, back,
         json.dumps({"Question": front, "Answer": back}), "", "m", 0, 0)
        for uid, deck, front, back in cards
    ])
    wh.replace_partition("verified_cards", RUN_DATE, ("run_date", "card_uid", "passed", "score", "reason"),
                         [(RUN_DATE, c[0], passed, 0.9, "") for c in cards])


# ---------------------------------------------------------------- dedup

def test_fuzzy_ratio_ignores_case_and_articles():
    assert dedup.fuzzy_ratio("The Mitochondria", "mitochondria") == 1.0
    assert dedup.fuzzy_ratio("What is Python?", "How does SQL work?") < 0.85


def test_cosine_zero_vector():
    assert dedup.cosine_similarity(np.array([1.0, 2.0]), np.zeros(2)) == 0.0


def test_duplicates_of_existing_notes_are_dropped(wh, modern_collection, tmp_path, fake_embeddings, cfg):
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [
        ("u1", "DS::SQL", "What is a CTE?", "A named subquery."),                 # fuzzy dup of note 2
        # Reworded: fuzzy ratio 0.54 misses it, embedding cosine 0.95 catches it.
        ("u2", "DS::SQL", "LATERAL join: what is it?", "A join whose right side can reference the left."),
        ("u3", "DS::SQL", "What is a covering index?", "An index containing every queried column."),
    ])
    result = dedup.run(wh, RUN_DATE)
    outcome = {r["card_uid"]: r["is_dup"] for r in wh.query("SELECT * FROM dedup_results")}
    assert outcome == {"u1": True, "u2": True, "u3": False}
    reasons = {r["card_uid"]: r["reason"] for r in wh.query("SELECT * FROM dedup_results")}
    assert reasons["u1"].startswith("fuzzy")
    assert reasons["u2"].startswith("semantic")      # and against a *subdeck* note
    assert result["semantic"] is True


def test_a_card_you_already_have_in_another_deck_still_counts(
        wh, modern_collection, tmp_path, fake_embeddings, cfg):
    """Knowledge is not new because a different deck asked for it.

    The collection's CTE card lives in DS::SQL; generating it again under
    Data Platform would just show you the same fact twice.
    """
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [("u1", "Data Platform::Fundamentals", "What is a CTE?", "A named subquery.")])
    dedup.run(wh, RUN_DATE)
    [row] = wh.query("SELECT is_dup, reason FROM dedup_results WHERE run_date = ?", [RUN_DATE])
    assert row["is_dup"] is True
    assert "in DS::SQL" in row["reason"]          # says which deck already has it


def test_low_threshold_model_catches_more(wh, modern_collection, tmp_path, fake_embeddings, cfg, monkeypatch):
    """A model whose scores run lower needs a lower threshold to catch the same pair."""
    from ankigen import dedup as d
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [("u1", "DS::SQL", "LATERAL join: what is it?",
                "A join whose right side can reference the left.")])
    monkeypatch.setattr(cfg, "semantic_threshold", 0.99)
    strict = d.run(wh, RUN_DATE)
    assert strict["threshold"] == 0.99 and strict["duplicates"] == 0   # too strict to fire
    monkeypatch.setattr(cfg, "semantic_threshold", 0.60)
    relaxed = d.run(wh, RUN_DATE)
    assert relaxed["threshold"] == 0.60 and relaxed["duplicates"] == 1


def test_duplicates_within_the_same_run(wh, modern_collection, tmp_path, fake_embeddings, cfg):
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [
        ("u1", "DS::SQL", "What does EXPLAIN ANALYZE show?", "Actual timings."),
        ("u2", "DS::SQL", "What does EXPLAIN ANALYZE show?", "Real run times."),
    ])
    dedup.run(wh, RUN_DATE)
    assert wh.scalar("SELECT COUNT(*) FROM dedup_results WHERE is_dup") == 1


def test_embedding_cache_means_second_run_embeds_nothing(wh, modern_collection, tmp_path, fake_embeddings, cfg):
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [("u1", "DS::SQL", "What is a covering index?", "Every queried column.")])
    first = dedup.run(wh, RUN_DATE)
    second = dedup.run(wh, RUN_DATE)
    assert first["embedded_now"] > 0 and second["embedded_now"] == 0


def test_embedding_outage_fails_the_stage(wh, modern_collection, tmp_path, monkeypatch, cfg):
    """Falling back silently once replaced a good partition with "no
    duplicates found" and reported success."""
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [("u1", "DS::SQL", "What is a CTE?", "x"), ("u2", "DS::SQL", "Unrelated new idea", "y")])

    def down(self, texts):
        raise ConnectionError("ollama not running")

    monkeypatch.setattr(dedup.Embedder, "_embed_batch", down)
    with pytest.raises(dedup.EmbeddingsUnavailable, match="ollama not running"):
        dedup.run(wh, RUN_DATE)


def test_fuzzy_only_matching_is_available_when_asked_for(wh, modern_collection, tmp_path, cfg):
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [("u1", "DS::SQL", "What is a CTE?", "x"), ("u2", "DS::SQL", "Unrelated new idea", "y")])
    result = dedup.run(wh, RUN_DATE, use_embeddings=False)
    assert result["semantic"] is False
    assert result["duplicates"] == 1                           # fuzzy still caught the CTE card


def test_a_near_duplicate_is_kept_but_flagged(wh, modern_collection, tmp_path, fake_embeddings, cfg):
    """Between "obviously the same" and "clearly different" the embedding model
    cannot be trusted to decide, so the card ships with a tag instead."""
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [("u1", "DS::SQL", "What is a CTE in SQL?", "A named subquery.")])
    dedup.run(wh, RUN_DATE)
    [row] = wh.query("SELECT is_dup, reason FROM dedup_results WHERE run_date = ?", [RUN_DATE])
    assert row["is_dup"] is False
    assert row["reason"].startswith(dedup.NEAR_DUP)


def test_verify_failures_are_not_deduped(wh, modern_collection, tmp_path, fake_embeddings, cfg):
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed(wh, [("u1", "DS::SQL", "Anything", "x")], passed=False)
    assert dedup.run(wh, RUN_DATE)["checked"] == 0


# ---------------------------------------------------------------- export

def _kept(wh, cards, **kw):
    _seed(wh, cards, **kw)
    wh.replace_partition("dedup_results", RUN_DATE, ("run_date", "card_uid", "is_dup", "reason", "similarity"),
                         [(RUN_DATE, c[0], False, None, None) for c in cards])


def _notes_in(apkg):
    with zipfile.ZipFile(apkg) as z:
        z.extract("collection.anki2", apkg.parent / "x")
    con = sqlite3.connect(apkg.parent / "x" / "collection.anki2")
    try:
        decks = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
        notes = con.execute("SELECT guid, flds, tags FROM notes").fetchall()
    finally:
        con.close()
    return {d["name"] for d in decks.values()}, notes


def test_export_writes_inbox_package_with_tags(wh, tmp_path):
    _kept(wh, [("u1", "DS::SQL", "What is a CTE?", "A named subquery.")], request_reason="weak_card")
    result = export.run(wh, RUN_DATE, tmp_path / "out")
    decks, notes = _notes_in(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")
    assert result["kept"] == 1
    assert "DS::SQL" in decks          # straight into the real deck, no merge step
    [(guid, flds, tags)] = notes
    assert flds == "What is a CTE?\x1fA named subquery."
    assert "ankigen::weak_card" in tags and f"ankigen::run_{RUN_DATE}" in tags


def test_near_duplicates_ship_tagged_for_review(wh, tmp_path):
    """So a judgement call the model cannot make becomes one click in Anki
    (`tag:ankigen::near-dup`) rather than a card you never see."""
    _seed(wh, [("u1", "DS::SQL", "What is a CTE?", "A named subquery.")])
    wh.replace_partition(
        "dedup_results", RUN_DATE, ("run_date", "card_uid", "is_dup", "reason", "similarity"),
        [(RUN_DATE, "u1", False, f"{dedup.NEAR_DUP} 0.86: What is a CTE in SQL?", 0.86)],
    )
    export.run(wh, RUN_DATE, tmp_path / "out")
    tags = _notes_in(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")[1][0][2]
    assert "ankigen::near-dup" in tags


def test_export_guid_is_stable(wh, tmp_path):
    _kept(wh, [("u1", "DS::SQL", "What is a CTE?", "A named subquery.")])
    export.run(wh, RUN_DATE, tmp_path / "a")
    export.run(wh, RUN_DATE, tmp_path / "b")
    guid_a = _notes_in(tmp_path / "a" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")[1][0][0]
    guid_b = _notes_in(tmp_path / "b" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")[1][0][0]
    assert guid_a == guid_b                     # reimporting updates instead of duplicating


def test_rerun_with_nothing_kept_removes_stale_package(wh, tmp_path):
    _kept(wh, [("u1", "DS::SQL", "What is a CTE?", "A named subquery.")])
    export.run(wh, RUN_DATE, tmp_path / "out")
    wh.replace_partition("dedup_results", RUN_DATE, ("run_date", "card_uid", "is_dup", "reason", "similarity"),
                         [(RUN_DATE, "u1", True, "dup", 0.99)])
    assert export.run(wh, RUN_DATE, tmp_path / "out")["apkg"] is None
    assert not (tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg").exists()


def test_unverified_cards_are_tagged(wh, tmp_path):
    _kept(wh, [("u1", "DS::SQL", "Q?", "A.")])
    wh.con.execute("UPDATE verified_cards SET reason = 'unverified: 429'")
    export.run(wh, RUN_DATE, tmp_path / "out")
    tags = _notes_in(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")[1][0][2]
    assert "ankigen::unverified" in tags


# ---------------------------------------------------------------- the same command

def _seed_commands(wh, cards):
    """Generated command cards: (uid, deck, task, command)."""
    wh.replace_partition("requests", RUN_DATE,
                         ("run_date", "request_id", "deck", "topic", "card_type", "n", "reason", "focus", "prompt"),
                         [(RUN_DATE, "r1", cards[0][1], "t", "command", len(cards), "topic", None, "p")])
    wh.replace_partition("generated_cards", RUN_DATE, generate.GENERATED_COLUMNS, [
        (RUN_DATE, uid, "r1", deck, "command", task, f"`{command}` a note",
         json.dumps({"Task": task, "Command": f"<code>{command}</code>", "Note": "a note"}),
         "", "m", 0, 0)
        for uid, deck, task, command in cards
    ])
    wh.replace_partition("verified_cards", RUN_DATE, ("run_date", "card_uid", "passed", "score", "reason"),
                         [(RUN_DATE, c[0], True, 0.9, "") for c in cards])


CURL_NOTE = (7, "Data Platform::01 Linux",
             ["Show only the response headers from https://example.com",
              "<code>curl -I https://example.com</code>"], 0, 2500, 3)


def test_the_same_command_under_another_task_is_a_duplicate(
        wh, make_collection, tmp_path, fake_embeddings, cfg):
    """Worded apart enough to pass by meaning, and in another deck: still the
    same command, and the reverse card would show it twice."""
    from conftest import SAMPLE_NOTES

    ingest(wh, RUN_DATE, make_collection([*SAMPLE_NOTES, CURL_NOTE]), tmp_path / "raw")
    _seed_commands(wh, [
        ("u1", "Data Platform::02 Networking",
         "Show only the HTTP status line and headers of a page, no body", "curl -I https://example.com"),
        ("u2", "Data Platform::02 Networking",
         "Show the status line, headers and body of a page", "curl -i https://example.com"),
    ])
    dedup.run(wh, RUN_DATE)
    rows = {r["card_uid"]: r for r in wh.query("SELECT * FROM dedup_results")}
    assert rows["u1"]["is_dup"] and rows["u1"]["reason"].startswith("same command in Data Platform::01 Linux")
    assert not rows["u2"]["is_dup"]                  # -i is not -I


def test_the_same_command_twice_in_one_run_keeps_the_first(
        wh, modern_collection, tmp_path, fake_embeddings, cfg):
    ingest(wh, RUN_DATE, modern_collection, tmp_path / "raw")
    _seed_commands(wh, [
        ("u1", "DS::SQL", "Look up the address of example.com", "dig +short example.com"),
        ("u2", "DS::SQL", "Print just the IP that a name resolves to", "dig  +short  example.com"),
    ])
    dedup.run(wh, RUN_DATE)
    outcome = {r["card_uid"]: r["is_dup"] for r in wh.query("SELECT * FROM dedup_results")}
    assert outcome == {"u1": False, "u2": True}


def test_command_key_is_the_command_as_typed():
    assert dedup.command_key("<code>du -sh *</code> | <code>sort -h</code>") == "du -sh * | sort -h"
    assert dedup.command_key("`q` (in less)") == "q (in less)"
    assert dedup.command_key("<code>N</code>") != dedup.command_key("<code>n</code>")


# ---------------------------------------------------------------- report

def test_the_report_adds_up_every_stages_tokens(wh):
    used = {
        "generate": {"writer": {"prompt": 100, "completion": 50}},
        "verify": {"checker": {"prompt": 60, "completion": 30}},
        "dedup": {},
        "refill": {"writer": {"prompt": 10, "completion": 5},
                   "checker": {"prompt": 6, "completion": 3}},
        "images": {"looker": {"prompt": 900, "completion": 9}},
        "guide": {"writer": {"prompt": 200, "completion": 400},
                  "checker": {"prompt": 80, "completion": 20}},
    }
    for stage, tokens in used.items():
        wh.start_stage(RUN_DATE, stage)
        wh.finish_stage(RUN_DATE, stage, "success", detail={"tokens": tokens})
    tokens = export.build_report(wh, RUN_DATE)["tokens"]
    assert tokens["by_stage"] == {
        "generate": {"prompt": 100, "completion": 50}, "verify": {"prompt": 60, "completion": 30},
        "refill": {"prompt": 16, "completion": 8}, "images": {"prompt": 900, "completion": 9},
        "guide": {"prompt": 280, "completion": 420},
    }
    assert tokens["by_model"] == {"writer": {"prompt": 310, "completion": 455},
                                  "checker": {"prompt": 146, "completion": 53},
                                  "looker": {"prompt": 900, "completion": 9}}
    assert tokens["total"] == {"prompt": 1356, "completion": 517}
    assert (tokens["prompt"], tokens["completion"]) == (1356, 517)


def test_the_report_prices_tokens_and_lists_a_model_with_no_price_as_unpriced(wh):
    used = {"generate": {"writer": {"prompt": 1_000_000, "completion": 500_000},
                         "mystery": {"prompt": 400, "completion": 100}},
            "verify": {"writer": {"prompt": 1_000_000, "completion": 0},
                       "x/free-one:free": {"prompt": 50, "completion": 50}}}
    for stage, tokens in used.items():
        wh.start_stage(RUN_DATE, stage)
        wh.finish_stage(RUN_DATE, stage, "success", detail={"tokens": tokens})
    prices = {"checked": "2026-01-01", "models": {"writer": {"input": 2.0, "output": 4.0}}}
    cost = cost_usd({s: {"tokens": t} for s, t in used.items()}, prices)
    assert cost["unpriced"] == ["mystery"]                 # not counted as free
    assert cost["by_model"] == {"writer": 6.0, "x/free-one:free": 0.0}
    assert cost["by_stage"] == {"generate": 4.0, "verify": 2.0}
    assert cost["llm_usd"] == 6.0 and cost["prices_checked"] == "2026-01-01"
    report_cost = export.build_report(wh, RUN_DATE)["cost"]
    assert report_cost["unpriced"] == ["mystery", "writer"]   # not in the shipped table
    assert report_cost["per_kept_card_usd"] is None           # no kept cards in this run
