# AnkiGen architecture

This document explains how AnkiGen is built, the way a computer architecture
course would. It goes through the **abstraction levels** first, where each layer
only talks to the one directly below it. Then it covers the **pipeline**: the
steps every daily run goes through, with the tools used in each.

> The analogy has limits: no instructions are executed here. It is useful
> because the same design problems come up. You need clear boundaries between
> layers, buffers between stages, a cache, and the ability to replay a stage
> safely.

---

## 1. The big picture

![AnkiGen pipeline](pipeline.svg)

<sub>Source: [`pipeline.svg`](pipeline.svg). Renders in browsers, GitHub and most Markdown viewers, in light and dark themes.</sub>

A run takes a **date** as input. It reads a *copy* of your Anki collection and
writes new cards for that date. Every stage writes its results into one
database, the warehouse. Stages never pass data to each other in memory, which
is what makes the design robust.

The LLM and embedding boxes are whichever provider `.env` names. With
`LLM_PROVIDER=openrouter` both are the same service on one key; with `groq` the
embeddings come from a local Ollama instead.

---

## 2. Abstraction levels

In a computer, each level (transistors, gates, microarchitecture, ISA, operating
system, programs) hides the one below it. AnkiGen has six levels, from the
lowest up:

```
 L5  Interface         ankigen CLI (typer)            │ what you type
 L4  Orchestration     pipeline.py                    │ runs stages in order, records status
 L3  Stages            ingest target generate verify  │ one job each, idempotent
                       dedup export report            │
 L2  Domain model      Note, Profile, GenerationRequest, Card
 L1  Services          warehouse · llm · embeddings · genanki
 L0  Physical storage  collection.anki2 · warehouse.duckdb · files on disk
```

| Level | Computer analogy | What it is in AnkiGen | Files | Tools |
|---|---|---|---|---|
| **L0 Storage** | Memory and disk | Your Anki collection (read only), the warehouse database, and the files a run produces | `data/` | SQLite, DuckDB file, filesystem |
| **L1 Services** | Device drivers | Thin wrappers that hide *how* to talk to each external system: the database, the LLM, the embedding model, and the `.apkg` writer | `warehouse.py`, `llm.py`, `dedup.Embedder`, `export.py` | `duckdb`, `pyarrow`, `openai` SDK, `requests`, `genanki` |
| **L2 Domain** | Instruction set (ISA) | The vocabulary every stage shares: a `Note` read from Anki, a `Profile`, a `GenerationRequest`, a generated `Card` | `ingest.Note`, `profile.py`, `targeting.GenerationRequest`, `generate.Card` | `dataclasses`, `pydantic` |
| **L3 Stages** | Pipeline stages | Seven functions, each doing one job: read its input from the warehouse, write its output back | `ingest.py` … `export.py` | see §3 |
| **L4 Orchestration** | Control unit | Decides which stages run, in what order, and records success or failure for each | `pipeline.py` | plain Python; later Airflow and Kubernetes |
| **L5 Interface** | Shell | Commands a human types | `cli.py` | `typer` |

**The rule that makes it work:** a stage (L3) never calls another stage. It only
uses services (L1) and domain types (L2). That is why the orchestrator (L4) can
be swapped out, from `ankigen run` to an Airflow DAG to Kubernetes pods, without
touching any stage.

---

## 3. The pipeline of events

### 3.1 CPU pipeline analogy

A classic CPU runs every instruction through the same stages. AnkiGen runs every
day through its own:

| CPU stage | What it does in a CPU | AnkiGen stage | What it does here |
|---|---|---|---|
| **IF**: Instruction Fetch | Read the next instruction from memory | **ingest** | Read the Anki collection into the warehouse |
| **ID**: Instruction Decode | Work out what the instruction means | **target** | Work out what to generate today, and build the prompts |
| **EX**: Execute | The ALU does the work | **generate** | The LLM writes the cards |
| hazard and fault checks | Detect bad results before committing | **verify** + **dedup** | Drop wrong cards and repeated cards |
| **WB**: Write Back | Commit the result | **export** | Write the `.apkg` for Anki |
| performance counters | Count what happened | **report** | Timings, drops, token use |

Two more pieces of hardware have direct equivalents:

- **Pipeline registers → warehouse tables.** In a CPU, a register sits between
  each pair of stages and holds the hand-off. Here, each stage writes to its own
  table (`requests`, `generated_cards`, `verified_cards`, …) and the next stage
  reads from it.
- **Cache → `embedding_cache`.** Converting text to an embedding is slow, like a
  memory access. The first run embedded 681 cards in 22s. The next run read them
  back from the cache in 0.8s.

### 3.2 Stage by stage

