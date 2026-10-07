# AnkiGen for Android

A phone view of the pipelines. There is no server: the repository is the
backend.

| The app | Reads or writes |
|---|---|
| Pipelines, their schedule and next run | `pipelines/<id>/pipeline.yaml` on the default branch |
| Latest run, past runs, each day's cards, the curriculum | the `ankigen-status` branch, written by every run |
| Setup: name, on/off, cron, time zone, guide, push, call budget | commits `pipeline.yaml` (CI validates it) |
| Run the next day now | dispatches `run-pipeline.yml` |
| Change the plan, New pipeline | dispatches `edit-plan.yml`; Apply merges its `plan/` branch, Discard deletes it |
| Study guide PDFs, saved to a folder you pick | each run's artifact (needs the token) |

**Where the curriculum is.** Each run writes the day after the last one
written, whenever it runs, so a run by hand pulls the whole plan forward and
the dates in `profile.yaml` stop matching the days they are written on. The
app numbers the days instead: "next run writes day 15 of 82 · 9 days ahead of
the plan", and beside each day still to come, when the schedule will write it.

**Changing the plan.** On the Curriculum tab, say what to change. Claude
edits the plan on a draft branch and replies; the draft shows each request,
each reply and the diff. Ask for more until it is right, then Apply (merged,
and the curriculum updates within a minute) or Discard. **New pipeline** on
the home screen works the same way: describe the subject and Claude plans
every deck. Uses the Claude subscription, about a minute a round.

**Study guides.** In Settings, choose a folder (on the phone, or in a cloud
app such as Drive). Every guide is saved there, in a folder per pipeline, as
`<pipeline>/<pipeline> <curriculum day>.pdf`: every 6 hours in the
background, and whenever the app reloads. A guide deleted from its folder is
not saved again. Any day's guide opens from
the Runs tab, and is saved again then if it is missing. GitHub keeps a
run's files for 90 days.

## Install

Download `ankigen.apk` from the
[app-latest](https://github.com/PedroLFerraz/ankigen/releases/tag/app-latest)
pre-release, which `android.yml` rebuilds on every change to `android/`, and
allow installs from your browser. Every build signs with the same debug key
(`app/debug.keystore`), so a new APK installs over the old one. The first app
(0.1.0) was signed with another key; uninstall it first.

Reading this public repository needs no sign-in. To save settings or start a
run, open **Settings** and paste a
[fine-grained token](https://github.com/settings/personal-access-tokens/new)
for this repository only, with **Contents** and **Actions** set to read and
write. It stays in the app's private storage.

**Code branch** in Settings reads and saves `pipelines/` on another branch,
for trying a change before it is merged. Runs are still dispatched there, and
GitHub only dispatches workflows that exist on the default branch.

## Build

On this machine, run `build-apk.bat` in the repository root: it builds the
debug APK and drops `AnkiGen.apk` beside it, ready to copy to a phone.

Elsewhere:

```bash
./gradlew testDebugUnitTest assembleDebug
```

Needs JDK 17 (Android Studio's `jbr` works) and the Android SDK. On Windows,
this repository's folder name contains U+2800, which the Gradle wrapper and the
test JVM cannot put on a classpath. Build from a drive letter instead:
`subst A: <repo>`, then run Gradle from `A:\android`.
