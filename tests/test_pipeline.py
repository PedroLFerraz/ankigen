"""End-to-end: the whole pipeline against a real (tiny) collection, fake LLM."""
import json
import re

import pytest
import yaml
from conftest import RUN_DATE

from ankigen import export, pipeline

SNAPSHOT_TABLES = ("requests", "generated_cards", "verified_cards", "dedup_results")


@pytest.fixture
def ctx(cfg, profile, fake_llm, fake_embeddings):
    with open(cfg.ankigen_profile, "w", encoding="utf-8") as f:
        yaml.safe_dump(profile.model_dump(), f)
    c = pipeline.open_context(cfg=cfg)
    yield c
    c.wh.close()


def _state(wh):
    return {
        t: wh.query(f"SELECT * EXCLUDE (run_date) FROM {t} WHERE run_date = ? ORDER BY ALL", [RUN_DATE])
        for t in SNAPSHOT_TABLES
    }


def test_full_run(ctx, cfg, fake_llm):
    results = pipeline.run(ctx, RUN_DATE)
    assert list(results) == list(pipeline.STAGES)
    assert results["generate"]["cards"] == results["target"]["cards_requested"] == 6
    assert results["export"]["kept"] > 0

    report = json.loads((cfg.data_path / "out" / str(RUN_DATE) / "run_report.json").read_text())
    assert all(s["status"] == "success" for s in report["stages"])      # export no longer 'running'
    assert report["tokens"]["prompt"] > 0
    assert report["apkg"].endswith(f"ankigen_{RUN_DATE}.apkg")
    assert (cfg.data_path / "curated" / "generated_cards" / f"run_date={RUN_DATE}" / "part-0.parquet").exists()



def test_every_stage_that_asks_a_model_counts_its_tokens(ctx, fake_llm):
    pipeline.run(ctx, RUN_DATE)
    tokens = export.build_report(ctx.wh, RUN_DATE)["tokens"]
    assert {"generate", "verify", "guide"} <= set(tokens["by_stage"])
    assert tokens["by_stage"]["verify"]["prompt"] > 0          # once only generate was counted
    assert set(tokens["by_model"]) == {"fake", "fake-checker"}
    for split in ("by_stage", "by_model"):
        assert tokens["total"] == {k: sum(t[k] for t in tokens[split].values())
                                   for k in ("prompt", "completion")}
    assert (tokens["prompt"], tokens["completion"]) == tuple(tokens["total"].values())
    total = tokens["total"]["prompt"] + tokens["total"]["completion"]
    assert f"**Tokens:** {total:,} (" in export.markdown_summary(ctx.wh, RUN_DATE)

def test_rerunning_a_date_is_idempotent(ctx):
    pipeline.run(ctx, RUN_DATE)
    before = _state(ctx.wh)
    pipeline.run(ctx, RUN_DATE)
    assert _state(ctx.wh) == before                    # same rows, not doubled


def test_downstream_stages_rerun_without_llm_calls(ctx, fake_llm):
    pipeline.run(ctx, RUN_DATE)
    calls = len(fake_llm.calls)
    before = _state(ctx.wh)
    pipeline.run(ctx, RUN_DATE, stages=["dedup", "export", "report"])
    assert len(fake_llm.calls) == calls
    assert _state(ctx.wh) == before


def test_dry_run_makes_no_llm_calls(ctx, fake_llm):
    results = pipeline.run(ctx, RUN_DATE, dry_run=True)
    assert list(results) == ["ingest", "target"] and fake_llm.calls == []


def test_verify_drops_are_reported(ctx, cfg, fake_llm):
    fake_llm.bad_words = ("Question 0",)
    pipeline.run(ctx, RUN_DATE)
    report = json.loads((cfg.data_path / "out" / str(RUN_DATE) / "run_report.json").read_text())
    assert report["outcomes"].get("dropped_verify", 0) > 0
    assert all(d["reason"].startswith("incorrect") for d in report["dropped"] if d["outcome"] == "dropped_verify")


UP_TO_DEDUP = ["ingest", "target", "generate", "verify", "dedup"]


@pytest.fixture
def distinct(fake_llm):
    fake_llm.distinct = True
    return fake_llm


