# GitHub Actions

## What it's for

GitHub Actions runs the pipelines without leaving a machine on: each run gets
a clean Linux VM, syncs your collection down from AnkiWeb, writes the cards,
syncs them back, and records what it did on the `ankigen-status` branch. The
same workflows check every change and build the Docker image and the Android
APK. Free on a public repository.

Setting it up (the secrets, the first dry run) and using it from a phone are
in [GITHUB_ACTIONS.md](../GITHUB_ACTIONS.md). This page is the map of the
workflows.

## Entry points

The eight workflows in `.github/workflows/`:

| Workflow | Name in Actions | Triggered by | What it does |
|---|---|---|---|
| [tick.yml](../../.github/workflows/tick.yml) | tick | every hour at :23; by hand, with **dry run** and a pretend **now** | Asks `ankigen pipelines due` which pipelines' crons have fired since they last ran, and runs each through `run-pipeline`, one after another. |
| [run-pipeline.yml](../../.github/workflows/run-pipeline.yml) | run pipeline | `tick` (`workflow_call`); by hand or from the app, with **pipeline** and optionally **deck**, **topic**, **prompt**, **count**, **kind**, **run_date**, **stages** | `ankigen pull`, `run`, `push`, then the run's summary, the cards artifact, and `pipelines record` on the status branch. |
| [edit-plan.yml](../../.github/workflows/edit-plan.yml) | edit plan | by hand or from the app's Curriculum tab, with **pipeline**, **request**, **branch** (`plan/<pipeline>/...`) and **new** | Claude Code edits `pipelines/<id>/` on the draft branch, checks it with `ankigen pipelines validate`, and commits the request and its reply. |
| [add-theme.yml](../../.github/workflows/add-theme.yml) | add a theme | by hand, with **pipeline**, **deck**, **about**, **quota** | `ankigen add-theme`, proposed as a branch and a pull request ([Book, themes and parts](book-themes-parts.md)). |
| [curriculum.yml](../../.github/workflows/curriculum.yml) | curriculum | a push to `master` that changes `pipelines/**`; by hand | Rewrites every `curriculum.json` on the status branch, so a merged plan shows in the app without waiting for the next run. |
| [tests.yml](../../.github/workflows/tests.yml) | tests | every pull request and push to `master` | One job per component, each with only the extras and system packages its tests need, plus `pipelines validate`; then an `e2e` job with everything installed. |
| [docker.yml](../../.github/workflows/docker.yml) | docker | pull requests and pushes to `master` that touch the image | Builds the [Docker](docker.md) image, scans it with Trivy, and on `master` pushes it to ghcr.io. |
| [android.yml](../../.github/workflows/android.yml) | android | changes to `android/`; by hand | Unit tests and a debug APK; on `master` it replaces the APK on the `app-latest` pre-release ([Android app](android-app.md)). |

The outer loop, from a change of plan to cards on your phone:

```
app or phone ─▶ edit-plan / add-theme ─▶ merge into master ─▶ curriculum
                                                        │
tick (hourly) ─▶ pipelines due? ─▶ run-pipeline ─▶ AnkiWeb ─▶ your devices
      ▲                                   │
      └────── ankigen-status branch ◀─────┘ ─▶ the app reads it
```

## Reads / writes

| A run needs | Where it comes from |
|---|---|
| the code | cloned from this repo |
| your collection | synced down from AnkiWeb, cached between runs (`anki-collection-`) |
| embeddings | `fastembed`, in-process: there is no Ollama on a runner |
| past runs | the pipeline's warehouse, restored from the Actions cache |
| keys | the repository's secrets: `CLAUDE_CODE_OAUTH_TOKEN`, `GOOGLE_API_KEY`, `ANKIWEB_KEY`, and `LLM_API_KEY` for an OpenAI-compatible writer |

What a run leaves behind:

- the cards in your collection, through AnkiWeb;
- a `cards-<pipeline>-<day>` artifact with the `.apkg`, `cards.json`, the
  study guide and the run report, kept for 90 days;
- on the `ankigen-status` branch, per pipeline: `latest.json`, `runs/`,
  `cards/`, `scheduler.json` and `curriculum.json`. This is both the tick's
  memory and what the [Android app](android-app.md) reads.

`edit-plan` writes `plan/<pipeline>/...` branches, `add-theme` writes a
proposal branch and pull request, and `android` writes the `app-latest`
release.

## Depends on

The [CLI](cli.md), the [pipelines](profiles-and-pipelines.md) and
[AnkiWeb sync](ankiweb-sync.md). `run-pipeline` installs the package with
`[gemini,push,guide]`, Graphviz and Pango, and the Claude Code CLI, the same
set as the [Docker](docker.md) image. `tick` installs only pydantic and
cronsim.

## How to run and test it

From Actions (or the GitHub app): **tick** → Run workflow with **dry run**
ticked lists what is due without running anything. **run pipeline** with
only **pipeline** set writes that pipeline's next day now.

The workflows have no tests of their own; what they run does.
`ankigen pipelines due --help` and `ankigen pipelines env <id>` show
locally what the tick and a run would use.

## Limits and known issues

[GITHUB_ACTIONS.md](../GITHUB_ACTIONS.md#what-to-know) lists them: the
best-effort schedule (why the tick runs at :23), scheduled workflows stopping
after 60 days without activity, one run at a time and what GitHub cancels,
cache eviction, the Gemini free tier, and how long snapshots are kept.

Deploying to AWS through OIDC instead of running on GitHub's machines is
planned in [#60](https://github.com/PedroLFerraz/ankigen/issues/60).
