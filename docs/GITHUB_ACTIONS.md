# Running the daily pipeline without leaving a PC on

GitHub Actions runs the pipeline on GitHub's machines: each run gets a clean
Linux VM, does the work, and is destroyed. On a public repository the standard
runners are free, with no minute limit and no card on file, which makes this the
cheapest always-on option there is.

Because the runner cannot see your machine, everything it needs is fetched or
rebuilt on the spot:

| it needs | where it comes from |
|---|---|
| the code | cloned from this repo |
| your collection | synced down from AnkiWeb |
| embeddings | `fastembed`, in-process — there is no Ollama on a runner |
| past runs | the warehouse, restored from the Actions cache |
| the cards | synced back to AnkiWeb, and kept as an artifact |

Nothing is imported by hand: the run adds the cards to your collection and syncs
them up, and your phone and desktop pull them down like any other change.

## Setup

Secrets, under Settings → Secrets and variables → Actions.

**`CLAUDE_CODE_OAUTH_TOKEN`** — optional, and what writes the cards when set:
Claude Opus writes them and Claude Sonnet checks them, on your Claude
subscription (Pro or Max), through the Claude Code CLI. Make one locally with
`claude setup-token` and store it with `gh secret set CLAUDE_CODE_OAUTH_TOKEN`
(it prompts for the value). The code removes any Anthropic API key from the
CLI's environment, so these calls can only ever run on the subscription.
Whenever Claude cannot answer (the plan's usage limit reached, a missing or
expired token), the rest of that run goes to Gemini instead. Keep *extra
usage* off at claude.ai/settings/usage if a reached limit should mean Gemini
rather than paid overage.

**`GOOGLE_API_KEY`** — your Gemini key: the fallback above, and what checks
pictures found on the web.

**`ANKIWEB_KEY`** — a sync token, not your password. Get one locally:

```bash
pip install ".[push]"
ankigen push --login
```

It prints `ANKIWEB_KEY=...`. Add that as the secret, and put it in your local
`.env` too so `pull` and `push` work from your machine. Changing your AnkiWeb
password invalidates it.

Optionally set *variables* (not secrets) to change models without editing the
workflow, each a comma-separated preference order, best first:
`CLAUDE_MODEL` and `CLAUDE_VERIFY_MODEL` for Claude's writer and checker,
`GEMINI_MODEL` and `VERIFY_MODEL` for Gemini's. `LLM_PROVIDER=gemini` and
`VERIFY_PROVIDER=gemini` put Gemini back in front.

Then Actions → **daily cards** → Run workflow. After that it runs daily at
08:17 UTC (05:17 in São Paulo), with a backup at 11:43 UTC that does nothing if
the first one already succeeded.

## From your phone

Everything below works from the GitHub app: Actions, pick the workflow, then
**Run workflow**.

**The next day of the curriculum, now.** *daily cards* with every field
blank. Each run writes the day after the last one in your collection, so this
adds tomorrow's cards today, and the scheduled run then writes the day after
that: nothing is repeated, nothing skipped, and every later deck moves up a day.
It is how to go faster than one day a day, as often as you can keep up with.
The run's summary names the day it wrote, which is also the cards' tag
(`tag:ankigen::run_<day>`); `added:1` in Anki finds everything added today.

**More cards on one subject, now.** *daily cards* with **deck** set (and
optionally **topic**, **prompt**, **count**) writes for that deck instead of
the day's plan. The cards reach your phone on its next sync. They are not a
day of the curriculum and do not move it along. The same thing
locally:

```bash
ankigen run --deck "Data Platform::Kubernetes" --topic "probes" --prompt "Contrast what happens to traffic when each one fails."
```

**A new subject in the daily rotation.** *add a theme* with a **deck** name and
a sentence **about** it. The model plans an ordered list of topics in the same
shape as your other decks and proposes it as a change to the profile: open the
run's summary for the link and the topics. Merge it to start the subject, or
close it. When the profile is a curriculum (decks with `start:` dates), the new
deck joins the end of it at the same pace, starting the day after the last one
finishes. Leave **quota** blank for that. Locally: `ankigen add-theme --deck "..." --about "..."`.

**Redo a day's pictures.** *daily cards* with **stages** set to
`images,export,report` and that day's **run_date**. No new cards are written;
the push puts the new pictures on the day's notes and leaves everything else
alone.

**A study guide for a day that has none.** *daily cards* with **stages** set
to `guide,export,report` and that day's **run_date**. The guide is written
again from that day's kept cards. Notes already in your collection keep what
they have; only cards pushed for the first time get the `Guide … · §` line.

## The study guide

Each run also writes `guide_<day>.pdf`: a chapter per topic, opening with how
the thing works, then a section per card with why its answer is what it is, an
example, the usual mistakes and what to look at next. Every card says where
its section is (`Guide 2026-10-03 · §2.3`, under the answer). It is in the
run's **cards-<day>** artifact, next to the `.apkg`: open the run in the
GitHub app, scroll to *Artifacts*, download, and open the PDF from the zip.
Artifacts are kept for 90 days.

The checker reads the guide too. A section it disagrees with stays in, marked
with what it disputes; if the day's allowance runs out, the remaining chapters
say so and the cards go out regardless.

## Pictures

Most pictures are drawn from the card itself: when an answer is a comparison
or a flow, the model that writes the card also describes a table or a
Graphviz diagram, the checker verifies it with the card, and it is drawn into
the note as HTML or SVG in the card's own colours. Cards that need a real
picture (a screenshot, a photograph) are searched for on the web, and the
result is shown to a vision model with the card before it is used.

## What to know

**Pictures travel inside the notes.** The runner never syncs media: for its
working copy that would mean downloading your whole media folder, and media
sync runs in the background, where closing the collection cancelled it — the
first pushed pictures arrived as broken-image icons for exactly that reason.
Drawn pictures are a few KB of SVG or HTML, and a searched picture is
re-encoded small (640px JPEG) and stored as a `data:` URI. A push replaces a
picture only with a newer one, and never takes a working picture off a note.

**The model chain has backups.** Free tiers meter each model separately, so the
run walks down `GEMINI_MODEL` (a repository variable here) as models run out or
stay busy, then tries `gemini-3-flash-preview` and Gemma 4, which have daily
allowances of their own. A busy model is tried once and skipped for ten
minutes: on the free tier even a 503 seems to count against the day.

**The sync refuses to guess.** If AnkiWeb reports that the runner's copy and
yours have diverged beyond a normal merge, the job stops. Resolving that means
declaring one side the winner, and choosing the runner's could discard review
history. Sync from Anki on your own machine, then re-run; if it persists, clear
the `anki-collection-` cache so the next run starts from a fresh download.

**GitHub's schedule is best-effort.** Runs start late under load and are
sometimes dropped altogether, most often on the hour, which is how the first
09:00 run never happened. That is why the schedule sits on an odd minute and has
a backup.

**Scheduled workflows stop after 60 days without a commit.** GitHub disables
them on dormant repositories, and does not tell you.

**Caches can be evicted** after 7 days unused, which a daily schedule prevents.
Losing the collection cache costs a fresh download; losing the warehouse cache
costs about ninety seconds of re-embedding and the memory of which cards were
already made.

**Gemini's free tier is 20 chat requests per day, per model**, which is why both
model settings are chains. A run costs roughly ten calls plus one per image
checked.

**Snapshots are kept for 14 days.** Each run stores a full snapshot of your
notes, and only that day's is ever read, so ingest drops the ones older than
two weeks. Without that the warehouse — which rides in the Actions cache,
restored and saved every run — grew by megabytes a day, forever.