def _kept(wh):
    return wh.scalar("SELECT COUNT(*) FROM card_outcomes WHERE run_date = ? AND outcome = 'kept'",
                     [RUN_DATE])


def test_dropped_cards_are_refilled(ctx, fake_llm, distinct):
    fake_llm.bad_words = ("Question 0",)
    results = pipeline.run(ctx, RUN_DATE, stages=UP_TO_DEDUP)
    requested = results["target"]["cards_requested"]
    assert _kept(ctx.wh) < requested

    fake_llm.bad_words = ()                            # the second attempt gets it right
    refill = pipeline.run(ctx, RUN_DATE, stages=["refill"])["refill"]
    assert refill["short"] == refill["kept"] > 0
    assert _kept(ctx.wh) == requested

    prompt = next(p for p in fake_llm.calls if "A FIRST ATTEMPT" in p)
    assert "These were rejected" in prompt and "rejected: incorrect: wrong fact" in prompt
    assert "These cards were kept" in prompt


def test_refill_asks_for_the_missing_count_only(ctx, fake_llm, distinct):
    fake_llm.bad_words = ("Question 0",)
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_DEDUP)
    calls = len(fake_llm.calls)
    pipeline.run(ctx, RUN_DATE, stages=["refill"])
    asks = [p for p in fake_llm.calls[calls:] if "A FIRST ATTEMPT" in p]
    assert asks and all(re.search(r"Write exactly 1\b", p) for p in asks)


def test_refill_is_one_round(ctx, fake_llm, distinct):
    fake_llm.bad_words = ("Question 0",)               # wrong again the second time
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_DEDUP)
    calls = len(fake_llm.calls)
    refill = pipeline.run(ctx, RUN_DATE, stages=["refill"])["refill"]
    assert refill["kept"] == 0 and refill["passed"] == 0
    # One generate and one check per short request, and no more.
    assert len(fake_llm.calls) - calls == 2 * refill["short_requests"]


def test_rerunning_refill_replaces_its_own_cards(ctx, fake_llm, distinct):
    fake_llm.bad_words = ("Question 0",)
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_DEDUP)
    fake_llm.bad_words = ()
    pipeline.run(ctx, RUN_DATE, stages=["refill"])
    before = _state(ctx.wh)
    pipeline.run(ctx, RUN_DATE, stages=["refill"])
    assert _state(ctx.wh) == before


def test_a_day_with_nothing_dropped_asks_for_nothing(ctx, fake_llm, distinct):
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_DEDUP)
    calls = len(fake_llm.calls)
    refill = pipeline.run(ctx, RUN_DATE, stages=["refill"])["refill"]
    assert refill["short"] == 0 and len(fake_llm.calls) == calls


def test_refill_needs_verify_and_dedup_first(ctx):
    pipeline.run(ctx, RUN_DATE, stages=["ingest", "target", "generate"])
    with pytest.raises(RuntimeError, match="verify and dedup"):
        pipeline.run(ctx, RUN_DATE, stages=["refill"])


def test_the_summary_says_what_the_refill_replaced(ctx, fake_llm, distinct):
    from ankigen.export import markdown_summary
    fake_llm.bad_words = ("Question 0",)
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_DEDUP)
    fake_llm.bad_words = ()
    refill = pipeline.run(ctx, RUN_DATE, stages=["refill"])["refill"]
    text = markdown_summary(ctx.wh, RUN_DATE)
    assert f"replaced by the refill" in text and "*(refill)*" in text
    assert f", {refill['kept']} replaced" in text


def test_failed_stage_is_recorded(ctx, monkeypatch):
    pipeline.run(ctx, RUN_DATE, stages=["ingest", "target"])

    def boom(*a):
        raise RuntimeError("disk full")

    monkeypatch.setitem(pipeline.STAGES, "generate", boom)
    with pytest.raises(RuntimeError):
        pipeline.run(ctx, RUN_DATE, stages=["generate"])
    row = ctx.wh.query("SELECT status, detail FROM pipeline_runs WHERE stage = 'generate'")[0]
    assert row["status"] == "failed" and "disk full" in row["detail"]