```
 run_date ─▶ [ingest] ─▶ [target] ─▶ [generate] ─▶ [verify] ─▶ [dedup] ─▶ [export] ─▶ [report]
              raw_notes   requests    generated_    verified_   dedup_     .apkg       run_report
              raw_revlog              cards         cards       results    parquet     .json
                                   ▲                                          │
                                   └──────── card_outcomes view ──────────────┘
```

#### ① ingest: fetch

| | |
|---|---|
| **Input** | `collection.anki2`, your live Anki file |
| **Output** | `raw_notes` (a full snapshot each day), `raw_revlog` (only new reviews) |
| **Tools** | `sqlite3` **backup API**, `html` (strip tags), `re` (strip cloze markers), `pyarrow` → DuckDB |

1. Take a consistent **snapshot** into `data/raw/<date>/collection.anki2`. It
   uses the backup API because Anki keeps recent changes in a separate `-wal`
   file that a plain copy misses. A test proves this.
2. Read the decks, turning Anki's `\x1f` separator into `::`. Read note types in
   both the old and new schema.
3. For every note, find the **front and back** without knowing its note type:
   take the first two fields that still contain text once images and audio are
   removed. This handles `Front/Back`, `Frente/Verso`, `Vorderseite/Rückseite`
   and so on.
4. Attach review stats to each note: lapses, ease, interval.
5. Load **notes** as a full daily snapshot. Load **reviews** *incrementally*:
   only rows with an id newer than the ones already loaded. These are the two
   classic loading patterns.

#### ② target: decode

| | |
|---|---|
| **Input** | `raw_notes`, the pipeline's `profile.yaml` |
| **Output** | `requests`: one row per request, including the complete prompt |
| **Tools** | `pydantic` + `PyYAML` (profile), `random` seeded by the date, `string.Template` + `importlib.resources` (prompt files) |

For each deck in the profile, up to its `daily_quota`:

1. **Weak cards first.** These are cards with 2 or more lapses, or ease at or
   below 2100. It shuffles the 12 weakest and asks for the fact to be taught
   from a *different angle*, not reworded.
2. **Topics next.** It rotates through the profile's topics by date, about 5
   cards per topic. A deck with a `start:` date is a curriculum step instead:
   nothing before that day, then its topics once, in order from the first,
   `daily_quota / 5` a day, then nothing, weak cards included. Decks with
   start dates run one after another; `ankigen validate` prints the timeline
   and fails if two running at once want more than `global_quota`. Those
   dates are the curriculum's days, not the calendar's: a plan run with no
   date writes the day after the latest `ankigen::run_<date>` tag in the
   collection (`ankigen next`), so extra runs move the curriculum ahead and
   the schedule carries on from there. Cards for one deck by hand are tagged
   `ankigen::ad-hoc` as well and do not count.
3. **No topics?** It asks the model to find what the deck is missing.

Each prompt is built from the `prompts/generate.txt` template and contains:

- the learner profile and style rules
- **few-shot examples**: real cards from your deck, so new cards match your
  phrasing and length
- a **do-not-duplicate list**: the 40 existing cards most related to today's
  topic. Unrelated cards are left out to save tokens.
- the JSON format the answer must follow

This stage is **deterministic**: the same date always produces the same plan.
It makes no network calls, which is why `ankigen plan` is instant.

#### ③ generate: execute

| | |
|---|---|
| **Input** | `requests` |
| **Output** | `generated_cards` (with token counts) |
| **Tools** | `openai` SDK → any OpenAI-compatible provider (**Groq** `openai/gpt-oss-120b`, or **OpenRouter** `nvidia/nemotron-3-super-120b-a12b:free`); `google-genai` for Gemini |

1. Send the prompt and ask for JSON mode. If a model refuses JSON mode, switch
   to reading the JSON out of plain text, and remember that for the model.
2. On a rate-limit error, wait as long as Groq says ("try again in 7s"), up to 3
   attempts.
3. Parse and clean each card, and repair broken cloze syntax.
4. If fewer cards came back than asked for, ask **once** more for the rest.
5. If one request fails, the others carry on.

**This is the only stage whose output changes from run to run.** Every later
stage reads what it stored, so retrying any later stage never calls the model
again.

#### ④ verify: fault check

| | |
|---|---|
| **Input** | `generated_cards` |
| **Output** | `verified_cards` (passed, score, reason) |
| **Tools** | the same LLM provider, with the `prompts/verify.txt` fact-checker prompt |

One call per request checks all of its cards. A card **passes** only if it is
*correct*, *answerable*, and scores at least 0.7.

If the checker itself fails (outage or rate limit), cards pass through marked
`unverified` and are tagged so in Anki. A flaky checker should not make the day
produce nothing, and you review everything in the inbox anyway.

#### ⑤ dedup: hazard check

