"""DuckDB warehouse: every stage reads its inputs from here and writes its outputs back.

Tables are partitioned by `run_date`. A stage replaces its own partition in a
single transaction, so re-running any stage for any date is safe — which is what
makes Airflow retries and backfills work without special handling.

Layers:
    raw_*        snapshots and incremental loads from the Anki collection
    requests     what the targeting stage decided to generate
    generated_*  / verified_* / dedup_*   one table per downstream stage
    card_outcomes  view joining them into one row per generated card
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Sequence

import duckdb
import pyarrow as pa

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_notes (
    run_date      DATE    NOT NULL,
    note_id       BIGINT  NOT NULL,
    deck          VARCHAR NOT NULL,
    notetype      VARCHAR,
    front         VARCHAR,
    back          VARCHAR,
    tags          VARCHAR,
    is_cloze      BOOLEAN,
    content_hash  VARCHAR NOT NULL,
    lapses        INTEGER,
    ease          INTEGER,
    reps          INTEGER,
    interval_days INTEGER,
    queue         INTEGER,
    modified_at   BIGINT
);

-- Review history is append-only in Anki, so it loads incrementally by id.
CREATE TABLE IF NOT EXISTS raw_revlog (
    review_id   BIGINT PRIMARY KEY,
    card_id     BIGINT,
    ease        INTEGER,
    interval    INTEGER,
    time_ms     INTEGER,
    review_type INTEGER,
    loaded_on   DATE
);

CREATE TABLE IF NOT EXISTS requests (
    run_date    DATE    NOT NULL,
    request_id  VARCHAR NOT NULL,
    deck        VARCHAR NOT NULL,
    topic       VARCHAR,
    card_type   VARCHAR NOT NULL,
    n           INTEGER NOT NULL,
    reason      VARCHAR NOT NULL,
    focus       VARCHAR,
    prompt      VARCHAR NOT NULL,
    kind        VARCHAR,
    level       INTEGER,
    ad_hoc      BOOLEAN
);

CREATE TABLE IF NOT EXISTS generated_cards (
    run_date    DATE    NOT NULL,
    card_uid    VARCHAR NOT NULL,
    request_id  VARCHAR NOT NULL,
    deck        VARCHAR NOT NULL,
    card_type   VARCHAR NOT NULL,
    front       VARCHAR NOT NULL,
    back        VARCHAR NOT NULL,
    fields_json VARCHAR NOT NULL,
    image_query VARCHAR,
    model       VARCHAR,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    visual_json VARCHAR,
    -- Written by the refill stage, to replace cards the checks dropped.
    refill      BOOLEAN
);

CREATE TABLE IF NOT EXISTS verified_cards (
    run_date  DATE    NOT NULL,
    card_uid  VARCHAR NOT NULL,
    passed    BOOLEAN NOT NULL,
    score     DOUBLE,
    reason    VARCHAR,
    visual_ok BOOLEAN
);

CREATE TABLE IF NOT EXISTS dedup_results (
    run_date   DATE    NOT NULL,
    card_uid   VARCHAR NOT NULL,
    is_dup     BOOLEAN NOT NULL,
    reason     VARCHAR,
    similarity DOUBLE
);

CREATE TABLE IF NOT EXISTS card_images (
    run_date  DATE    NOT NULL,
    card_uid  VARCHAR NOT NULL,
    query     VARCHAR,
    filename  VARCHAR,
    source    VARCHAR,
    url       VARCHAR,
    detail    VARCHAR
);

-- Pictures drawn from the card itself (see visuals.py), as the HTML that goes
-- in the note.
CREATE TABLE IF NOT EXISTS card_visuals (
    run_date  DATE    NOT NULL,
    card_uid  VARCHAR NOT NULL,
    kind      VARCHAR,
    html      VARCHAR,
    detail    VARCHAR
);

-- The day's study guide (guide.py): a chapter opening per request, a section
-- per kept card. `ref` is what the card carries: "2" for a chapter, "2.3" for
-- its third card.
CREATE TABLE IF NOT EXISTS guide_sections (
    run_date   DATE    NOT NULL,
    card_uid   VARCHAR,
    request_id VARCHAR NOT NULL,
    ref        VARCHAR NOT NULL,
    kind       VARCHAR NOT NULL,
    markdown   VARCHAR,
    status     VARCHAR NOT NULL,
    issue      VARCHAR,
    model      VARCHAR
);

CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash VARCHAR NOT NULL,
    model        VARCHAR NOT NULL,
    vector       FLOAT[] NOT NULL,
    PRIMARY KEY (content_hash, model)
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_date    DATE      NOT NULL,
    stage       VARCHAR   NOT NULL,
    status      VARCHAR   NOT NULL,
    started_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    rows_out    INTEGER,
    detail      VARCHAR
);

CREATE OR REPLACE VIEW card_outcomes AS
SELECT
    g.run_date, g.card_uid, g.request_id, g.deck, g.card_type, g.front, g.back,
    g.fields_json, r.reason AS request_reason, r.topic, COALESCE(r.ad_hoc, FALSE) AS ad_hoc,
    g.image_query, g.visual_json, COALESCE(g.refill, FALSE) AS refill,
    v.passed AS verify_passed, v.score AS verify_score, v.reason AS verify_reason,
    v.visual_ok,
    cv.kind AS visual_kind, cv.html AS visual_html,
    d.is_dup, d.reason AS dup_reason,
    i.filename AS image_filename, i.source AS image_source,
    gs.guide_ref,
    CASE
        WHEN v.passed IS FALSE THEN 'dropped_verify'
        WHEN d.is_dup IS TRUE  THEN 'dropped_duplicate'
        WHEN d.is_dup IS FALSE THEN 'kept'
        ELSE 'pending'
    END AS outcome
FROM generated_cards g
JOIN requests r USING (run_date, request_id)
LEFT JOIN verified_cards v USING (run_date, card_uid)
LEFT JOIN dedup_results d USING (run_date, card_uid)
LEFT JOIN card_images   i USING (run_date, card_uid)
LEFT JOIN card_visuals  cv USING (run_date, card_uid)
LEFT JOIN (SELECT run_date, card_uid, ref AS guide_ref FROM guide_sections
           WHERE kind = 'card') gs USING (run_date, card_uid);
"""