def test_target_without_ingest_fails_clearly(ctx):
    with pytest.raises(RuntimeError, match="Run the ingest stage first"):
        pipeline.run(ctx, RUN_DATE, stages=["target"])


def test_unknown_stage(ctx):
    with pytest.raises(ValueError, match="Unknown stage"):
        pipeline.run(ctx, RUN_DATE, stages=["deploy"])


def test_planning_a_day_that_has_run_leaves_its_cards_alone(ctx, monkeypatch, fake_llm,
                                                           fake_embeddings):
    """A fresh plan replaced the day's requests, and the cards generated from
    the old ones dropped out of `report` and `push` without a word."""
    from typer.testing import CliRunner

    from ankigen import cli

    pipeline.run(ctx, RUN_DATE, stages=["ingest", "target", "generate"])
    before = ctx.wh.query("SELECT request_id FROM requests WHERE run_date = ?", [RUN_DATE])
    monkeypatch.setattr(pipeline, "open_context", lambda *a, **k: ctx)
    monkeypatch.setattr(ctx.wh, "close", lambda: None)

    shown = CliRunner().invoke(cli.app, ["plan", "-d", str(RUN_DATE), "--prompts", "0"])
    assert shown.exit_code == 0 and "already run" in shown.output
    refused = CliRunner().invoke(cli.app, ["run", "-d", str(RUN_DATE), "--dry-run"])
    assert refused.exit_code != 0
    after = ctx.wh.query("SELECT request_id FROM requests WHERE run_date = ?", [RUN_DATE])
    assert after == before


# ---------------------------------------------------------------- the next day

@pytest.fixture
def cli_ctx(ctx, monkeypatch):
    from ankigen import cli

    monkeypatch.setattr(pipeline, "open_context", lambda *a, **k: ctx)
    monkeypatch.setattr(ctx.wh, "close", lambda: None)
    return cli.app


def _tag_collection(cfg, tags):
    import sqlite3

    con = sqlite3.connect(cfg.anki_collection_path)
    con.execute("UPDATE notes SET tags = ? WHERE id = 1", (f" {tags} ",))
    con.commit()
    con.close()


def test_a_plan_run_without_a_date_writes_the_next_day(ctx, cfg, cli_ctx):
    """Run by hand after the day's cards are in, it adds the next day instead
    of planning the same topics again over the day that already ran."""
    from datetime import timedelta

    from typer.testing import CliRunner

    _tag_collection(cfg, f"ankigen ankigen::run_{RUN_DATE} ankigen::topic")
    nxt = RUN_DATE + timedelta(days=1)
    shown = CliRunner().invoke(cli_ctx, ["next"])
    assert shown.exit_code == 0 and shown.output.strip() == str(nxt)

    planned = CliRunner().invoke(cli_ctx, ["run", "--dry-run"])
    assert planned.exit_code == 0, planned.output
    assert str(nxt) in planned.output
    days = {r["run_date"] for r in ctx.wh.query("SELECT DISTINCT run_date FROM requests")}
    assert days == {nxt}


def test_redoing_stages_needs_the_day(ctx, cli_ctx):
    from typer.testing import CliRunner

    # The message itself is boxed and coloured by rich, so it is not matched.
    refused = CliRunner().invoke(cli_ctx, ["run", "--stage", "images", "--stage", "export"])
    assert refused.exit_code == 2
    assert not ctx.wh.scalar("SELECT COUNT(*) FROM pipeline_runs")


def test_cards_for_one_deck_by_hand_are_marked(ctx, fake_llm):
    stages = ["ingest", "target", "generate", "verify", "dedup"]
    pipeline.run(ctx, RUN_DATE, stages=stages)
    planned = ctx.wh.query("SELECT DISTINCT ad_hoc FROM card_outcomes WHERE run_date = ?", [RUN_DATE])
    assert planned == [{"ad_hoc": False}]

    ctx.ad_hoc = pipeline.AdHoc(deck="DS::SQL", topic="joins")
    pipeline.run(ctx, RUN_DATE, stages=stages)
    by_hand = ctx.wh.query("SELECT DISTINCT ad_hoc FROM card_outcomes WHERE run_date = ?", [RUN_DATE])
    assert by_hand == [{"ad_hoc": True}]
