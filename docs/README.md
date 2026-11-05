# AnkiGen documentation

AnkiGen is a Python package (`src/ankigen/`), the pipeline folders it reads
(`pipelines/`), the GitHub workflows that run it (`.github/workflows/`), an
Android app (`android/`) and two ways to run it in containers (`Dockerfile`,
`infra/airflow/`). Each part has a page below, and every page uses the same
headings: what it's for, entry points, reads / writes, depends on, how to run
and test it, and limits and known issues.

For how the parts fit together end to end, read
[ARCHITECTURE.md](ARCHITECTURE.md). For the quick start, the
[front page](../README.md).

## The package

**[CLI](components/cli.md).** The `ankigen` command: sixteen commands for
running the pipeline, inspecting a run, syncing with AnkiWeb and the
commands that sit outside the daily run. Every other entry point (the
workflows, Airflow, Docker) ends up calling it or the functions behind it.

**[Daily pipeline](components/daily-pipeline.md).** The ten stages a run goes
through, from `ingest` to `report`, each one idempotent per `run_date`. What
each stage reads and writes, and how to rerun one.

**[Warehouse](components/warehouse.md).** The DuckDB file every stage reads
from and writes to, partitioned by `run_date`, plus the Parquet copies of
each day.

**[LLM providers and embeddings](components/llm-providers.md).** How the
writer, the checker and the fallback are chosen, the model chains that keep a
run going when a free tier runs out, and the embeddings dedup uses.

**[Profiles and pipelines](components/profiles-and-pipelines.md).** The two
files under `pipelines/<id>/`: `profile.yaml` (what to teach) and
`pipeline.yaml` (when to run, what to produce, which models, how many calls).

**[Card types and prompts](components/card-types-and-prompts.md).** The five
Anki note types the cards are written as, the card *kinds* a curriculum asks
for, and the eighteen prompt templates in `src/ankigen/prompts/`.

**[Study guide](components/study-guide.md).** The PDF written next to each
day's cards: a chapter per topic and a section per card, which each card
names under its answer.

**[Images](components/images.md).** Pictures on cards: tables and diagrams
drawn from the card itself, and pictures searched on the web and checked by a
model before they are used.

**[AnkiWeb sync](components/ankiweb-sync.md).** `pull` and `push`: how the
cards reach your collection without importing anything, and why the sync
refuses to guess.

**[Book, themes and parts](components/book-themes-parts.md).** The three
commands outside the daily run: cards from a chapter of a PDF book, a new
subject planned by the model, and command breakdowns for cards already in
Anki.

## Around the package

**[GitHub Actions](components/github-actions.md).** The eight workflows: the
hourly tick and the run it starts, the plan and theme editors, the curriculum
refresh, and the tests, image and APK builds.

**[Android app](components/android-app.md).** A phone view of the pipelines
that uses the repository as its backend: it reads the `ankigen-status`
branch, commits `pipeline.yaml` and dispatches the workflows.

**[Airflow](components/airflow.md).** A local Airflow stack that runs the same
ten stages as one task each.

**[Docker](components/docker.md).** One image with the package, every extra,
Graphviz, Pango and the Claude Code CLI, published to ghcr.io.