| | |
|---|---|
| **Input** | cards that passed verify; `raw_notes`; cards kept by earlier runs |
| **Output** | `dedup_results` (is it a duplicate, why, how similar) |
| **Tools** | `difflib` (fuzzy match), an embedding provider (**Ollama** `nomic-embed-text` locally, or **OpenRouter** `liquid/lfm-2.5-embedding-350m:free`), `numpy` (vectorised cosine), `embedding_cache` |

Two checks. The cheapest one runs first:

1. **Fuzzy**: question wording at least 0.85 similar → duplicate.
2. **Semantic**: embedding cosine similarity at least 0.90 → duplicate. This
   catches rewordings the fuzzy check misses.

Each card is compared with its deck and subdecks, with cards kept by earlier
runs that you haven't imported yet, and with cards already kept in this run.
Only decks the profile targets are embedded, so a 5,000-card German deck costs
nothing. If embeddings are unavailable, only the fuzzy check runs, and the
report says so.

#### guide: the explanations

| | |
|---|---|
| **Input** | `card_outcomes` view, filtered to `outcome = 'kept'`, with `requests` |
| **Output** | `guide_sections`; `data/out/<date>/guide_<date>.html` and `.pdf` |
| **Tools** | the writer's LLM (`prompts/guide.txt`), the checker's (`prompts/guide_check.txt`), `markdown`, WeasyPrint |

