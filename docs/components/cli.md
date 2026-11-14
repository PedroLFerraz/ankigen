# CLI

## What it's for

`ankigen` is the one way in to the package. A person types it, the workflows
run it as `python -m ankigen`, and the Docker image has it as its entry point.
The commands are thin: each one opens a pipeline context (profile, settings,
warehouse) and calls the module that does the work.

## Entry points

`src/ankigen/cli.py`, installed as `ankigen` (`[project.scripts]` in
`pyproject.toml`) and runnable as `python -m ankigen`. `ankigen <command>
--help` lists every option; `-v` before the command turns on debug logging.

| Command | What it does | Page |
|---|---|---|
| `run` | The daily pipeline, or one deck by hand with `--deck` (and `--topic`, `--prompt`, `--count`, `--kind`). `--stage` runs only some stages; `--dry-run` stops before the first LLM call. | [Daily pipeline](daily-pipeline.md) |
| `plan` | What the next run would generate, and the prompts it would send (`--prompts N`). For a day that has run, the plan it ran with. | [Daily pipeline](daily-pipeline.md) |
| `next` | The curriculum day the next run writes, `YYYY-MM-DD`. | [Profiles and pipelines](profiles-and-pipelines.md) |
| `decks` | The collection's decks with note and weak-card counts, marking the ones the profile targets. | |
| `validate` | Checks the profile against the collection and prints the curriculum's timeline. | [Profiles and pipelines](profiles-and-pipelines.md) |
| `report` | Prints a day's `run_report.json`. | [Daily pipeline](daily-pipeline.md) |
| `summary` | A day's cards as Markdown, for the run's page on GitHub. | [GitHub Actions](github-actions.md) |
| `pull` | Brings the AnkiWeb working copy of the collection up to date. | [AnkiWeb sync](ankiweb-sync.md) |
| `push` | Adds a day's kept cards to the collection over AnkiWeb (`--login` makes the key, `--dry-run` lists them). | [AnkiWeb sync](ankiweb-sync.md) |
| `book` | Cards from one chapter of a PDF book. | [Book, themes and parts](book-themes-parts.md) |
| `parts` | Adds a breakdown of each command to command cards already in Anki. | [Book, themes and parts](book-themes-parts.md) |
| `add-theme` | A new deck whose topics the model plans, appended to the profile. | [Book, themes and parts](book-themes-parts.md) |
| `audit-images` | Has a model look at the pictures already on AnkiGen cards and prints an Anki search for the bad ones. Reads the collection, never writes to it. | [Images](images.md) |
| `reset` | Deletes this pipeline's warehouse, snapshots, media and output (`--yes` to do it). Keeps the embedding cache unless `--drop-embeddings`. Never touches the collection. | [Warehouse](warehouse.md) |
| `pipelines` | `list`, `validate`, `due`, `env`, `schema`, `record`: the same as `python -m ankigen.pipelines`. | [Profiles and pipelines](profiles-and-pipelines.md) |
| `providers` | The provider presets, the resolved generation and embedding settings, and a connection test. | [LLM providers](llm-providers.md) |

## Reads / writes

The CLI itself writes nothing; the modules it calls do. What it reads is
settings, from the environment and `.env` (`src/ankigen/config.py`):

| Setting | Default | |
|---|---|---|
| `ANKIGEN_PIPELINE` | empty | Which folder under `pipelines/` to run. Empty: `data-platform`'s profile, with the warehouse in `DATA_DIR` itself. |
| `ANKIGEN_PROFILE` | empty | A profile to use instead of the pipeline's. Same as `--profile`. |
| `DATA_DIR` | `./data` | Where every pipeline keeps its files, under `pipelines/<id>/`. |
| `ANKI_COLLECTION_PATH` | Anki desktop's, if it has exactly one profile | The collection to read. |

The provider settings are on the [LLM providers](llm-providers.md) page, and
the AnkiWeb ones on [AnkiWeb sync](ankiweb-sync.md).

## Depends on

Typer for the commands. Everything else is imported inside the command that
needs it, so `ankigen --help` and `ankigen pipelines` start without loading
DuckDB, the embedding model or the `anki` package.

## How to run and test it

```bash
pip install -e ".[dev]"      # or ".[dev,push,guide]" for every command
ankigen --help
ankigen providers            # does the configured model answer?
```

`tests/test_e2e.py` runs the installed `ankigen run` end to end, in a
subprocess, over a small test collection with a fake model and fake
embeddings. It imports the installed package, so install it first. It is part
of the `e2e` job in [tests.yml](../../.github/workflows/tests.yml).

```bash
python -m pytest tests/test_e2e.py
```

## Limits and known issues

- Expected failures (a locked collection, an invalid profile, a missing file)
  print one line and exit 1, or 2 for an invalid profile. Anything else is a
  traceback, with locals hidden so it does not print the whole profile.
- `report` counts only the generate stage's tokens:
  [#58](https://github.com/PedroLFerraz/ankigen/issues/58).
