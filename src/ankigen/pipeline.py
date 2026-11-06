"""The daily pipeline: ingest -> target -> generate -> verify -> dedup -> refill -> images -> guide -> export -> report.

Each stage is a function of (context, run_date) that reads its inputs from the
warehouse and replaces its own `run_date` partition. That contract is what lets
the same code run three ways without changes:

    ankigen run                      all stages in one process
    ankigen run --stage verify       one stage (debugging, or one Airflow task)
    Airflow / Kubernetes             one task or pod per stage

A stage never calls another stage: whatever it needs from an earlier one, it
reads from the warehouse. Each stage's docstring says which tables it reads,
which it writes, and what a failure part of the way through leaves behind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from ankigen import (dedup, export, generate, guide, images, ingest, llm, pipelines, refill,
                     targeting, verify, visuals)
from ankigen.config import Settings, settings
from ankigen.pipelines import PipelineSpec
from ankigen.profile import Profile, load_profile
from ankigen.warehouse import Warehouse

logger = logging.getLogger(__name__)


@dataclass
class AdHoc:
    """A run asked for by hand: this deck, optionally this topic and steer."""
    deck: str
    topic: str = ""
    extra: str = ""
    n: int = 0
    kind: str = ""          # command, concept...; blank: the topic's own, else plain


@dataclass
class Context:
    """Everything a stage is given besides the date: settings, profile and
    the open warehouse. Nothing in it is state one stage leaves for another;
    that goes through the warehouse."""
    settings: Settings
    profile: Profile
    wh: Warehouse
    ad_hoc: AdHoc | None = None
    # The pipeline's own settings (pipelines/<id>/pipeline.yaml), when the run
    # is one of them rather than a bare profile.
    spec: PipelineSpec | None = None

    @property
    def raw_dir(self) -> Path:
        """Collection snapshots, a folder per run_date."""
        return self.settings.work_path / "raw"

    @property
    def out_dir(self) -> Path:
        """What a run makes for people (package, guide, report), a folder per run_date."""
        return self.settings.work_path / "out"


def open_context(profile_path: str | None = None, cfg: Settings | None = None) -> Context:
    """A context for the configured pipeline: its profile (or `profile_path`),
    its pipeline.yaml when ANKIGEN_PIPELINE names one, and its warehouse.
    The caller closes `ctx.wh`."""
    cfg = cfg or settings
    profile = load_profile(profile_path or cfg.profile_path())
    spec = pipelines.load(cfg.ankigen_pipeline, cfg).spec if cfg.ankigen_pipeline else None
    wh = Warehouse(cfg.work_path / "warehouse.duckdb")
    return Context(cfg, profile, wh, spec=spec)


# --------------------------------------------------------------- stages

def stage_ingest(ctx: Context, run_date: date) -> dict:
    """Snapshot the Anki collection and load it.

    Reads: the live collection, through a snapshot in raw_dir/<run_date>/.
    Writes: raw_notes (replaces the partition, and drops snapshots older than
    two weeks); raw_revlog (appends reviews newer than the last loaded, by id).

    The only stage that reads outside the warehouse. A collection that cannot
    be read fails before anything is written. Reviews are inserted with
    INSERT OR IGNORE, so a rerun after a failure between the two loads adds
    nothing twice.
    """
    return ingest.ingest(ctx.wh, run_date, ctx.settings.anki_collection_path, ctx.raw_dir)


def stage_target(ctx: Context, run_date: date) -> dict:
    """Decide what to generate, and render each request's prompt.

    Reads: raw_notes. Writes: requests (replaces the partition).

    Deterministic and free of model calls. A day with no ingested notes, or a
    profile that does not match the collection (a deck missing and not marked
    `new_deck`), fails before writing, leaving the day's earlier requests as
    they were.
    """
    notes = ingest.load_notes(ctx.wh, run_date)
    if not notes:
        raise RuntimeError(f"No ingested notes for {run_date}. Run the ingest stage first.")
    problems = [] if ctx.ad_hoc else ctx.profile.validate_against({n.deck for n in notes})
    if problems:
        raise ValueError("Profile does not match the collection:\n  " + "\n  ".join(problems))
    if ctx.ad_hoc:
        reqs = targeting.ad_hoc_request(
            ctx.profile, notes, run_date, ctx.ad_hoc.deck,
            topic=ctx.ad_hoc.topic, n=ctx.ad_hoc.n, extra=ctx.ad_hoc.extra,
            kind=ctx.ad_hoc.kind,
        )
    else:
        reqs = targeting.build_requests(ctx.profile, notes, run_date)
    targeting.save_requests(ctx.wh, run_date, reqs)
    return {
        "requests": len(reqs),
        "cards_requested": sum(r.cards for r in reqs),
        "by_reason": {k: sum(1 for r in reqs if r.reason == k) for k in ("topic", "weak_card", "gap")},
    }


def stage_generate(ctx: Context, run_date: date) -> dict:
    """Ask the writer's model for each request's cards.

    Reads: requests. Writes: generated_cards (replaces the partition, the
    refill stage's cards included).

    A request that fails is logged and skipped; a quota that runs out skips
    the rest. The stage fails only when no request produced a card, and it
    writes the empty partition first, so later stages do not see the last
    attempt's cards.
    """
    return generate.run(ctx.wh, run_date, targeting.load_requests(ctx.wh, run_date))


def stage_verify(ctx: Context, run_date: date) -> dict:
    """Fact-check every generated card with the checker's model.

    Reads: generated_cards. Writes: verified_cards (replaces the partition).

    A checker that fails or runs out of quota does not fail the stage: its
    cards pass, marked unverified, and the report counts them.
    """
    return verify.run(ctx.wh, run_date, ctx.profile)


def stage_dedup(ctx: Context, run_date: date) -> dict:
    """Drop the cards that passed verify but repeat one already studied.

    Reads: generated_cards and verified_cards, raw_notes, and card_outcomes
    for cards kept on earlier days; embedding_cache, which it also adds to.
    Writes: dedup_results (replaces the partition).

    Configured embeddings that fail fail the stage before it writes, so a
    fuzzy-only pass never overwrites a good result with "no duplicates".
    """
    return dedup.run(ctx.wh, run_date)


def stage_refill(ctx: Context, run_date: date) -> dict:
    """Ask once more for the cards verify and dedup dropped.

    Reads: card_outcomes and requests, and needs verify and dedup to have run.
    Writes: rows flagged `refill` in generated_cards, with their own rows in
    verified_cards and dedup_results. It does not replace whole partitions:
    it deletes the previous refill's rows and adds new ones.

    A request that fails leaves the day as it was. A rerun clears the refill
    rows before asking again, so a failure part of the way through is undone
    on the next attempt.
    """
    return refill.run(ctx.wh, run_date, ctx.profile)


def stage_images(ctx: Context, run_date: date) -> dict:
    """Illustrate the cards that survived. Running after dedup means no image is
    ever downloaded for a card that is about to be thrown away.

    A card the model described a picture for gets that picture drawn, and is
    not searched for: the drawing is of exactly what the card says. Search is
    for the cards left over that asked for a real picture.

    Reads: card_outcomes (kept cards in decks that want images). Writes:
    card_visuals and card_images (replaces both partitions), and the image
    files in work_path/media.

    One card's failure is recorded on its row (no html, or no filename, and
    why in `detail`) instead of failing the stage.
    """
    rows = ctx.wh.query(
        """SELECT card_uid, deck, image_query, visual_json, visual_ok, front, back
           FROM card_outcomes
           WHERE run_date = ? AND outcome = 'kept'
             AND (COALESCE(image_query, '') != '' OR visual_json IS NOT NULL)
           ORDER BY card_uid""",
        [run_date],
    )
    rows = [r for r in rows if ctx.profile.wants_images(r["deck"])]
    drawn, drawn_rows, failures = set(), [], []
    for r in rows:
        visual = visuals.parse(r["visual_json"])
        if not visual:
            continue
        kind = "table" if "table" in visual else "diagram"
        if r["visual_ok"] is False:
            drawn_rows.append((run_date, r["card_uid"], kind, None, "the checker found it wrong"))
            continue
        try:
            drawn_rows.append((run_date, r["card_uid"], kind, visuals.to_html(visual), ""))
            drawn.add(r["card_uid"])
        except visuals.NotDrawable as e:
            failures.append(str(e))
            drawn_rows.append((run_date, r["card_uid"], kind, None, str(e)))
    ctx.wh.replace_partition(
        "card_visuals", run_date, ("run_date", "card_uid", "kind", "html", "detail"), drawn_rows)

    jobs = []
    for r in rows:
        if r["card_uid"] in drawn or not (r["image_query"] or "").strip():
            continue
        context = ctx.profile.image_context_for(r["deck"])
        jobs.append((r["card_uid"], images.with_context(r["image_query"], context),
                     f"{r['front']} — {r['back'] or ''}", context))
    # Someone has to look at the picture: a page whose title matches the query
    # routinely carries an image that has nothing to do with it. The checker's
    # models do it, not the writer's, so looking at pictures does not eat the
    # allowance that writes the cards.
    checker = ctx.settings.resolve_verify()
    verifier = (
        (lambda blob, card, query: llm.check_image(blob, card, query, cfg=checker))
        if ctx.profile.verify_images else None
    )
    results = images.fetch_many(jobs, ctx.settings.work_path / "media", verifier=verifier)

    ctx.wh.replace_partition(
        "card_images", run_date,
        ("run_date", "card_uid", "query", "filename", "source", "url", "detail"),
        [(run_date, r.card_uid, r.query, r.filename, r.source, r.url, r.detail) for r in results],
    )
    found = [r for r in results if r.found]
    return {
        "drawn": len(drawn),
        "not_drawn": failures,
        "wanted": len(jobs),
        "found": len(found),
        "unchecked": sum(1 for r in found if (r.detail or "").startswith(images.UNCHECKED)),
        "missing": [r.query for r in results if not r.found],
    }


def stage_guide(ctx: Context, run_date: date) -> dict:
    """A study guide explaining the kept cards in depth. After images, so it
    can show the drawn tables and diagrams; before export, so the notes can
    carry their section number.

    Reads: card_outcomes and requests. Writes: guide_sections (replaces the
    partition), and out_dir/<run_date>/guide_<run_date>.html and .pdf.

    Model failures do not fail the stage: a chapter that could not be written
    or checked is marked so in the guide, and its rows say why.
    """
    return guide.run(ctx.wh, run_date, ctx.profile, ctx.out_dir)


def stage_export(ctx: Context, run_date: date) -> dict:
    """Package the kept cards for Anki.

    Reads: card_outcomes, and the images in work_path/media. Writes no table:
    out_dir/<run_date>/ankigen_<run_date>.apkg and cards.json, and the day's
    requests, generated_cards, verified_cards and dedup_results as Parquet
    under work_path/curated.

    The previous package is deleted first, so a rerun that keeps nothing
    leaves no stale .apkg to import.
    """
    result = export.run(ctx.wh, run_date, ctx.out_dir, ctx.profile,
                        media_dir=ctx.settings.work_path / "media",
                        pipeline_id=ctx.settings.ankigen_pipeline)
    result["parquet_files"] = len(ctx.wh.export_parquet(run_date, ctx.settings.work_path / "curated"))
    return result


def stage_report(ctx: Context, run_date: date) -> dict:
    """Write the run report.

    Reads: pipeline_runs, card_outcomes and card_images. Writes no table:
    out_dir/<run_date>/run_report.json. Last, so every other stage's status
    and timing is final.
    """
    return export.write_report(ctx.wh, run_date, ctx.out_dir)


STAGES: dict[str, Callable[[Context, date], dict]] = {
    "ingest": stage_ingest,
    "target": stage_target,
    "generate": stage_generate,
    "verify": stage_verify,
    "dedup": stage_dedup,
    "refill": stage_refill,
    "images": stage_images,
    "guide": stage_guide,
    "export": stage_export,
    "report": stage_report,
}
# Everything up to (not including) the first LLM call.
DRY_RUN_STAGES = ("ingest", "target")


def run_stage(ctx: Context, name: str, run_date: date) -> dict:
    """Run one stage and record it in pipeline_runs: "success" with its
    detail and model calls, or "failed" with the error, which is re-raised."""
    ctx.wh.start_stage(run_date, name)
    calls, tokens = llm.calls_made(), llm.tokens_used()
    try:
        detail = STAGES[name](ctx, run_date)
    except Exception as e:
        ctx.wh.finish_stage(run_date, name, "failed",
                            detail={"error": str(e), "llm_calls": llm.calls_made() - calls,
                                    "tokens": llm.tokens_since(tokens)})
        raise
    # Counted per stage, so the report adds them up however the stages ran:
    # in one process, or one Airflow task each.
    detail["llm_calls"] = llm.calls_made() - calls
    detail["tokens"] = llm.tokens_since(tokens)
    rows = next((detail[k] for k in ("sections", "cards", "notes", "requests", "checked", "kept") if k in detail), 0)
    ctx.wh.finish_stage(run_date, name, "success", rows_out=rows if isinstance(rows, int) else 0, detail=detail)
    logger.info("stage %-8s ok  %s", name, detail)
    return detail


def next_day(ctx: Context, today: date) -> date:
    """The curriculum day the next plan run writes: the one after the last
    day this pipeline's decks have cards for (see ingest.next_run_date)."""
    decks = [t.deck for t in ctx.profile.decks]
    done: set[date] = set()
    if ctx.spec and not ctx.spec.outputs.push_to_ankiweb:
        # Its cards never reach the collection, so the warehouse says which
        # days it wrote. One that pushes trusts the collection instead: a day
        # whose push failed is not done, and the next run writes it again.
        done = {r["run_date"] for r in ctx.wh.query(
            "SELECT DISTINCT run_date FROM card_outcomes WHERE outcome = 'kept' AND NOT ad_hoc")}
    return ingest.next_run_date(
        ctx.settings.anki_collection_path, ctx.raw_dir, today,
        within=lambda deck: any(targeting.in_deck(deck, target) for target in decks),
        also_done=done)


def default_stages(ctx: Context) -> list[str]:
    """Every stage, less the guide for a pipeline that asked for no PDF."""
    names = list(STAGES)
    if ctx.spec and not ctx.spec.outputs.guide_pdf:
        names.remove("guide")
    return names


def run(ctx: Context, run_date: date, stages: list[str] | None = None, dry_run: bool = False) -> dict:
    """Run `stages` (default: `default_stages`) in order, stopping at the
    first that fails. A dry run is ingest and target only, with no model call."""
    names = list(DRY_RUN_STAGES) if dry_run else (stages or default_stages(ctx))
    unknown = [s for s in names if s not in STAGES]
    if unknown:
        raise ValueError(f"Unknown stage(s): {unknown}. Choose from {list(STAGES)}.")
    # Before ingest, not after it: a provider that cannot work at all should
    # say so in a second rather than once per request, ninety seconds in.
    if {"generate", "verify", "refill", "guide"} & set(names):
        llm.preflight()
    return {name: run_stage(ctx, name, run_date) for name in names}
