# Card types and prompts

## What it's for

Two things decide what a card looks like: the **note type** it is filed as in
Anki (its fields, templates and CSS, in `src/ankigen/card_types.py`), and the
**prompt** that asked a model to write it (the templates in
`src/ankigen/prompts/`). This page covers both, and the *kinds* a curriculum
topic can ask for, which connect them.

## Entry points

**Note types.** `CARD_TYPES` in `card_types.py`. Each one has a fixed
`model_id`, so every package and every push uses the same note type and Anki
never makes a duplicate.

| Type | Fields | Cards per note |
|---|---|---|
| `basic` | Question, Answer | 1 |
| `detailed` | Question, Summary, Explanation, Image, Reference | 1 |
| `cloze` | Text, Extra | one per gap |
| `command` | Task, Command, Note | 2: task to command, and command to what it does |
| `visual` | Image, Title, Explanation | 1 (defined, but no stage writes it) |

A deck's plain cards take its `card_type` (`basic`, `cloze` or `detailed`).
A curriculum topic instead names a **kind**, and the kind picks the note
type:

| Kind | Note type | The card |
|---|---|---|
| `command`, `shortcut` | AnkiGen Command | A task and its command or keys. Two cards: task to command, and command to what it does. |
| `concept` | AnkiGen Basic | A short question, an answer of one sentence. |
| `build` | AnkiGen Cloze | One real file or script in a code block, with a gap per part, and a card per gap. |
| `scenario` | AnkiGen Detailed | A situation, what happens, and why. |

**Prompts.** Eighteen `string.Template` files, shipped inside the package
(`[tool.setuptools.package-data]`) and read with `importlib.resources`:

| Template | Used by | For |
|---|---|---|
| `generate.txt` | targeting | the frame of every generation prompt |
| `format_basic.txt`, `format_cloze.txt`, `format_detailed.txt` | targeting | the JSON shape for a deck's plain cards |
| `kind_command.txt`, `kind_shortcut.txt`, `kind_concept.txt`, `kind_build.txt`, `kind_scenario.txt` | targeting | the shape of one kind's cards |
| `picture_hint.txt`, `image_hint.txt` | targeting | when to describe a table or diagram, and how to write a search query ([Images](images.md)) |
| `verify.txt` | verify, refill | the fact-checker |
| `guide.txt`, `guide_check.txt` | guide | writing and checking a chapter ([Study guide](study-guide.md)) |
| `parts.txt`, `parts_check.txt` | `ankigen parts` | command breakdowns ([Book, themes and parts](book-themes-parts.md)) |
| `theme.txt` | `ankigen add-theme` | planning a new subject's topics |
| `book.txt` | `ankigen book` | cards from a section of a book |

Every generation prompt is built from:

- your learner profile and style rules;
- a few real cards from that deck as examples, so new cards match your
  phrasing and length;
- the existing cards most related to today's topic, as a do-not-duplicate
  list (unrelated cards are left out to save tokens);
- for weak cards, the card you keep failing, with an instruction to approach
  it from a different angle rather than rephrase it.

`ankigen plan --prompts 3` prints exactly what will be sent.

## Reads / writes

- **Reads**: the profile (level, language, style rules, the deck's kind or
  card type) and `raw_notes` for the examples and the do-not-duplicate list.
- **Writes**: the rendered prompt into `requests.prompt`, so the warehouse
  keeps exactly what each request sent. Export writes the note types into
  the `.apkg` by their `name` (`basic`, `command`, ...). Push creates them in
  the collection as `AnkiGen Basic`, `AnkiGen Command` and so on, if missing.

## Depends on

Nothing beyond the standard library: `string.Template` and
`importlib.resources`. genanki (export) and the `anki` package (push) are
what turn the note types into Anki ones.

## How to run and test it

```bash
ankigen plan --prompts 5                 # the rendered prompts for the next day
ankigen run --deck "Data Platform::01 Linux" --kind build --topic "a crontab" --count 3
```

```bash
python -m pytest tests/test_card_kinds.py tests/test_targeting.py tests/test_generate_verify.py
```

`test_card_kinds.py` parses each kind into its fields and checks the
curriculum levels. One test pushes a command note into a real Anki
collection to check it makes two cards; it needs the `[push]` extra and is
skipped without it.

## Limits and known issues

- A note type's fields cannot change after the fact without a one-way sync:
  Anki syncs a schema change over whichever side loses. That is why
  `ankigen parts` writes into an existing field rather than adding one.
- A prompt edit changes every later day's cards but not the days already in
  the warehouse; rerun `generate` for a day to see the difference.
- The project's `anki-cards` and `llm-prompts` skills still describe the app
  before the rewrite:
  [#74](https://github.com/PedroLFerraz/ankigen/issues/74).
