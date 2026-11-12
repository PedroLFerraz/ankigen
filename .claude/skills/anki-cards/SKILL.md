---
name: anki-cards
description: Use when changing what a generated card looks like: note types, card kinds, or the fields a stage fills. Points to card_types.py, profile.py and the kind prompts.
---

# Anki cards

Two things decide a card: its **note type** (fields, templates and CSS in
`src/ankigen/card_types.py`, `CARD_TYPES`) and the **prompt** that asked for it
(`src/ankigen/prompts/`). Full tables are in
[docs/components/card-types-and-prompts.md](../../../docs/components/card-types-and-prompts.md).

- Note types: `basic`, `detailed`, `cloze`, `command`, `visual`. Each has a fixed
  `model_id`; never change one, or Anki makes a duplicate note type.
- A deck's plain cards take its `card_type` from the profile. A curriculum topic
  names a **kind** (`KINDS` in `profile.py`: `command`, `shortcut`, `concept`,
  `build`, `scenario`) and the kind picks the note type.
- A kind's JSON shape lives in `prompts/kind_<kind>.txt`. Adding a kind means:
  `KINDS`, a `kind_<kind>.txt`, `targeting.py` (loads it) and `generate.py` (files the cards as a note type), the
  `--kind` help in `cli.py`, and a test in `tests/test_card_kinds.py`.
- A card can carry a drawn table or Graphviz diagram (`visuals.py`) or a searched
  picture (`images.py`, DuckDuckGo plus a vision check). See
  [docs/components/images.md](../../../docs/components/images.md).
- Cards are tagged `ankigen::run_<date>`; near-duplicates `ankigen::near-dup`.
- The `card_outcomes` table in the warehouse holds every card and why it was
  kept or dropped.
