# Running the pipelines without leaving a PC on

GitHub Actions runs the pipelines on GitHub's machines: each run gets a clean
Linux VM, does the work, and is destroyed. On a public repository the standard
runners are free, with no minute limit and no card on file, which makes this the
cheapest always-on option there is.

Because the runner cannot see your machine, everything it needs is fetched or
rebuilt on the spot:

| it needs | where it comes from |
|---|---|
| the code | cloned from this repo |
| your collection | synced down from AnkiWeb (one for every pipeline) |
| embeddings | `fastembed`, in-process — there is no Ollama on a runner |
| past runs | the pipeline's warehouse, restored from the Actions cache |
| the cards | synced back to AnkiWeb, and kept as an artifact with the study guide |
| what already ran | the `ankigen-status` branch |

Nothing is imported by hand: the run adds the cards to your collection and syncs
them up, and your phone and desktop pull them down like any other change.

## Pipelines

A pipeline is a folder under `pipelines/`: a `profile.yaml` saying what to
teach and a `pipeline.yaml` saying when to run it, what to produce, which
models to use and how many calls a run may make. Both files, and the
`ankigen pipelines` commands, are described in
[Profiles and pipelines](components/profiles-and-pipelines.md).

**How the schedule works.** GitHub's own schedule is one cron per workflow
file, so a workflow, **tick**, runs every hour at :23. It reads each pipeline's
cron in the pipeline's time zone, checks `ankigen-status` for what already ran,
and runs the due pipelines one after another (they share one AnkiWeb
collection, so only one syncs at a time). A pipeline therefore runs within about
an hour of its time. Times the tick missed collapse into one run: the
curriculum is a queue, so a day is written late, never skipped. A failed run is
tried again on the next two ticks, then waits for its next scheduled time.

**What a run leaves behind.** On the `ankigen-status` branch, per pipeline:
`latest.json` (what the last run made, or why it failed), `runs/` (one file per
run), `cards/` (each day's cards), `scheduler.json` (the last scheduled time
and how it went) and `curriculum.json` (every topic, and which are done or
next). This is what the app reads.

