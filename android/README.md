# AnkiGen for Android

A phone view of the pipelines. There is no server: the repository is the
backend.

| The app | Reads or writes |
|---|---|
| Pipelines, their schedule and next run | `pipelines/<id>/pipeline.yaml` on the default branch |
| Latest run, past runs, each day's cards, the curriculum | the `ankigen-status` branch, written by every run |
| Setup: name, on/off, cron, time zone, guide, push, call budget | commits `pipeline.yaml` (CI validates it) |
| Run the next day now | dispatches `run-pipeline.yml` |

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

```bash
./gradlew testDebugUnitTest assembleDebug
```

Needs JDK 17 (Android Studio's `jbr` works) and the Android SDK. On Windows,
this repository's folder name contains U+2800, which the Gradle wrapper and the
test JVM cannot put on a classpath. Build from a drive letter instead:
`subst A: <repo>`, then run Gradle from `A:\android`.
