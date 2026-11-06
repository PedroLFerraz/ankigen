# Warehouse

## What it's for

The one place a run's data lives between stages. Every stage of the
[daily pipeline](daily-pipeline.md) reads its inputs from here and writes its
outputs back, so a stage can be rerun alone, in another process, or as one
Airflow task, and a retry or backfill leaves one copy of the data, never two.

It is a single DuckDB file per pipeline, `warehouse.duckdb`, with tables in
layers and partitioned by `run_date`.

## Entry points

`Warehouse` in `src/ankigen/warehouse.py`. Nothing outside the package opens
it; the stages and a few CLI commands (`plan`, `decks`, `push`, `report`,
`summary`, `reset`) do. The methods the stages use:

| Method | What it does |
|---|---|
| `replace_partition(table, run_date, columns, rows)` | Deletes the date's rows and inserts the new ones in one transaction. This is the idempotency guarantee. |
| `insert(table, columns, rows)` | Bulk insert through Arrow. `executemany` cost about 0.6 ms a row, so the 142k-row review history took minutes; Arrow loads it in under a second. |
| `query`, `scalar` | SQL in, a list of dicts or one value out. |
| `start_stage`, `finish_stage` | One `pipeline_runs` row per (date, stage). |
| `export_parquet(run_date, out_dir)` | Copies the date's partition of the stage tables to Parquet. |

## Reads / writes

`DATA_DIR/pipelines/<id>/warehouse.duckdb`, or `DATA_DIR/warehouse.duckdb`
with no pipeline set. The tables, in the order a run fills them:

| Layer | Table / view | Written by | Load pattern |
|---|---|---|---|
| raw | `raw_notes` | ingest | full snapshot per date |
| raw | `raw_revlog` | ingest | incremental, by review id |
| plan | `requests` | target | replaces that date's rows |
| stage | `generated_cards` | generate, refill | replaces that date's rows (refill replaces only its own, flagged `refill`) |
| stage | `verified_cards` | verify, refill | replaces that date's rows |
| stage | `dedup_results` | dedup, refill | replaces that date's rows |
| stage | `card_visuals` | images | replaces that date's rows |
| stage | `card_images` | images | replaces that date's rows |
| stage | `guide_sections` | guide | replaces that date's rows |
| mart | `card_outcomes` *(view)* | derived | one row per generated card: `kept`, `dropped_verify`, `dropped_duplicate` or `pending` |
| cache | `embedding_cache` | dedup | keyed by (content hash, model), kept across dates |
| ops | `pipeline_runs` | orchestrator | one row per (date, stage): status, rows, timing, detail |

Export also writes `requests`, `generated_cards`, `verified_cards` and
`dedup_results` as Parquet, to
`curated/<table>/run_date=<date>/part-0.parquet`: the layout the S3 `curated/`
layer will have.

On GitHub Actions the warehouse is not on any disk between runs; it is saved
to and restored from the Actions cache, one per pipeline
([GitHub Actions](github-actions.md)).

## Depends on

DuckDB (`duckdb>=1.0,<3`) and PyArrow. Nothing else: the warehouse knows
nothing about Anki or models.

## How to run and test it

Open it read-only while no run is writing to it:

```bash
python -c "
import duckdb
con = duckdb.connect('data/pipelines/data-platform/warehouse.duckdb', read_only=True)
con.sql('SELECT run_date, outcome, COUNT(*) FROM card_outcomes GROUP BY ALL ORDER BY 1 DESC LIMIT 10').show()"
```

`ankigen reset` deletes it, with the snapshots and output, and keeps the
embedding cache unless told not to; without `--yes` it only lists what it
would delete.

```bash
python -m pytest tests/test_warehouse.py tests/test_ingest.py
```

## Limits and known issues

- DuckDB takes one writer at a time, which is why the Airflow DAG has
  `max_active_runs=1` and the workflows run one pipeline at a time.
- New columns are added with `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` when
  the file is opened. There are no migrations beyond that.
- The module and its methods need fuller docstrings:
  [#73](https://github.com/PedroLFerraz/ankigen/issues/73).
- Moving the layers to S3 is planned in
  [#59](https://github.com/PedroLFerraz/ankigen/issues/59).
