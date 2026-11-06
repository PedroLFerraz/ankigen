# Study guide

## What it's for

A card is short on purpose, which leaves the *why* somewhere else: a search,
a man page, a blog post. The study guide is where it goes instead. Each run
writes one next to its cards: a chapter per topic (usually five cards) that
opens with how the thing works, then a section per card with why its answer
is what it is, an example, the usual mistakes, and what to learn next.

Every card carries its section number under the answer
(`Guide 2026-10-03 · §2.3`), so a card you fail leads straight to its
explanation.

## Entry points

- The `guide` stage (`src/ankigen/guide.py`), after `images` so drawn tables
  and diagrams appear in it, and before `export` so the notes carry their
  reference. Skipped when the pipeline's `outputs.guide_pdf` is false.
- A guide for a day that has none: `ankigen run --date <day> --stage guide
  --stage export --stage report`. Notes already in the collection keep what
  they have; only cards pushed for the first time get the `Guide … · §`
  line.
- On GitHub, the guide is in each run's `cards-<pipeline>-<day>` artifact
  ([GitHub Actions](../GITHUB_ACTIONS.md#the-study-guide)), and the
  [Android app](../../android/README.md) saves every guide to a folder you
  pick.

## Reads / writes

- **Reads**: kept cards from `card_outcomes`, with their `requests` (topic,
  deck) and `card_visuals`; the profile for the learner's level and
  language.
- **Writes**: `guide_sections` (one row per chapter opening and per card:
  the Markdown, its status `checked`, `disputed`, `unchecked` or `missing`,
  and the checker's issue); `out/<date>/guide_<date>.html` and
  `guide_<date>.pdf`.

How a run goes:

1. Number the chapters (one per request, decks in the profile's order,
   topics in the curriculum's) and the cards under them, **before** any model
   call, so each card's reference holds even if its chapter is not written.
2. Per chapter, one writer call (`prompts/guide.txt`) writes the opening and
   a section per card, and one checker call (`prompts/guide_check.txt`)
   reads it. A section the checker disputes stays in, marked with the
   dispute: the cards were already checked, and an explanation with a
   flagged sentence is still more use than none.
3. Render HTML, and a PDF when WeasyPrint is installed.

## Depends on

The writer's and checker's models ([LLM providers](llm-providers.md)), and
the `[guide]` extra: `markdown` for the HTML and WeasyPrint, with Pango from
the system, for the PDF. Without WeasyPrint the guide is HTML only; without
`markdown`, paragraphs and code blocks still come out readable.

## How to run and test it

```bash
pip install ".[guide]"
sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0
ankigen run --date 2026-10-03 --stage guide --stage export --stage report
```

```bash
python -m pytest tests/test_guide.py
```

## Limits and known issues

- Two calls per chapter. On a free tier the guide is often what runs out
  first; a chapter that could not be written says "not written today", and
  the cards go out regardless. Nothing in this stage fails the run.
- A PDF rendering error costs the PDF, not the day: the HTML is still
  written.
