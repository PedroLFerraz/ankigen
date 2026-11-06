# Book, themes and parts

## What it's for

Three commands that make or change cards outside the daily run. None of them
goes through the warehouse or the stages; each one calls the model directly
and checks its own output.

- **`ankigen book`** (`src/ankigen/book.py`): cards from one chapter of a
  PDF book, in the book's order, with its own figures. Cards written from a
  topic name assume whatever background the model thinks is usual; written
  from the book the learner is reading, each card leans on what the book has
  explained so far, cites its page, and shows the book's diagram when one
  illustrates it.
- **`ankigen add-theme`** (`src/ankigen/themes.py`): starts a new subject.
  The model plans an ordered list of topics in the same shape as the
  profile's other decks, and the deck is appended to `profile.yaml`.
- **`ankigen parts`** (`src/ankigen/breakdown.py`): gives command cards
  already in the collection what new ones are written with: what each piece
  of the command does, under the answer.

## Entry points

| Command | Options | Workflow |
|---|---|---|
| `ankigen book <pdf>` | no `--chapter`: list the chapters; `--chapter N` writes one; `--deck`, `--level`, `--language`; `--rewrite` writes it again; `--push` also pushes it | none |
| `ankigen add-theme --deck "..."` | `--about`, `--quota`, `--topics` (16), `--card-type`, `--dry-run`, `--summary` (Markdown for a pull request) | `add-theme.yml` ("add a theme") |
| `ankigen parts` | `--deck`, `--limit`, `--dry-run` | none |

## Reads / writes

**book**

- Reads the PDF (its chapter bookmarks, text and figures) on this machine.
  The text goes to the model; the book stays out of the repository.
- Writes `DATA_DIR/books/<book>/chNN.json` (the cards), `chNN.apkg` and the
  extracted `figures/`. The chapter goes to its own subdeck,
  `<Book title>::NN <Chapter title>`. Once written, the chapter's cards are
  reused as they are, so the ones you reviewed are the ones `--push` sends.
- One model call per section; a section that fails is logged and the others
  go on.

**add-theme**

- Reads the profile and the subject's sentence; one model call
  (`prompts/theme.txt`).
- Appends the deck to `profile.yaml` as text, so every comment in the file
  survives, and raises `global_quota` if the new deck would otherwise get no
  cards. In a curriculum (decks with `start:`), the new deck starts the day
  after the last one ends. `decks:` must be the file's last section.
- From the *add a theme* workflow it arrives as a branch and a pull request
  (or a link to open one): nothing joins the daily run until it is merged.

**parts**

- Reads the collection over AnkiWeb (sync down first, like `push`), and
  picks `AnkiGen Command` notes without a breakdown, plus `AnkiGen Basic`
  notes whose whole answer is one command.
- Writes the breakdown into an existing field (`Note`, or `Answer`) and
  syncs up. A new field would change the note type, which Anki can only sync
  one way; appending to a field is an ordinary edit.
- Ten notes per request. Written by the writer's model (`prompts/parts.txt`),
  checked by the checker's (`prompts/parts_check.txt`); a breakdown the
  checker faults is left off, and the next run asks again.

## Depends on

The [LLM providers](llm-providers.md) for all three. `book` uses `pypdf`;
`book --push` and `parts` need the `[push]` extra and an AnkiWeb key
([AnkiWeb sync](ankiweb-sync.md)). `add-theme` validates against the
profile model ([Profiles and pipelines](profiles-and-pipelines.md)).

## How to run and test it

```bash
ankigen book ~/books/ddia.pdf                    # list the chapters
ankigen book ~/books/ddia.pdf --chapter 3        # write chapter 3, review chNN.json
ankigen book ~/books/ddia.pdf --chapter 3 --push

ANKIGEN_PIPELINE=data-platform ankigen add-theme --deck "Data Platform::Spark" \
  --about "Spark for someone who knows pandas" --dry-run

ankigen parts --deck "Data Platform::01 Linux" --limit 10 --dry-run
```

```bash
python -m pytest tests/test_book.py tests/test_themes.py tests/test_breakdown.py
```

## Limits and known issues

- `book` splits a PDF by its bookmarks; a PDF without chapter bookmarks is
  refused. Figures are found by their captions (`Figure 3-2. ...`), the
  convention of technical publishers.
- `add-theme` refuses a deck already in the profile, and a profile whose
  last section is not `decks:`, where appending would land under another
  key.
- `parts` only knows the two AnkiGen note types; command cards written by
  hand in another note type are not touched.
