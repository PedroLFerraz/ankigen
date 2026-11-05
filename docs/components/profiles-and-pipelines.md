# Profiles and pipelines

## What it's for

A **pipeline** is one card-making setup: what to teach, and how and when to
run. The repository holds several, one folder each under `pipelines/`:

| File | What it is | Written by |
|---|---|---|
| `profile.yaml` | what to teach: the learner, the decks, the curriculum | hand, `add-theme`, or Claude through `edit-plan` |
| `pipeline.yaml` | how to run it: schedule, outputs, models, call budget | hand, or the [Android app](../../android/README.md) |

Each pipeline writes to decks of its own, keeps its own curriculum and
warehouse, and tags its notes `ankigen::pipeline::<id>`.

## Entry points

- `ANKIGEN_PIPELINE=<id>` picks the pipeline for any command; empty means
  `data-platform`'s profile, with the warehouse in `DATA_DIR` itself.
  `ANKIGEN_PROFILE` or `--profile` runs a bare profile instead.
- `ankigen validate`: the profile against the collection, with the
  curriculum's timeline.
- `ankigen next`: the curriculum day the next run writes.
- `ankigen pipelines <command>`, the same as `python -m ankigen.pipelines <command>`,
  which needs only pydantic and cronsim installed:

| Command | What it does |
|---|---|
| `list` | Every pipeline, its schedule and its next run. |
| `validate` | Checks every pipeline and exits 1 on a problem. Runs on every push. |
| `due` | The pipelines whose cron has fired since they last ran; what the hourly tick asks. |
| `env <id>` | The pipeline's settings as `KEY=VALUE` lines, for `$GITHUB_ENV`. |
| `schema` | `pipeline.yaml`'s JSON Schema; `--write` updates the checked-in `pipelines/schema.json`. |
| `curriculum` | Rewrites every pipeline's `curriculum.json` on a checkout of the status branch; the `curriculum` workflow runs it after a plan is merged. |
| `record` | Writes a run's outcome into a checkout of the `ankigen-status` branch. |

The code is `src/ankigen/profile.py` (the profile's model) and
`src/ankigen/pipelines.py` (the pipeline spec, scheduling and the status
branch).

### The profile

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
look like (see [Card types and prompts](card-types-and-prompts.md)). Short
cards for the basics, and the long "why" cards only where they belong.

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

**The dates are the curriculum's own days, not the calendar's.** Each run
writes the day after the last one the pipeline's decks have cards for (the
`ankigen::run_<date>` tags in the collection), so the schedule does one a
day, running it again by hand does the next, and everything after it moves
up. A pipeline that never pushes counts the days in its warehouse instead,
since its cards never reach the collection. Cards for one deck by hand
(`ankigen run --deck`) are tagged `ankigen::ad-hoc` and do not count.

Other per-deck settings: `card_type`, `images`, `image_context` (put in front
of every image search, so "airflow pool" is not a swimming pool) and
`instructions`. Profile-wide: `verify`, `images`, `verify_images` and
`inbox` (a subdeck to file new cards in, instead of the deck itself).

### The pipeline file

```yaml
# pipelines/data-platform/pipeline.yaml
name: Data Platform
enabled: true
schedule: {cron: "17 5 * * *", timezone: America/Sao_Paulo}   # one run = one curriculum day
outputs:  {guide_pdf: true, push_to_ankiweb: true}
models:   {writer: claude, checker: claude, fallback: gemini}  # and each one's model chain
budget:   {max_llm_calls_per_run: 0}                           # 0: no cap
```

`models:` takes a chain per role (`writer_models`, `checker_models`,
`fallback_models`, `fallback_checker_models`), which `pipelines env` turns
into the settings on the [LLM providers](llm-providers.md) page.
`budget.max_llm_calls_per_run` caps one run's share of the subscription and
free tiers every pipeline shares; when it is spent, each stage stops asking
and keeps what it has, as it does when a quota runs out.

## Reads / writes

- **Reads**: `pipelines/<id>/` (`ANKIGEN_PIPELINES_DIR` to look elsewhere),
  and for `due`, a checkout of the `ankigen-status` branch.
- **Writes**: `add-theme` appends to `profile.yaml` as text, so comments
  survive. The Android app rewrites `pipeline.yaml` whole, so comments there
  are not kept. `record` writes the status branch, per pipeline:
  `latest.json`, `runs/`, `cards/`, `scheduler.json` and `curriculum.json`
  (see [GitHub Actions](github-actions.md)).

## Depends on

Pydantic for both files, PyYAML, and cronsim for reading a cron in its own
time zone. `pipelines.py` imports nothing heavy at module level, so the tick
can decide what is due without installing the rest of the pipeline.

## How to run and test it

```bash
ankigen pipelines list               # every pipeline, and when it runs next
ankigen pipelines validate           # check them all
ANKIGEN_PIPELINE=historia-brasil ankigen validate
ANKIGEN_PIPELINE=historia-brasil ankigen run
```

```bash
python -m pytest tests/test_pipelines.py tests/test_config.py tests/test_targeting.py
```

`test_pipelines.py` checks every pipeline in the repository, and that
`pipelines/schema.json` matches the model; after changing `PipelineSpec`,
regenerate it with `ankigen pipelines schema --write`.

## Limits and known issues

- Two pipelines may not write to the same deck or to a deck and its
  subdeck: each would move the other's curriculum along. `validate` refuses
  it.
- Curriculum decks with start dates run one after another; `validate` fails
  if two running at once want more than `global_quota`.
- `profile.yaml` has no JSON Schema of its own; only `pipeline.yaml`, which
  the app writes, is checked against one.
