<p align="center">
  <img src="docs/brand/icon.svg" width="96" alt="">
</p>

<h1 align="center">AnkiGen</h1>

<p align="center">
  A daily LLM pipeline that writes Anki cards that fit the deck you already study.
</p>

<p align="center">
  <a href="https://github.com/PedroLFerraz/ankigen/actions/workflows/tests.yml"><img src="https://github.com/PedroLFerraz/ankigen/actions/workflows/tests.yml/badge.svg" alt="tests"></a>
  <a href="https://github.com/PedroLFerraz/ankigen/actions/workflows/tick.yml"><img src="https://github.com/PedroLFerraz/ankigen/actions/workflows/tick.yml/badge.svg" alt="tick"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB" alt="Python 3.10+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-lightgrey" alt="MIT license"></a>
</p>


A daily batch pipeline that reads your Anki collection and writes new cards
that fit it. The cards match the phrasing of cards you already study, avoid what
you already know, re-teach what you keep forgetting, and are fact-checked before
they reach you. You steer it with one YAML profile.

<p align="center">
  <img src="docs/cards.png" alt="Three generated cards, answer side: an ssh tunnel command with a table explaining each part, a diagram of a load balancer forwarding plain HTTP to the app, and a table comparing 502 and 504 errors" width="100%">
</p>

<p align="center"><sub>Three cards from one day's run, as Anki shows their answers: a command broken into its parts, a diagram drawn from the card's own answer, and a comparison table.</sub></p>

It is also built the way a data platform team would build it: idempotent stages
keyed by date, a layered DuckDB warehouse, and output as Parquet. That makes it
ready to schedule with Airflow, containerise, and move to S3 and Kubernetes. See
the [roadmap](#roadmap).

<p align="center">
  <img src="docs/pipeline.svg" alt="Idempotent stages — ingest, target, generate, verify, dedup, refill, images, export, report — over a DuckDB warehouse partitioned by run_date" width="100%">
</p>

## Quick start

```bash
pip install ".[guide]"             # installs the `ankigen` command (+ the PDF guide)
cp .env.example .env               # set LLM_PROVIDER / LLM_API_KEY (Groq is free)
ankigen decks                      # see your decks, to write the profile
ankigen validate                   # check the profile against them
ankigen plan                       # what today's run would generate + the exact prompt
ankigen run                        # do it
ankigen report                     # what was kept, dropped, and why
```

Import `data/out/<date>/ankigen_<date>.apkg` into Anki with File > Import, and
read `data/out/<date>/guide_<date>.pdf` alongside it: it explains each card in
depth, and each card names its section.
Cards land **straight in their deck**, tagged `ankigen::run_<date>` so a batch
can be found or deleted later. Anything close to a card you already have is
tagged `ankigen::near-dup` with what it resembles, so search that tag first and
delete the few that are redundant. Re-importing the same day's package updates
those notes instead of duplicating them. (Set `inbox:` in the profile to file
them in a subdeck instead.)

Anki can stay open. It holds the collection exclusively, so the snapshot falls
back to copying the database with its `-wal` sidecar when the backup API cannot
get a lock.

## What each stage does

| Stage | Reads | Writes | Notes |
|---|---|---|---|
| **ingest** | your `collection.anki2` | `raw_notes` (daily snapshot), `raw_revlog` (incremental) | Snapshots through SQLite's backup API, so reviews still in the `-wal` file are included. A plain file copy misses them, and there's a test proving it. Handles both schema generations and any note type, from `Front/Back` to `Frente/Verso`. |
| **target** | `raw_notes`, profile | `requests` (with the rendered prompt) | Deterministic. The same date always produces the same plan, and topics and weak cards rotate day to day; a deck with a `start:` date covers its topics once, in order. No LLM calls, so `plan` is instant. |
| **generate** | `requests` | `generated_cards` | The only non-deterministic stage. If one request fails, the rest of the run still goes ahead. |
| **verify** | `generated_cards` | `verified_cards` | A second LLM pass fact-checks each card, ideally on a *different* model (`VERIFY_MODEL`) — a model marking its own homework shares its blind spots. On real runs it catches roughly a third of what the generator writes. If the checker is down, cards pass through tagged `ankigen::unverified` rather than being dropped. A card whose only fault is its drawn table or diagram keeps its place and loses the picture. |
| **refill** | requests left short by verify and dedup | more `generated_cards` (flagged `refill`), with their `verified_cards` and `dedup_results` | One more request per short batch, for exactly the cards it lost, showing the model each rejected card and why. The new cards go through the same check and dedup; one round only, so a stubborn topic cannot spend the day's free-tier requests. |
| **dedup** | the above + `raw_notes` + previous runs | `dedup_results` | Compared against your **whole collection**, not just the target deck — a fact you already have in `DS::SQL` is not new because a run asked for it under Data Platform. Embedding similarity catches rephrasings; fuzzy matching runs over the nearest twenty by meaning. Cards just below the duplicate threshold are kept and tagged `ankigen::near-dup` rather than dropped unseen. |
| **images** | kept cards wanting one | `card_visuals`, `card_images` | Only for cards whose answer is easier to hold as a picture. Mostly **drawn from the card itself**: the model that writes a card can describe a comparison as a table or a flow as a Graphviz diagram in the same request, so the picture shows exactly what the card says, and the fact-checker checks it along with the card. Cards that need a real picture — a screenshot, a photograph — are searched for on the web, then **shown to the model with the card** and kept only if it actually illustrates it: search engines match the words around a picture, never the picture, so a card about S3's flat namespace once arrived with a stock photo of a basketball player. Everything travels inside the note (inline SVG, HTML tables, small inline JPEGs), so no media sync is needed. |
| **guide** | kept cards | `guide_sections`, `guide_<date>.pdf` | A **study guide** for the day: a chapter per topic that opens with how the thing works, then a section per card with why the answer is what it is, an example, the usual mistakes, and what is related. Written by the writer's model and read by the checker's; a section it disputes stays in, marked with the dispute. Each card carries its section number under the answer (`Guide 2026-10-03 · §2.3`). Needs the `[guide]` extra for the PDF; without it the guide is HTML. |
| **export** | `card_outcomes` view | `.apkg`, Parquet | Stable note GUIDs and stable deck and note-type IDs. |
| **report** | `pipeline_runs` + all of the above | `run_report.json` | Per-stage timings, drops with reasons, and token usage. |

Every stage replaces its own `run_date` partition in a single transaction.
Re-running any stage for any date is therefore safe. Retrying `dedup` or
`export` never calls the model and never changes which cards exist.

```bash
ankigen run --date 2026-09-22 --stage dedup --stage export --stage report
```

## The profile

A pipeline's `profile.yaml` (`pipelines/data-platform/profile.yaml` by
default) is where personalisation lives:

