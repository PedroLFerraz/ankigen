# AnkiGen

A daily pipeline that reads an Anki collection and writes new cards that fit
it. Read [docs/README.md](docs/README.md) for a page on each part and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how they fit together. This
file only says what you need to work here without breaking it.

## Layout

- `src/ankigen/`: the package. `cli.py` is the `ankigen` command; `pipeline.py`
  holds the ten stages; `llm.py` the providers; `warehouse.py` the DuckDB file;
  `card_types.py` the note types; `prompts/*.txt` the prompt templates.
- `pipelines/<id>/`: `profile.yaml` (what to teach) and `pipeline.yaml` (when
  to run, models, limits) for each pipeline. `pipelines/schema.json` checks them.
- `tests/`: pytest, offline.
- `android/`: the app that edits the pipelines and reads the run status.
- `infra/`: Airflow (`infra/airflow/`) and other ways to run the image.
- `.github/workflows/`: tests, the hourly tick, the daily run, the image build.
  Do not edit them unless the task is about them.

## Commands

```bash
pip install ".[dev,push,gemini,guide]"         # extras as in .github/workflows/tests.yml
python -m pytest -q                            # the whole suite; add a file to run one
python -m ankigen pipelines validate           # every pipelines/*/ file, as CI does
ankigen validate                               # the profile against your own decks
ankigen run --dry-run                          # ingest and plan only, no LLM calls
ankigen plan                                   # what today would generate, with the prompt
```

The image and guide tests need `graphviz` and `libpango-1.0-0 libpangoft2-1.0-0`.

## The stage contract

Each stage is `stage_x(ctx, run_date)` in `pipeline.py`. It reads its inputs from
the warehouse and writes its own `run_date` partition with
`Warehouse.replace_partition`, so running it twice gives the same result. A stage
never calls another stage: what it needs from an earlier one, it reads from the
warehouse. Nothing passes between stages in memory. Register a new stage in
`STAGES`, and say in its docstring which tables it reads and writes.

## Conventions

- Prompts live in `src/ankigen/prompts/*.txt` and load through
  `string.Template` (`$name` placeholders, so JSON braces need no escaping). Do
  not build prompts with f-strings in Python.
- Tests stay offline. Use `FakeLLM` / the `fake_llm` fixture and
  `fake_embeddings` from `tests/conftest.py`; never call a real provider.
- Match the file you are editing: its comment density, naming and idiom.
- Commit messages: a conventional prefix (`feat:`, `fix:`, `chore:`, `ci:`,
  `docs:`, `test:`), a lowercase one-line summary of what now works, and the
  issue number at the end, e.g. `fix: the warehouse reads a scalar's whole
  result, so DuckDB 2.0 runs the next query (#54)`.

## Do not touch

- The `ankigen-status` branch: the workflows write run status there and the app
  reads it. Never commit to it by hand.
- Real AnkiWeb credentials (`ANKIWEB_*`) and `.env`. Tests and runs here use
  fakes.
- The user's Anki collection: ingest copies it, nothing writes to the original.
  Only `ankigen push` changes a collection, and only through AnkiWeb.
- `master`: work on a branch and open a pull request.

## Skills

`.claude/skills/` has short guides for card types (`anki-cards`), prompt
templates (`llm-prompts`) and the test suite (`testing`).