**Shared allowances.** Every pipeline uses the same Claude subscription and
the same fallback provider's allowance, if one is set; `budget.max_llm_calls_per_run` caps one run's share
([Profiles and pipelines](components/profiles-and-pipelines.md#the-pipeline-file)).

## Setup

Secrets, under Settings → Secrets and variables → Actions. They are shared by
every pipeline.

**`CLAUDE_CODE_OAUTH_TOKEN`** — what writes and checks the cards, and looks at
the pictures: Claude Opus writes them and Claude Sonnet checks them, on your Claude
subscription (Pro or Max), through the Claude Code CLI. Make one locally with
`claude setup-token` and store it with `gh secret set CLAUDE_CODE_OAUTH_TOKEN`
(it prompts for the value). The code removes any Anthropic API key from the
CLI's environment, so these calls can only ever run on the subscription.
Whenever Claude cannot answer (the plan's usage limit reached, a missing or
expired token), the run fails before the push step and the next tick writes the
whole day again, unless the pipeline names a `fallback` provider. Keep *extra
usage* off at claude.ai/settings/usage if a reached limit should mean a failed
run rather than paid overage.

**`ANKIWEB_KEY`** — a sync token, not your password. Get one locally:

```bash
pip install ".[push]"
ankigen push --login
```

It prints `ANKIWEB_KEY=...`. Add that as the secret, and put it in your local
`.env` too so `pull` and `push` work from your machine. Changing your AnkiWeb
password invalidates it.

**`LLM_API_KEY`** — only for a pipeline whose writer is an OpenAI-compatible
provider such as `groq` or `openrouter`.

Models are set per pipeline, in `pipeline.yaml`'s `models:`, each chain a
preference order, best first.

Then Actions → **tick** → Run workflow with **dry run** ticked: its summary
lists what is due. After that it runs every hour by itself.

## From your phone

Everything below works from the GitHub app: Actions, pick the workflow, then
**Run workflow**.

**The next day of a curriculum, now.** *run pipeline* with **pipeline** set
and every other field blank. Each run writes the day after the last one the
pipeline's decks have, so this adds tomorrow's cards today, and the scheduled
run then writes the day after that: nothing is repeated, nothing skipped, and
every later deck moves up a day. It is how to go faster than one day a day, as
often as you can keep up with. The run's summary names the day it wrote, which
is also the cards' tag (`tag:ankigen::run_<day>`); `added:1` in Anki finds
everything added today.

**More cards on one subject, now.** *run pipeline* with **deck** set (and
optionally **topic**, **prompt**, **count**) writes for that deck instead of
the day's plan. The cards reach your phone on its next sync. They are not a
day of the curriculum and do not move it along. The same thing
locally:

```bash
ankigen run --deck "Data Platform::Kubernetes" --topic "probes" --prompt "Contrast what happens to traffic when each one fails."
```

**A new subject.** *add a theme* with the **pipeline**, a **deck** name and a
sentence **about** it. The model plans an ordered list of topics in the same
shape as the pipeline's other decks and proposes it as a change to its profile:
open the run's summary for the link and the topics. Merge it to start the
subject, or close it. When the profile is a curriculum (decks with `start:`
dates), the new deck joins the end of it at the same pace, starting the day
after the last one finishes. Leave **quota** blank for that. Locally:
`ankigen add-theme --deck "..." --about "..."`.

**Change a plan, in words.** *edit plan* (what the app's Curriculum tab
sends) with the **pipeline**, a **request** and a draft **branch**
(`plan/<pipeline>/<anything>`). Claude Code edits `pipelines/<id>/` on that
branch, checks it with `ankigen pipelines validate`, and commits the round:
the request and its reply. Ask again on the same branch to iterate; merge it
to apply, delete it to discard. Once merged, *curriculum* rewrites the
curriculum the app shows, without waiting for the next run.

**A new pipeline.** From the app: **New pipeline**, which is *edit plan* with
**new** set, so Claude plans the whole curriculum on a draft. By hand: add a
folder under `pipelines/` with a `pipeline.yaml` and a `profile.yaml` (copy
another pipeline's and change it), and push. The next tick picks it up at
its scheduled time.

**Redo a day's pictures.** *run pipeline* with **stages** set to
`images,export,report` and that day's **run_date**. No new cards are written;
the push puts the new pictures on the day's notes and leaves everything else
alone.

**A study guide for a day that has none.** *run pipeline* with **stages** set
to `guide,export,report` and that day's **run_date**. The guide is written
again from that day's kept cards; see the
[Study guide](components/study-guide.md) for what reaches notes already in
your collection.

## The study guide

A pipeline with `guide_pdf: true` also writes `guide_<day>.pdf`
([Study guide](components/study-guide.md)). It is in the run's
**cards-<pipeline>-<day>** artifact, next to the `.apkg` and `cards.json`:
open the run in the GitHub app, scroll to *Artifacts*, download, and open the
PDF from the zip. Artifacts are kept for 90 days.

## What to know

**Pictures travel inside the notes**, because the runner never syncs media,
and **the sync refuses to guess** when AnkiWeb reports the copies have
diverged: both are explained on [AnkiWeb sync](components/ankiweb-sync.md).
If a run stops on a diverged sync, sync from Anki on your own machine, then
re-run; if it persists, clear the `anki-collection-` cache.

**The model chain has backups**: the run walks down each pipeline's chains as
models run out or stay busy ([LLM providers](components/llm-providers.md)).

**One run at a time.** Runs share a concurrency group, because they share the
collection. GitHub keeps only one run waiting in a group and cancels an older
waiting one for a newer, which is why the tick runs due pipelines one after
another itself, and why starting several runs by hand at once leaves only the
first and the last. A cancelled scheduled run is due again on the next tick.

**GitHub's schedule is best-effort.** Runs start late under load and are
sometimes dropped altogether, most often on the hour, which is how the first
09:00 run never happened. That is why the tick sits on an odd minute; and since
it runs hourly, a dropped tick only delays a pipeline to the next one.

**Scheduled workflows stop after 60 days without repository activity.** GitHub
disables them on dormant repositories, and does not tell you.

**Caches can be evicted** after 7 days unused, which a daily schedule prevents.
Losing the collection cache costs a fresh download; losing a warehouse cache
costs about ninety seconds of re-embedding and the memory of which cards were
already made.

**A run that Claude cannot finish fails** instead of pushing a short or
unchecked day; what a run costs is on
[LLM providers](components/llm-providers.md).

**Snapshots are kept for 14 days.** Each run stores a full snapshot of your
notes, and only that day's is ever read, so ingest drops the ones older than
two weeks. Without that the warehouse — which rides in the Actions cache,
restored and saved every run — grew by megabytes a day, forever.