```yaml
learner:
  level: "working data scientist, moving into data platform"
  goals: ["Interview-ready on core data science: statistics, SQL, ML"]
style:
  max_answer_words: 40
  examples_per_prompt: 4          # few-shot examples drawn from YOUR cards
  rules: ["When a concept has a common misconception, target it."]
weak_cards: {min_lapses: 2, max_ease: 2100, max_per_deck: 1}
global_quota: 15
decks:
  - deck: DS::SQL                 # existing deck: extend it in your own voice
    daily_quota: 3
    topics: [window functions, NULL semantics]
    instructions: Use small SQL snippets in backticks.
  - deck: Data Platform::Airflow  # a subject you don't have yet
    new_deck: true
    topics: [idempotent tasks and safe backfills]
```

A subject can also be a **curriculum**: a `start:` date, levels from 1 (never
used it) to 4 (interview), and a *kind* per topic that decides what its cards
look like. Short cards for the basics, and the long "why" cards only where they
belong. The dates are the curriculum's own days: each run writes the day after
the last one in your collection, so the schedule does one a day and running it
again by hand does the next, and everything after it moves up.

```yaml
  - deck: Data Platform::01 Linux
    start: 2026-09-28
    first_day_quota: 100            # a kickoff day, then daily_quota
    daily_quota: 20
    levels:
      1:
        - command: "moving around: pwd, cd, ls"           # task -> `cd -`, and back
        - shortcut: "command history: Ctrl+R and !!"      # task -> `Ctrl+R`, and back
        - concept: "absolute versus relative paths"       # one-sentence answer
      2:
        - build: "a first bash script"                    # one file, several gaps
      4:
        - scenario: "a full disk with files still open"   # situation, answer, why
```

| Kind | Note type | The card |
|---|---|---|
| `command`, `shortcut` | AnkiGen Command | A task and its command or keys. Two cards: task to command, and command to what it does. |
| `concept` | AnkiGen Basic | A short question, an answer of one sentence. |
| `build` | AnkiGen Cloze | One real file or script in a code block, with a gap per part, and a card per gap. |
| `scenario` | AnkiGen Detailed | A situation, what happens, and why. |