class Warehouse:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path))
        # Columns added after a database already existed. The view is recreated
        # by SCHEMA below, so it must run after the table has caught up.
        self.con.execute(
            "ALTER TABLE IF EXISTS generated_cards ADD COLUMN IF NOT EXISTS image_query VARCHAR"
        )
        self.con.execute(
            "ALTER TABLE IF EXISTS card_images ADD COLUMN IF NOT EXISTS url VARCHAR"
        )
        self.con.execute(
            "ALTER TABLE IF EXISTS generated_cards ADD COLUMN IF NOT EXISTS visual_json VARCHAR"
        )
        self.con.execute(
            "ALTER TABLE IF EXISTS verified_cards ADD COLUMN IF NOT EXISTS visual_ok BOOLEAN"
        )
        self.con.execute(
            "ALTER TABLE IF EXISTS generated_cards ADD COLUMN IF NOT EXISTS refill BOOLEAN"
        )
        for column in ("kind VARCHAR", "level INTEGER", "ad_hoc BOOLEAN"):
            self.con.execute(f"ALTER TABLE IF EXISTS requests ADD COLUMN IF NOT EXISTS {column}")
        self.con.execute(SCHEMA)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        self.con.execute("BEGIN TRANSACTION")
        try:
            yield self.con
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        self.con.execute("COMMIT")

    def insert(self, table: str, columns: Sequence[str], rows: Sequence[tuple],
               or_ignore: bool = False, con=None) -> int:
        """Bulk insert through Arrow.

        DuckDB's executemany inserts row by row (~0.6 ms/row), which turns a
        150k-row review history into minutes. Arrow does the same in under a second.
        """
        if not rows:
            return 0
        con = con or self.con
        arrow = pa.table({c: list(vals) for c, vals in zip(columns, zip(*rows))})
        con.register("_bulk_in", arrow)
        try:
            verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
            con.execute(f"{verb} INTO {table} ({', '.join(columns)}) SELECT * FROM _bulk_in")
        finally:
            con.unregister("_bulk_in")
        return len(rows)

    def replace_partition(
        self, table: str, run_date: date, columns: Sequence[str], rows: Sequence[tuple]
    ) -> int:
        """Atomically swap one run_date's rows. The heart of idempotency."""
        with self.transaction() as con:
            con.execute(f"DELETE FROM {table} WHERE run_date = ?", [run_date])
            self.insert(table, columns, rows, con=con)
        return len(rows)

    def query(self, sql: str, params: Sequence | None = None) -> list[dict]:
        cur = self.con.execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def scalar(self, sql: str, params: Sequence | None = None):
        row = self.con.execute(sql, params or []).fetchone()
        return row[0] if row else None

    # --- run bookkeeping ---

    def start_stage(self, run_date: date, stage: str) -> datetime:
        started = datetime.now()
        self.con.execute(
            "DELETE FROM pipeline_runs WHERE run_date = ? AND stage = ?", [run_date, stage]
        )
        self.con.execute(
            "INSERT INTO pipeline_runs (run_date, stage, status, started_at) VALUES (?, ?, 'running', ?)",
            [run_date, stage, started],
        )
        return started

    def finish_stage(
        self, run_date: date, stage: str, status: str, rows_out: int = 0, detail: dict | None = None
    ) -> None:
        self.con.execute(
            """UPDATE pipeline_runs
               SET status = ?, finished_at = ?, rows_out = ?, detail = ?
               WHERE run_date = ? AND stage = ?""",
            [status, datetime.now(), rows_out, json.dumps(detail or {}, default=str), run_date, stage],
        )

    def export_parquet(self, run_date: date, out_dir: str | Path) -> list[Path]:
        """Write this run's partition of each table as Parquet — the future S3 layout."""
        out_dir = Path(out_dir)
        written = []
        for table in ("requests", "generated_cards", "verified_cards", "dedup_results"):
            target = out_dir / table / f"run_date={run_date}" / "part-0.parquet"
            target.parent.mkdir(parents=True, exist_ok=True)
            # COPY takes no bound parameters; run_date is a datetime.date, never free text.
            self.con.execute(
                f"COPY (SELECT * FROM {table} WHERE run_date = DATE '{run_date.isoformat()}') "
                f"TO '{target.as_posix()}' (FORMAT PARQUET)"
            )
            written.append(target)
        return written
