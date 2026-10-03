"""ankigen_daily — one Airflow task per pipeline stage.

The stages already read their inputs from the warehouse and replace their own
`run_date` partition, so this DAG is a thin wrapper: no data passes through
XCom, retries are safe, and a backfill of an old date reproduces that date's
plan exactly.

`logical_date` is the run date, which is what makes `catchup` meaningful —
backfilling 2026-09-01 generates what that day's topic rotation would have
asked for, not today's.
"""
from __future__ import annotations

import pendulum

try:                                    # Airflow 3
    from airflow.sdk import dag, task
except ImportError:                     # Airflow 2.x
    from airflow.decorators import dag, task

# Order matters: each stage consumes what the previous one wrote.
STAGES = ["ingest", "target", "generate", "verify", "dedup", "refill", "images", "guide", "export", "report"]


@dag(
    dag_id="ankigen_daily",
    schedule="0 6 * * *",
    # Airflow reads the cron in the DAG's own timezone, so this is 6am where the
    # cards are reviewed, not 3am. It also fixes the logical date: a run started
    # at 6am UTC would be filed under the previous day locally.
    start_date=pendulum.datetime(2026, 9, 1, tz="America/Sao_Paulo"),
    catchup=False,              # set True (or use `airflow dags backfill`) to fill history
    max_active_runs=1,          # DuckDB takes one writer at a time
    default_args={
        "retries": 2,
        "retry_delay": pendulum.duration(minutes=5),
        "retry_exponential_backoff": True,
    },
    tags=["ankigen"],
    doc_md=__doc__,
)
def ankigen_daily():
    def make_task(stage: str):
        @task(task_id=stage, retries=0 if stage == "generate" else 2)
        def run_stage(stage_name: str = stage, **context) -> dict:
            """Run one stage for this DAG run's logical date.

            `generate` gets no retries on purpose: it is the only stage that
            calls the model, so a retry would spend quota to produce a
            *different* set of cards rather than repeating work.
            """
            from datetime import date

            from ankigen import pipeline

            run_date = date.fromisoformat(context["ds"])
            ctx = pipeline.open_context()
            try:
                return pipeline.run_stage(ctx, stage_name, run_date)
            finally:
                ctx.wh.close()

        return run_stage()

    previous = None
    for stage in STAGES:
        current = make_task(stage)
        if previous is not None:
            previous >> current
        previous = current


ankigen_daily()
