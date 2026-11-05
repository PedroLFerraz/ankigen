# Images

## What it's for

Some answers are easier to hold as a picture. The `images` stage gives those
cards one, in two ways:

- **Drawn from the card itself** (`src/ankigen/visuals.py`). The model that
  writes a card can describe a comparison as a table or a flow as a Graphviz
  diagram in the same request, so the picture shows exactly what the card
  says, costs no extra call, and the fact-checker checks it along with the
  card. Tables become HTML and diagrams inline SVG, drawn in `currentColor`
  so they read on dark and light cards alike.
- **Searched on the web** (`src/ankigen/images.py`), for cards that need a
  real picture: a screenshot, a photograph. Search engines match the words
  around a picture, never the picture, so a card about S3's flat namespace
  once arrived with a stock photo of a basketball player. Each result is
  therefore **shown to a vision model with the card** and kept only if it
  actually illustrates it. A picture nothing could check is not used: a card
  with no picture beats a card with the wrong one.

Everything travels inside the note (inline SVG, HTML tables, small inline
JPEGs), so no media sync is needed ([AnkiWeb sync](ankiweb-sync.md)).

## Entry points

- The `images` stage (`stage_images` in `src/ankigen/pipeline.py`), after
  dedup, so no picture is fetched for a card about to be thrown away. It
  draws every card with a described visual first, and searches only for the
  ones left that asked for a picture.
- `ankigen audit-images`: has the checker look at the pictures already on
  AnkiGen cards in your collection and prints an Anki search (`nid:...`)
  for the bad ones. Reads the collection, never writes to it.
- To redo a day's pictures: `ankigen run --date <day> --stage images --stage
  export --stage report`, then `ankigen push --date <day>`. A push replaces a
  picture only with a newer one, and never takes a working picture off a
  note.

## Reads / writes

- **Reads**: kept cards from `card_outcomes` with a `visual_json` or an
  `image_query`; the profile's `images`, `verify_images` and per-deck
  `image_context` (put in front of every search, so "airflow pool" is not a
  swimming pool); DuckDuckGo image search, through `ddgs`.
- **Writes**: `card_visuals` (the drawn HTML, or why it could not be drawn)
  and `card_images` (query, file, source page, and what the checker saw);
  downloaded pictures in `media/`, re-encoded as JPEG (Anki's importer
  dropped some PNGs and WebPs). Push inlines them as 640 px `data:` URIs.

## Depends on

Graphviz (`dot`) on the system for diagrams; without it a diagram cannot be
drawn and the card falls back to a search. `ddgs` and `primp` for search,
Pillow for re-encoding, and the checker's model for looking at pictures
(`llm.check_image`, see [LLM providers](llm-providers.md)).

## How to run and test it

```bash
sudo apt-get install graphviz        # for diagrams
ankigen run --date 2026-10-03 --stage images --stage export --stage report
ankigen audit-images --limit 20
```

```bash
python -m pytest tests/test_images.py tests/test_visuals.py
```

The tests never reach the web: search and download are replaced with fakes.

## Limits and known issues

- At most three pictures are checked per card, and 24 per run: every look
  costs a request on a metered free tier.
- A table is at most 6 rows by 4 columns, a diagram at most 12 nodes; a
  description over the limits is not drawn. Diagrams wider than 2:1 are laid
  out top to bottom so they stay readable on a phone.
- `ddgs` is pinned below 9.10 together with `primp` 0.15: newer `ddgs`
  fails every DuckDuckGo request and falls back to Bing, which serves a CI
  runner trending junk (see the comment in `pyproject.toml`).
- Wikimedia Commons was removed as a source: its search matches file
  descriptions, so for software topics it answered with stock photographs.
- Moving the picture check to Claude:
  [#76](https://github.com/PedroLFerraz/ankigen/issues/76).
