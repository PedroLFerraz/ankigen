# Android app

## What it's for

A phone view of the pipelines: see when each one runs next, what the last run
made, and where the curriculum is; change a pipeline's setup or its plan; run
the next day now; and keep every study guide in a folder on the phone. There
is no server: the repository is the backend.

What each screen does, how to install the APK and how to sign in are in
[android/README.md](../../android/README.md), next to the code.

## Entry points

The screens (`Screens.kt`): the home screen lists the pipelines and has
**New pipeline**; a pipeline has four tabs, **Latest**, **Curriculum**,
**Runs** and **Setup**; and **Settings** holds the token, the code branch and
the guides folder.

| In the app | Does |
|---|---|
| Setup: save | commits `pipelines/<id>/pipeline.yaml` |
| Run the next day now | dispatches `run-pipeline.yml` |
| Curriculum: change the plan; New pipeline | dispatches `edit-plan.yml`; **Apply** merges its `plan/` branch, **Discard** deletes it |
| Study guides | downloads each run's artifact into the folder picked in Settings, every 6 hours and on reload |

## Reads / writes

| Reads | Writes |
|---|---|
| `pipelines/<id>/pipeline.yaml` on the default branch (or the **Code branch** set in Settings) | commits to `pipeline.yaml`, which CI validates against `pipelines/schema.json` |
| the `ankigen-status` branch: `latest.json`, `runs/`, `cards/`, `curriculum.json` | workflow dispatches: `run-pipeline.yml`, `edit-plan.yml` |
| `plan/<pipeline>/...` branches: each round's request, reply and diff | merges or deletes those branches |
| run artifacts (the guide PDFs), with the token | `<pipeline>/<pipeline> <day>.pdf` in the guides folder |

Reading needs no sign-in, since the repository is public. Writing and
downloading artifacts need a fine-grained token for this repository only,
with Contents and Actions read and write, kept in the app's private storage.

## Depends on

The GitHub REST API (`GitHub.kt`), and the files the Python side writes: the
`pipeline.yaml` format ([Profiles and pipelines](profiles-and-pipelines.md))
and the status branch ([GitHub Actions](github-actions.md)). Kotlin and
Jetpack Compose with Material 3; Android 8.0 (API 26) and up.

## How to run and test it

From `android/`, with JDK 17 and the Android SDK:

```bash
./gradlew testDebugUnitTest assembleDebug
```

The APK is `app/build/outputs/apk/debug/app-debug.apk`. The unit tests cover
the cron reader (`CronTest`, which must read a cron the way the tick does),
the `edit-plan` draft rounds (`DraftTest`) and `pipeline.yaml`
(`SpecTest`, which reads the repository's own `data-platform` pipeline).
[android.yml](../../.github/workflows/android.yml) runs them on every change
to `android/`.

## Limits and known issues

- Every build signs with the debug key in `app/debug.keystore`, so a new APK
  installs over the old one. The first app (0.1.0) used another key and has
  to be uninstalled first.
- `pipeline.yaml` is written from a template, so comments in it are lost
  when the app saves it.
- With a **Code branch** set, runs are still dispatched, but GitHub only
  dispatches workflows that exist on the default branch.
- GitHub keeps a run's artifacts for 90 days; a guide not saved by then is
  gone.
