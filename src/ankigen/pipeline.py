"""The daily pipeline: ingest -> target -> generate -> verify -> dedup -> refill -> images -> guide -> export -> report.

Each stage is a function of (context, run_date) that reads its inputs from the
warehouse and replaces its own `run_date` partition. That contract is what lets
the same code run three ways without changes:

    ankigen run                      all stages in one process
    ankigen run --stage verify       one stage (debugging, or one Airflow task)
    Airflow / Kubernetes             one task or pod per stage
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
    settings: Settings
    profile: Profile
    wh: Warehouse
    ad_hoc: AdHoc | None = None
    # The pipeline's own settings (pipelines/<id>/pipeline.yaml), when the run
    # is one of them rather than a bare profile.
    spec: PipelineSpec | None = None

    @property
    def raw_dir(self) -> Path:
        return self.settings.work_path / "raw"

    @property
    def out_dir(self) -> Path:
        return self.settings.work_path / "out"


def open_context(profile_path: str | None = None, cfg: Settings | None = None) -> Context:
    cfg = cfg or settings
    profile = load_profile(profile_path or cfg.profile_path())
    spec = pipelines.load(cfg.ankigen_pipeline, cfg).spec if cfg.ankigen_pipeline else None
    wh = Warehouse(cfg.work_path / "warehouse.duckdb")
    return Context(cfg, profile, wh, spec=spec)


# --------------------------------------------------------------- stages

def stage_ingest(ctx: Context, run_date: date) -> dict:
    return ingest.ingest(ctx.wh, run_date, ctx.settings.anki_collection_path, ctx.raw_dir)


def stage_target(ctx: Context, run_date: date) -> dict:
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
    return generate.run(ctx.wh, run_date, targeting.load_requests(ctx.wh, run_date))


def stage_verify(ctx: Context, run_date: date) -> dict:
    return verify.run(ctx.wh, run_date, ctx.profile)


def stage_dedup(ctx: Context, run_date: date) -> dict:
    return dedup.run(ctx.wh, run_date)


def stage_refill(ctx: Context, run_date: date) -> dict:
    return refill.run(ctx.wh, run_date, ctx.profile)


def stage_images(ctx: Context, run_date: date) -> dict:
    """Illustrate the cards that survived. Running after dedup means no image is
    ever downloaded for a card that is about to be thrown away.

    A card the model described a picture for gets that picture drawn, and is
    not searched for: the drawing is of exactly what the card says. Search is
    for the cards left over that asked for a real picture.
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
    carry their section number."""
    return guide.run(ctx.wh, run_date, ctx.profile, ctx.out_dir)


def stage_export(ctx: Context, run_date: date) -> dict:
    result = export.run(ctx.wh, run_date, ctx.out_dir, ctx.profile,
                        media_dir=ctx.settings.work_path / "media",
                        pipeline_id=ctx.settings.ankigen_pipeline)
    result["parquet_files"] = len(ctx.wh.export_parquet(run_date, ctx.settings.work_path / "curated"))
    return result


def stage_report(ctx: Context, run_date: date) -> dict:
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
    ctx.wh.start_stage(run_date, name)
    calls = llm.calls_made()
    try:
        detail = STAGES[name](ctx, run_date)
    except Exception as e:
        ctx.wh.finish_stage(run_date, name, "failed",
                            detail={"error": str(e), "llm_calls": llm.calls_made() - calls})
        raise
    # Counted per stage, so the report adds them up however the stages ran:
    # in one process, or one Airflow task each.
    detail["llm_calls"] = llm.calls_made() - calls
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
    names = list(DRY_RUN_STAGES) if dry_run else (stages or default_stages(ctx))
    unknown = [s for s in names if s not in STAGES]
    if unknown:
        raise ValueError(f"Unknown stage(s): {unknown}. Choose from {list(STAGES)}.")
    # Before ingest, not after it: a provider that cannot work at all should
    # say so in a second rather than once per request, ninety seconds in.
    if {"generate", "verify", "refill", "guide"} & set(names):
        llm.preflight()
    return {name: run_stage(ctx, name, run_date) for name in names}
