"""The study guide: a chapter per request, a section per kept card, and the
reference each card carries to its section."""
import json
import sqlite3
import sys
import zipfile

import pytest
import yaml
from conftest import RUN_DATE

from ankigen import export, guide, llm, pipeline

UP_TO_IMAGES = ["ingest", "target", "generate", "verify", "dedup", "refill", "images"]


@pytest.fixture
def ctx(cfg, profile, fake_llm, fake_embeddings):
    with open(cfg.ankigen_profile, "w", encoding="utf-8") as f:
        yaml.safe_dump(profile.model_dump(), f)
    c = pipeline.open_context(cfg=cfg)
    yield c
    c.wh.close()


def _sections(wh):
    return wh.query("SELECT * EXCLUDE (run_date) FROM guide_sections WHERE run_date = ? "
                    "ORDER BY ref, kind", [RUN_DATE])


def _kept(wh):
    return wh.query("SELECT card_uid, guide_ref FROM card_outcomes "
                    "WHERE run_date = ? AND outcome = 'kept'", [RUN_DATE])


def test_the_guide_runs_between_images_and_export():
    names = list(pipeline.STAGES)
    assert names.index("images") < names.index("guide") < names.index("export")


def test_every_kept_card_gets_a_section_and_a_reference(ctx, cfg):
    detail = pipeline.run(ctx, RUN_DATE)["guide"]
    kept = _kept(ctx.wh)
    assert kept and all(k["guide_ref"] for k in kept)
    refs = [k["guide_ref"] for k in kept]
    assert len(set(refs)) == len(refs)

    sections = _sections(ctx.wh)
    primers = [s for s in sections if s["kind"] == "primer"]
    assert detail["chapters"] == len(primers) and detail["sections"] == len(sections)
    assert all(s["status"] == "checked" for s in sections)
    assert all(s["markdown"] for s in sections)
    # A chapter number for each request; its cards are numbered under it.
    assert {r.split(".")[0] for r in refs} == {p["ref"] for p in primers}

    page = (cfg.data_path / "out" / str(RUN_DATE) / f"guide_{RUN_DATE}.html").read_text()
    assert "Study guide" in page and "§1.1" in page and "<code>" in page


def test_the_numbering_is_settled_before_the_model_and_stable(ctx, fake_llm):
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_IMAGES)
    pipeline.run(ctx, RUN_DATE, stages=["guide"])
    first = [(s["card_uid"], s["ref"]) for s in _sections(ctx.wh)]
    pipeline.run(ctx, RUN_DATE, stages=["guide"])
    assert [(s["card_uid"], s["ref"]) for s in _sections(ctx.wh)] == first   # replaced, not doubled


def test_chapters_follow_the_profile_s_deck_order(ctx, profile):
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_IMAGES)
    chapters = guide.outline(ctx.wh, RUN_DATE, ctx.profile)
    order = [d.deck for d in profile.decks]
    decks = [ch.deck for ch in chapters]
    assert decks == sorted(decks, key=order.index)
    assert [ch.number for ch in chapters] == list(range(1, len(chapters) + 1))


def test_a_disputed_section_stays_in_with_the_dispute(ctx, cfg, fake_llm):
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_IMAGES)
    fake_llm.bad_words = ("Question 0",)
    detail = pipeline.run(ctx, RUN_DATE, stages=["guide"])["guide"]
    disputed = [s for s in _sections(ctx.wh) if s["status"] == "disputed"]
    assert disputed and detail["disputed"] == len(disputed)
    assert all(s["markdown"] and s["issue"] == "wrong flag" for s in disputed)
    page = (cfg.data_path / "out" / str(RUN_DATE) / f"guide_{RUN_DATE}.html").read_text()
    assert "The checker disputes this section</strong>: wrong flag" in page


def test_a_checker_that_fails_leaves_the_guide_unchecked(ctx, fake_llm):
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_IMAGES)
    fake_llm.fail_guide_check = True
    detail = pipeline.run(ctx, RUN_DATE, stages=["guide"])["guide"]
    sections = _sections(ctx.wh)
    assert detail["unchecked"] == len(sections)
    assert all(s["markdown"] and s["issue"].startswith("unchecked: ") for s in sections)