1. Number the chapters (one per request, decks in the profile's order, topics
   in the curriculum's) and the cards under them, **before** any model call,
   so each card's reference (`§2.3`) holds even if its chapter is not written.
2. Per chapter, one call writes an opening and a section per card, and one
   checker call reads it. A disputed section is kept and marked; one never
   checked is marked unchecked.
3. Render HTML, and a PDF when WeasyPrint is installed.

Runs after images so drawn tables and diagrams appear in it, and before export
so the notes carry their reference. Like verify, it never fails the run: a
spent allowance leaves the rest of the guide "not written today".

#### ⑥ export: write back

| | |
|---|---|
| **Input** | `card_outcomes` view, filtered to `outcome = 'kept'` |
| **Output** | `data/out/<date>/ankigen_<date>.apkg`; `data/curated/<table>/run_date=<date>/part-0.parquet` |
| **Tools** | `genanki`, DuckDB `COPY … TO … (FORMAT PARQUET)` |

- New notes go into **`AnkiGen Inbox::<deck>`** and are tagged `ankigen`,
  `ankigen::run_<date>` and `ankigen::<reason>`.
- A note's **GUID comes from the card's id**. Importing the same day twice
  updates those notes instead of duplicating them.
- It uses the same note-type IDs your collection already has, so no duplicate
  note types are created.
- If a rerun keeps nothing, the old `.apkg` is deleted rather than left behind.

#### ⑦ report: performance counters

| | |
|---|---|
| **Input** | `pipeline_runs` + `card_outcomes` |
| **Output** | `run_report.json` (read it with `ankigen report`) |
| **Tools** | DuckDB SQL, `json` |

This is a separate last stage, so every earlier stage has finished when the
report is written. It shows per-stage timings, outcomes per deck, every dropped
card with its reason, and token use.

---

## 4. The warehouse, as a memory map

One DuckDB file, `data/warehouse.duckdb`, split into layers:

| Layer | Table / view | Written by | Load pattern |
|---|---|---|---|
| raw | `raw_notes` | ingest | full snapshot per date |
| raw | `raw_revlog` | ingest | incremental, by review id |
| plan | `requests` | target | replaces that date's rows |
| stage | `generated_cards` | generate | replaces that date's rows |
| stage | `verified_cards` | verify | replaces that date's rows |
| stage | `dedup_results` | dedup | replaces that date's rows |
| mart | `card_outcomes` *(view)* | derived | joins the above: `kept` / `dropped_verify` / `dropped_duplicate` |
| cache | `embedding_cache` | dedup | keyed by (content hash, model) |
| stage | `guide_sections` | guide | replaces that date's rows |
| ops | `pipeline_runs` | orchestrator | one row per (date, stage): status, rows, timing, detail |

**The idempotency guarantee.** In one transaction, a stage deletes that date's
rows and inserts the new ones (`Warehouse.replace_partition`). Running a stage
twice leaves one copy of the data, never two. That is what makes retries and
backfills safe, the way a CPU can re-execute an instruction after an exception
without leaving a side effect behind.

Inserts go through **Arrow**, not `executemany`. `executemany` costs about
0.6 ms per row, so the 142k-row review history took minutes. Arrow loads it in
under a second.

Files on disk:

```
data/
  warehouse.duckdb                       all tables above
  raw/<date>/collection.anki2            that day's snapshot
  out/<date>/ankigen_<date>.apkg         import into Anki
  out/<date>/run_report.json             what happened
  curated/<table>/run_date=<date>/       Parquet, laid out the way S3 will be
```

---

## 5. Control signals: failure behaviour

| Where it fails | What happens | Why |
|---|---|---|
| Anki is open (collection locked) | ingest retries 3 times, then asks you to close Anki | Anki holds the file exclusively |
| Profile names a deck that doesn't exist | target stops before any LLM call | fail early and cheaply |
| One generation request errors | logged; the other requests continue | one bad topic shouldn't cost the day |
| Rate limited | wait as long as the provider says, then retry | respect the free tier |
| Model refuses JSON mode | read JSON from plain text and remember it | providers differ |
| Fact-checker unavailable | cards pass, tagged `ankigen::unverified` | human triage is the last line of defence |
| Embeddings unavailable | fuzzy check only; the report says so | degrade, don't crash |
| Any stage raises | `pipeline_runs.status = 'failed'`, error saved | visible, and can be retried |

---

## 6. Tool map

| Tool | Used for | Stage(s) |
|---|---|---|
| `sqlite3` backup API | consistent snapshot, including the WAL | ingest |
| DuckDB | warehouse, SQL, Parquet export | all |
| PyArrow | fast bulk inserts | all writers |
| Pydantic + pydantic-settings | profile validation, `.env` settings | target, config |
| PyYAML | read the profile | target |
| `string.Template` + `importlib.resources` | prompt templates shipped inside the package | target, verify |
| `openai` SDK | any OpenAI-compatible provider, for both chat and embeddings | generate, verify, dedup |
| Groq, OpenRouter, NVIDIA, Mistral, Gemini, Ollama | write and fact-check cards | generate, verify |
| Ollama (local) or OpenRouter (hosted, free model) | embeddings for dedup | dedup |
| NumPy | vectorised similarity | dedup |
| `difflib` | fuzzy wording match | dedup |
| genanki | writes `.apkg` packages | export |
| cronsim | reads each pipeline's cron in its own time zone | the tick |
| `markdown` + WeasyPrint | the study guide, as HTML and PDF | guide |
| Typer | CLI | interface |
| pytest | 73 offline tests, using fake LLM and embeddings | — |

---

## 7. Several pipelines

Everything above is one **pipeline**: one profile, one warehouse, one
curriculum. A repository holds several, one folder each under `pipelines/`,
and a run is one of them, chosen with `ANKIGEN_PIPELINE`:

| | per pipeline | shared |
|---|---|---|
| what to teach | `pipelines/<id>/profile.yaml` | |
| how to run it | `pipelines/<id>/pipeline.yaml`: cron and time zone, outputs, models, budget | |
| storage | `data/pipelines/<id>/` (warehouse, snapshots, output) | `data/anki/`, the AnkiWeb working copy |
| curriculum | the days its own decks have cards for | |
| credentials | | the repository's secrets |

**The curriculum queue is per pipeline.** The next curriculum day is the day
after the last one the collection has cards for, and the collection is
shared, so only notes in the pipeline's own decks count; two pipelines may not
share a deck. A pipeline that never pushes counts the days in its warehouse
instead, since its cards never reach the collection.

**The scheduler is a tick.** GitHub's schedule is one cron per workflow file,
so `tick.yml` runs hourly, asks `ankigen pipelines due` which crons have fired
since each pipeline last ran, and runs those through `run-pipeline.yml`, one
after another: one AnkiWeb collection means one sync at a time. Each run
records its outcome on the `ankigen-status` branch, which is both the tick's
memory and what the Android app reads:

```
tick (hourly) ──▶ pipelines due? ──▶ run-pipeline (one at a time) ──▶ AnkiWeb
      ▲                                     │
      └──────── ankigen-status branch ◀─────┘   latest.json, runs/, cards/, curriculum.json
```

`pipelines.py` imports nothing heavy, so the tick decides what is due after
installing only pydantic and cronsim.

## 8. What the future phases swap out

Levels L0 to L3 stay the same. Later phases only replace orchestration (L4) and
where the data lives (L0):

| Phase | Replaces | With |
|---|---|---|
| 2 Docker | the Python environment you run it in | one image, with the collection and profile mounted read-only |
| 3 Airflow | L4 `pipeline.run` | DAG `ankigen_daily`: one task per stage, `{{ ds }}` as `run_date`, `catchup` for backfills |
| 4 AWS | L0 local files | S3 `raw/`, `curated/`, `gold/`; DuckDB reads Parquet straight from S3 |
| 5 Kubernetes | L4 again | `CronJob`, then `KubernetesPodOperator`, one pod per stage |

This is only possible because every stage already has the same contract:
`stage(context, run_date)` reads the warehouse and replaces its own partition.