Every generation prompt is built from:

- your learner profile and style rules;
- a few real cards from that deck as examples, so new cards match your phrasing and length;
- the existing cards most related to today's topic, as a do-not-duplicate list (unrelated cards are left out to save tokens);
- for weak cards, the card you keep failing, with an instruction to approach it from a different angle rather than rephrase it.

`ankigen plan --prompts 3` prints exactly what will be sent.

## LLM providers

Any OpenAI-compatible endpoint works. Set `LLM_PROVIDER` to a preset (`groq`,
`openrouter`, `nvidia`, `sambanova`, `mistral`, `ollama`) or to `gemini`.
Two setups are worth knowing:

Embeddings are configured separately, because most free chat APIs serve none.
Left unset they fall back to **fastembed**, which runs an ONNX model in-process:
no server, no key, no quota, and the only option that works unchanged on a CI
runner.

| | Generation | Free limits (measured Sept 2026) |
|---|---|---|
| **Most headroom** | `groq` | 1000 requests/day |
| **Best cards** | `gemini` | 20 requests/day **per model** — far tighter than the blog posts claim |
| **Offline** | `ollama` | unlimited, and noticeably weaker cards |

A run costs about seven generation calls and seven checking calls, which is why
`VERIFY_MODEL` pointing at a second model matters on Gemini: it doubles the
allowance *and* gives an independent opinion.

```
LLM_PROVIDER=gemini
GOOGLE_API_KEY=...
VERIFY_MODEL=gemini-3.5-flash   # a different model checks what the first wrote
```

If embeddings are configured but unreachable, dedup **fails** rather than
quietly reporting "no duplicates" — falling back silently once replaced four
runs of good results with that claim. Fuzzy-only matching has to be asked for.

`ankigen providers` shows the resolved configuration and tests the connection.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

The tests build real SQLite collections in both Anki schemas and replace the LLM
and embedding provider with deterministic fakes, so they run offline in a few
seconds.

> **Non-ASCII paths:** an editable install can't be built from a directory whose
> path contains characters outside the system codepage, because setuptools
> writes a `.pth` file in that encoding. pytest is configured with
> `pythonpath = ["src"]`, and `python -m ankigen` works with `PYTHONPATH=src`.

## Pipelines, and running them daily

The repository can hold several **pipelines**, one folder each under
`pipelines/`: a `profile.yaml` saying what to teach, and a `pipeline.yaml`
saying when to run it (a cron in its own time zone), what to produce (the
study guide PDF, a push to AnkiWeb), which models to use and how many calls a
run may make. Each pipeline writes to decks of its own, keeps its own
curriculum and warehouse, and tags its notes `ankigen::pipeline::<id>`.

```bash
ankigen pipelines list               # every pipeline, and when it runs next
ankigen pipelines validate           # check them all
ANKIGEN_PIPELINE=german ankigen run  # one run of one pipeline
```

Without leaving a machine on: an hourly [GitHub Actions workflow](.github/workflows/tick.yml)
runs each pipeline at its time, on GitHub's runners, one after another. A run
syncs your collection down from AnkiWeb, writes the cards, and syncs them back,
so they appear on your phone and desktop with nothing to import; what it did is
recorded on the `ankigen-status` branch. Free on a public repo, two secrets to
set up — see [docs/GITHUB_ACTIONS.md](docs/GITHUB_ACTIONS.md).

There is also an [Airflow stack](infra/airflow/) that runs the same stages
as one task each, which is the orchestration you would use at work; it needs
something to be on, so Actions is what actually fires daily.

## Roadmap

1. ✅ **Core pipeline**: personalised, verified, deduplicated, illustrated daily cards.
2. ✅ **Scheduled**: GitHub Actions daily; an Airflow DAG for local orchestration.
3. **Feedback loop**: the review history is already ingested — measure whether generated cards lapse more than hand-written ones, and whether illustrated cards stick better.
4. **Docker**: multi-stage image; the collection and profile mounted read-only.
5. **AWS free tier**: S3 `raw/`, `curated/` and `gold/` layers via Terraform, and a least-privilege IAM role.
6. **Kubernetes**: `CronJob` on `kind`, then `KubernetesPodOperator` per stage.
7. **Android client**: create, schedule and watch pipelines from a phone, with each run's study guide saved to a folder on it. The backend it drives (pipelines, the hourly tick, the status branch) is in place.
8. Later: PDF ingestion as a second source.

## License

MIT. See [LICENSE](LICENSE).