def test_a_spent_allowance_costs_the_guide_not_the_day(ctx, fake_llm):
    pipeline.run(ctx, RUN_DATE, stages=UP_TO_IMAGES)
    fake_llm.fail_guide = llm.QuotaExhausted("out of quota for today")
    calls = len(fake_llm.calls)
    results = pipeline.run(ctx, RUN_DATE, stages=["guide", "export", "report"])
    # One call finds out; the rest of the chapters do not ask again.
    assert len(fake_llm.calls) == calls + 1
    assert results["guide"]["missing"] == results["guide"]["sections"]
    assert all(k["guide_ref"] for k in _kept(ctx.wh))       # the references still hold
    assert results["export"]["kept"] > 0


def test_without_weasyprint_the_guide_is_html_only(ctx, cfg, monkeypatch):
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    detail = pipeline.run(ctx, RUN_DATE)["guide"]
    assert detail["pdf"] is None and detail["html"]
    assert not (cfg.data_path / "out" / str(RUN_DATE) / f"guide_{RUN_DATE}.pdf").exists()


def test_the_guide_is_a_pdf(ctx, cfg):
    pytest.importorskip("weasyprint")
    detail = pipeline.run(ctx, RUN_DATE)["guide"]
    pdf = cfg.data_path / "out" / str(RUN_DATE) / f"guide_{RUN_DATE}.pdf"
    assert detail["pdf"] == str(pdf)
    assert pdf.read_bytes().startswith(b"%PDF")


def test_a_day_with_nothing_kept_leaves_no_guide_behind(ctx, cfg):
    pipeline.run(ctx, RUN_DATE)
    ctx.wh.con.execute("UPDATE dedup_results SET is_dup = TRUE WHERE run_date = ?", [RUN_DATE])
    detail = pipeline.run(ctx, RUN_DATE, stages=["guide"])["guide"]
    assert detail["sections"] == 0 and detail["html"] is None
    assert not (cfg.data_path / "out" / str(RUN_DATE) / f"guide_{RUN_DATE}.html").exists()


def test_the_package_carries_each_card_s_reference(ctx, cfg):
    pipeline.run(ctx, RUN_DATE)
    apkg = cfg.data_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg"
    with zipfile.ZipFile(apkg) as z:
        z.extract("collection.anki2", cfg.data_path)
    con = sqlite3.connect(cfg.data_path / "collection.anki2")
    fields = [row[0] for row in con.execute("SELECT flds FROM notes")]
    con.close()
    assert fields and all(f"Guide {RUN_DATE} · §" in f for f in fields)


def test_the_run_summary_mentions_the_guide(ctx):
    pipeline.run(ctx, RUN_DATE)
    summary = export.markdown_summary(ctx.wh, RUN_DATE)
    assert "**Study guide:**" in summary and f"guide_{RUN_DATE}.pdf" in summary


@pytest.mark.parametrize("card_type, field", [
    ("basic", "Answer"), ("command", "Note"), ("cloze", "Extra"), ("detailed", "Explanation"),
])
def test_the_reference_goes_on_the_answer_side(card_type, field):
    values = {field: "text"}
    placed = export.with_guide_ref(card_type, values, RUN_DATE, "2.3")
    assert placed[field].startswith("text") and f"Guide {RUN_DATE} · §2.3" in placed[field]
    assert values == {field: "text"}                       # a copy, not an edit
    assert export.with_guide_ref(card_type, values, RUN_DATE, None) == values


def test_a_card_s_section_lists_its_command_parts():
    card = {"front": "List with sizes", "back": "`ls -lh`", "ref": "1.1",
            "fields_json": json.dumps({"_parts": "`-h` = human-readable sizes"}),
            "visual_html": None}
    assert "human-readable sizes" in guide._card_box(card)
    assert "<code>ls -lh</code>" in guide._card_box(card)


def test_a_build_card_s_context_stays_text_and_its_code_is_code():
    card = {"front": "~/.bashrc: make `ll` run `ls -lh`\n[1] ll='ls -lh'", "back": "[1] alias",
            "ref": "1.1", "fields_json": "{}", "visual_html": None}
    box = guide._card_box(card)
    assert "make <code>ll</code> run <code>ls -lh</code>" in box
    assert "<pre><code>[1] ll='ls -lh'</code></pre>" in box
