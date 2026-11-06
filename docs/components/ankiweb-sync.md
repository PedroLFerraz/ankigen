# AnkiWeb sync

## What it's for

It puts the day's cards into your Anki collection itself, so your phone and
desktop pull them down like any other change and nothing is imported by
hand. AnkiWeb has no API for adding notes. What it has is the sync protocol,
which the official `anki` package speaks, so the way in is to keep a
collection of our own, add notes to it, and sync it up.

The order is the whole safety story:

```
sync down  ->  add notes  ->  sync up
```

Syncing down first means our copy is never behind yours, which is what would
otherwise make the server demand a one-way sync. If it demands one anyway,
the sync stops: the only way to satisfy it is to declare one side the
winner, and picking ours would overwrite years of review history with
whatever this copy holds. A full download happens exactly once, the first
time, when there is no copy yet. A full upload never happens.

## Entry points

`src/ankigen/push.py`, through these commands:

| Command | What it does |
|---|---|
| `ankigen push --login` | Trades your AnkiWeb password for a sync key and prints `ANKIWEB_KEY=...` to store instead. |
| `ankigen pull` | Brings the working copy up to date: a fresh download the first time, a normal sync after. What CI runs before the pipeline, since a runner has no local Anki. |
| `ankigen push --date <day>` | Sync down, add the day's kept cards, sync up. `--dry-run` lists what it would push and touches nothing. |
| `ankigen book --push`, `ankigen parts` | The same sync around their own notes ([Book, themes and parts](book-themes-parts.md)). |

The `run-pipeline` workflow runs `pull`, then `run`, then `push` when the
pipeline sets `outputs.push_to_ankiweb`.

## Reads / writes

- **Reads**: AnkiWeb, with `ANKIWEB_KEY` (or `ANKIWEB_USERNAME` and
  `ANKIWEB_PASSWORD`; `ANKIWEB_ENDPOINT` for a self-hosted sync server); the
  day's kept cards from `card_outcomes`.
- **Writes**: the working copy at `DATA_DIR/anki/collection.anki2`, shared
  by every pipeline; notes in your AnkiWeb collection, with the same tags the
  `.apkg` gives (`ankigen`, `ankigen::run_<date>`, why it was asked for
  such as `ankigen::topic`, `ankigen::pipeline::<id>`, and
  `ankigen::unverified`, `ankigen::near-dup` or `ankigen::ad-hoc` where they
  apply). Note types are created as
  `AnkiGen Basic`, `AnkiGen Command` and so on if the collection lacks them.

A note's GUID comes from the card's id, so pushing a day twice updates its
notes instead of adding them again. On an existing note, a push replaces a
picture only with a newer one, and never takes a working picture off it.

**Pictures travel inside the notes.** Media is never synced from here. Media
sync is how a picture file is uploaded, but it is also how a client catches
up on every file it lacks, which for this working copy is your whole media
folder, over a gigabyte, on every run. It also runs in the background, and
closing the collection cancels it: the first pushed pictures arrived on
phones as broken-image icons for exactly that reason. Drawn pictures are a
few KB of SVG or HTML, and a searched picture is re-encoded small (640 px
JPEG) and stored as a `data:` URI.

## Depends on

The `[push]` extra (`anki>=25.0`, a large download with a Rust backend,
which is why it is optional) and an AnkiWeb account. The pipeline itself runs
without it; you then import the `.apkg` by hand.

## How to run and test it

```bash
pip install ".[push]"
ankigen push --login                 # once; put ANKIWEB_KEY in .env
ankigen push --date 2026-10-03 --dry-run
ankigen push --date 2026-10-03
```

```bash
python -m pytest tests/test_push.py
```

The tests use real Anki collections from the `anki` package and never
contact AnkiWeb. They are mostly about what the code refuses to do, since
the collection it touches for real holds years of review history.

## Limits and known issues

- **The sync refuses to guess.** If AnkiWeb reports that the working copy
  and yours have diverged beyond a normal merge, the push stops. Sync from
  Anki on your own machine and run it again. On GitHub, see
  [GITHUB_ACTIONS.md](../GITHUB_ACTIONS.md#what-to-know) for when it
  persists.
- AnkiWeb spreads accounts over several sync hosts and names the right one
  on first contact, so every session starts with a handshake. Going straight
  to the default host fails a full download with `400 missing original
  size`.
- Changing your AnkiWeb password invalidates the key.
- One collection is shared by every pipeline, so only one may sync at a
  time; the workflows run them one after another.
