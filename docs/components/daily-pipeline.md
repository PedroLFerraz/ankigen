# Daily pipeline

## What it's for

One run takes a date and writes that day's cards: it reads a snapshot of your
collection, decides what to teach, has a model write the cards, checks and
deduplicates them, illustrates them, writes the study guide, and exports the
result. It is ten stages, each a function of `(context, run_date)` that reads
its inputs from the [warehouse](warehouse.md) and replaces its own `run_date`
partition. Stages never pass data to each other in memory.

[ARCHITECTURE.md](../ARCHITECTURE.md) explains the design: the abstraction
levels, the CPU-pipeline analogy and the failure behaviour. This page is the
reference for what each stage does.

## Entry points

- `ankigen run`: every stage, for the next curriculum day. `--date` redoes a
  given day, `--stage` (repeatable) runs only some stages, `--dry-run` runs
  `ingest` and `target` only, and `--deck` writes for one deck instead of the
  day's plan. See the [CLI](cli.md).
- `ankigen plan`: `ingest` and `target`, then prints the plan and the prompts.
- `pipeline.run()` and `pipeline.run_stage()` in `src/ankigen/pipeline.py`,
  which the CLI, the [Airflow](airflow.md) DAG and the tests call.
- The `run-pipeline.yml` workflow, which runs `ankigen run` on GitHub's
  machines ([GitHub Actions](github-actions.md)).

The stages, in order (`STAGES` in `src/ankigen/pipeline.py`):

| Stage | Reads | Writes | Notes |
|---|---|---|---|
| **ingest** | your `collection.anki2` | `raw_notes` (daily snapshot), `raw_revlog` (incremental) | Snapshots through SQLite's backup API, so reviews still in the `-wal` file are included. A plain file copy misses them, and there's a test proving it. Anki can stay open: it holds the collection exclusively, so when the backup API cannot get a lock the snapshot falls back to copying the database with its `-wal` sidecar. Handles both schema generations and any note type, from `Front/Back` to `Frente/Verso`. Snapshots older than 14 days are deleted. |
| **target** | `raw_notes`, profile | `requests` (with the rendered prompt) | Deterministic. The same date always produces the same plan, and topics and weak cards rotate day to day; a deck with a `start:` date covers its topics once, in order. No LLM calls, so `plan` is instant. How the prompt is built is on [Card types and prompts](card-types-and-prompts.md). |
| **generate** | `requests` | `generated_cards` | The only non-deterministic stage. If one request fails, the rest of the run still goes ahead. |
| **verify** | `generated_cards` | `verified_cards` | A second LLM pass fact-checks each card, ideally on a *different* model (`VERIFY_MODEL`): a model marking its own homework shares its blind spots. On real runs it catches roughly a third of what the generator writes. If the checker is down, cards pass through tagged `ankigen::unverified` rather than being dropped. A card whose only fault is its drawn table or diagram keeps its place and loses the picture. |
| **dedup** | the above + `raw_notes` + previous runs | `dedup_results` | Compared against your **whole collection**, not just the target deck: a fact you already have in `DS::SQL` is not new because a run asked for it under Data Platform. Embedding similarity catches rephrasings; fuzzy matching runs over the nearest twenty by meaning. Cards just below the duplicate threshold are kept and tagged `ankigen::near-dup` rather than dropped unseen. |
| **refill** | requests left short by verify and dedup | more `generated_cards` (flagged `refill`), with their `verified_cards` and `dedup_results` | One more request per short batch, for exactly the cards it lost, showing the model each rejected card and why. The new cards go through the same check and dedup; one round only, so a stubborn topic cannot spend the day's free-tier requests. |
| **images** | kept cards wanting one | `card_visuals`, `card_images` | Draws the tables and diagrams the writer described, and searches the web for the rest. See [Images](images.md). |
| **guide** | kept cards | `guide_sections`, `guide_<date>.pdf` | The day's study guide. Skipped when the pipeline sets `guide_pdf: false`. See [Study guide](study-guide.md). |
| **export** | `card_outcomes` view | `.apkg`, `cards.json`, Parquet | Stable note GUIDs and stable deck and note-type IDs. |
| **report** | `pipeline_runs` + all of the above | `run_report.json` | Per-stage timings, drops with reasons, token usage and its estimated cost. |

Every stage replaces its own `run_date` partition in a single transaction.
Re-running any stage for any date is therefore safe. Retrying `dedup` or
`export` never calls the model and never changes which cards exist.

```bash
ankigen run --date 2026-09-22 --stage dedup --stage export --stage report
```

## Reads / writes

- **Reads**: the collection at `ANKI_COLLECTION_PATH` (only through a
  snapshot), the pipeline's `profile.yaml` and `pipeline.yaml`, the model and
  embedding providers, and the web (image search).
- **Writes**, under `DATA_DIR/pipelines/<id>/` (or `DATA_DIR` itself with no
  pipeline set):

```
warehouse.duckdb                   every table, see the warehouse page
raw/<date>/collection.anki2        that day's snapshot
media/                             downloaded pictures, before they are inlined
out/<date>/ankigen_<date>.apkg     import into Anki, or let push add the cards
out/<date>/cards.json              the kept cards, for the app and the status branch
out/<date>/guide_<date>.pdf        the study guide (.html without WeasyPrint)
out/<date>/run_report.json         what happened, read by `ankigen report`
curated/<table>/run_date=<date>/   Parquet, laid out the way S3 will be
```

A run never writes to your collection. Cards reach it by importing the
`.apkg`, or through [AnkiWeb sync](ankiweb-sync.md).

## Depends on

The [warehouse](warehouse.md), the [LLM providers](llm-providers.md) (for
generate, verify, refill, guide and the image check), the
[profile and pipeline files](profiles-and-pipelines.md), the
[card types and prompts](card-types-and-prompts.md), genanki for the `.apkg`,
and optionally Graphviz (diagrams) and WeasyPrint with Pango (the guide's
PDF).

## How to run and test it

```bash
ankigen plan --prompts 3     # what will be sent, no LLM calls
ankigen run --dry-run        # ingest and target only
ankigen run
ankigen report
```

The tests build real SQLite collections in both Anki schemas and replace the
model and the embeddings with deterministic fakes, so they run offline in
seconds. `tests/test_pipeline.py` covers the orchestration (idempotent reruns,
dry runs, failed stages, refill); each stage has its own file, and
[tests.yml](../../.github/workflows/tests.yml) runs them as one job per
component:

```bash
python -m pytest tests/test_pipeline.py
python -m pytest tests/test_ingest.py tests/test_targeting.py tests/test_generate_verify.py tests/test_dedup_export.py
```

## Limits and known issues

- [ARCHITECTURE.md](../ARCHITECTURE.md) on `master` still describes seven
  stages; [#71](https://github.com/PedroLFerraz/ankigen/issues/71) brings it
  up to ten.
- The stage functions have no docstrings of their own yet:
  [#73](https://github.com/PedroLFerraz/ankigen/issues/73).
- The report counts only the generate stage's tokens
  ([#58](https://github.com/PedroLFerraz/ankigen/issues/58)), and puts no cost
  on a run ([#62](https://github.com/PedroLFerraz/ankigen/issues/62)).
- Dedup embeds every card with an English model, Portuguese ones included:
  [#75](https://github.com/PedroLFerraz/ankigen/issues/75).
- If embeddings are configured but unreachable, dedup fails rather than
  quietly reporting "no duplicates". Falling back silently once replaced four
  runs of good results with that claim. Fuzzy-only matching has to be asked
  for, and only code can ask (`dedup.run(..., use_embeddings=False)`); the
  CLI has no switch for it.
