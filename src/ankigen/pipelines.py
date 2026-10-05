"""Pipelines: several card-making setups in one repository.

Each one is a folder, pipelines/<id>/, holding two files:

    profile.yaml   what to teach: the learner, the decks, the curriculum.
                   Written by hand or by `add-theme`, comments and all.
    pipeline.yaml  how to run it: when, what to produce, which models, how
                   many calls. Small, and checked against pipelines/schema.json
                   because the Android app writes it.

A run is one pipeline, chosen with ANKIGEN_PIPELINE. GitHub's own schedule
lives in workflow files and cannot be one per pipeline, so an hourly tick
(.github/workflows/tick.yml) asks `due` which pipelines' crons have fired
since they last ran, and runs them one after another: they share one AnkiWeb
collection, so only one may sync at a time.

What a run did is written to the `ankigen-status` branch (`record`), which is
both how the tick knows what already ran and what the app shows.

Nothing here imports DuckDB, the embedding model or genanki at module level,
so the tick can use it after installing only pydantic and cronsim.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from cronsim import CronSim, CronSimError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ankigen import config
from ankigen.config import CLAUDE, NATIVE_PROVIDERS, provider_names
from ankigen.profile import Profile, load_profile

ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
SPEC_FILE = "pipeline.yaml"
PROFILE_FILE = "profile.yaml"
# A scheduled time that failed is tried again on the next ticks, this many
# times in all, and then left for the next scheduled time: a pipeline whose
# models are out for the day should not spend every hour finding that out.
MAX_ATTEMPTS = 3
# A pipeline that has never run on its schedule waits for its cron, unless
# that fired this recently: a tick runs hourly, and GitHub runs them late.
FIRST_RUN_WINDOW = timedelta(hours=2)


# ------------------------------------------------------------ the spec

class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: str = Field(description="Five fields, minute hour day month weekday, read in "
                                  "`timezone`. Each run writes the next curriculum day.")
    timezone: str = Field("UTC", description="An IANA time zone, e.g. America/Sao_Paulo.")

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v.split()) != 5:
            raise ValueError(f"{v!r}: a cron has five fields, minute hour day month weekday")
        try:
            CronSim(v, datetime.now(timezone.utc))
        except CronSimError as e:
            raise ValueError(f"{v!r} is not a cron: {e}") from None
        return v

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise ValueError(f"unknown time zone {v!r}") from None
        return v


class Outputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guide_pdf: bool = Field(True, description="Write the day's study guide as a PDF.")
    push_to_ankiweb: bool = Field(True, description="Put the cards into Anki over AnkiWeb. "
                                                    "Off, they are only in the .apkg.")


class Models(BaseModel):
    """Who writes and who checks. The defaults are what the daily run has used:
    Claude on the subscription, with Gemini's free tier behind it."""
    model_config = ConfigDict(extra="forbid")

    writer: str = "claude"
    writer_models: list[str] = ["claude-opus-5-5", "claude-sonnet-5"]
    checker: str = "claude"
    checker_models: list[str] = ["claude-sonnet-5"]
    fallback: str = Field("gemini", description="Takes over when the writer's provider cannot "
                                                "answer at all. Empty: none.")
    fallback_models: list[str] = ["gemini-3.8-flash", "gemini-3.7-flash",
                                  "gemini-3.6-flash", "gemini-3.5-flash"]
    fallback_checker_models: list[str] = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest",
                                          "gemini-3.1-flash-lite"]

    @field_validator("writer", "checker", "fallback")
    @classmethod
    def _provider(cls, v: str, info) -> str:
        v = v.strip().lower()
        if v or info.field_name != "fallback":
            if v not in provider_names():
                raise ValueError(f"{v!r} is not a provider: {', '.join(provider_names())}")
        return v

    @model_validator(mode="after")
    def _apart(self) -> "Models":
        if self.fallback and self.fallback == self.writer:
            raise ValueError("the fallback must be another provider than the writer")
        if self.fallback and model_var(self.fallback) == model_var(self.writer):
            raise ValueError(f"{self.writer} and {self.fallback} would both be set by "
                             f"{model_var(self.writer)}; pick claude or gemini for one of them")
        return self


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_llm_calls_per_run: int = Field(0, ge=0, description="0: no cap.")


class PipelineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", title="AnkiGen pipeline")

    name: str = Field(min_length=1)
    enabled: bool = True
    schedule: Schedule
    outputs: Outputs = Field(default_factory=Outputs)
    models: Models = Field(default_factory=Models)
    budget: Budget = Field(default_factory=Budget)


@dataclass(frozen=True)
class Pipeline:
    id: str
    dir: Path
    spec: PipelineSpec

    @property
    def profile_path(self) -> Path:
        return self.dir / PROFILE_FILE

    def profile(self) -> Profile:
        return load_profile(self.profile_path)


def model_var(provider: str) -> str:
    """The setting a provider's model chain goes in (see config.Settings)."""
    if provider == CLAUDE:
        return "CLAUDE_MODEL"
    if provider in NATIVE_PROVIDERS:
        return "GEMINI_MODEL"
    return "LLM_MODEL"


def root(cfg=None) -> Path:
    return Path((cfg or config.settings).ankigen_pipelines_dir)


def load(pipeline_id: str, cfg=None) -> Pipeline:
    folder = root(cfg) / pipeline_id
    path = folder / SPEC_FILE
    if not ID.match(pipeline_id) or not path.exists():
        known = ", ".join(p.id for p in all(cfg)) or "none"
        raise FileNotFoundError(f"No pipeline {pipeline_id!r}: {path} does not exist. "
                                f"Pipelines: {known}.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Pipeline(pipeline_id, folder, PipelineSpec.model_validate(data))


def all(cfg=None) -> list[Pipeline]:  # noqa: A001 - pipelines.all() reads well
    found = []
    for path in sorted(root(cfg).glob(f"*/{SPEC_FILE}")):
        found.append(load(path.parent.name, cfg))
    return found


def schema() -> str:
    """The JSON Schema the app checks pipeline.yaml against before committing it."""
    return json.dumps(PipelineSpec.model_json_schema(), indent=2) + "\n"


# ------------------------------------------------------------ running one

def env(p: Pipeline) -> dict[str, str]:
    """The settings a run of this pipeline takes, for $GITHUB_ENV. Secrets are
    not here: every pipeline shares the repository's."""
    m = p.spec.models
    out = {
        "ANKIGEN_PIPELINE": p.id,
        "ANKIGEN_PUSH": "true" if p.spec.outputs.push_to_ankiweb else "false",
        "LLM_PROVIDER": m.writer,
        "VERIFY_PROVIDER": m.checker,
        "FALLBACK_PROVIDER": m.fallback,
        "MAX_LLM_CALLS": str(p.spec.budget.max_llm_calls_per_run),
    }
    if m.writer_models:
        out[model_var(m.writer)] = ",".join(m.writer_models)
    if m.checker_models:
        out["VERIFY_MODEL"] = ",".join(m.checker_models)
    if m.fallback and m.fallback_models:
        out[model_var(m.fallback)] = ",".join(m.fallback_models)
    if m.fallback and m.fallback_checker_models:
        out["FALLBACK_VERIFY_MODEL"] = ",".join(m.fallback_checker_models)
    return out


def artifact_name(pipeline_id: str, run_date: date | str) -> str:
    return f"cards-{pipeline_id}-{run_date}" if pipeline_id else f"cards-{run_date}"


# ------------------------------------------------------------ the schedule

@dataclass(frozen=True)
class Due:
    pipeline: str
    scheduled_for: datetime     # UTC
    attempt: int

    def as_json(self) -> dict:
        return {"pipeline": self.pipeline, "scheduled_for": _iso(self.scheduled_for),
                "attempt": self.attempt}


def _iso(moment: datetime | date | None) -> str | None:
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return moment.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def last_fire(schedule: Schedule, now: datetime) -> datetime:
    """The latest time the cron fired at or before `now`, in UTC. Read in the
    pipeline's time zone, so 05:17 stays 05:17 across daylight saving."""
    local = now.astimezone(ZoneInfo(schedule.timezone))
    # A reverse walk excludes its starting point, and a fire at `now` counts.
    start = local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return next(CronSim(schedule.cron, start, reverse=True)).astimezone(timezone.utc)


def next_fire(schedule: Schedule, now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(schedule.timezone))
    return next(CronSim(schedule.cron, local)).astimezone(timezone.utc)


def due(p: Pipeline, state: dict | None, now: datetime) -> Due | None:
    """Whether the tick at `now` should run this pipeline, and for which time.

    `state` is what the last scheduled run recorded (see `record`). Times the
    tick missed collapse into one run: the curriculum is a queue, so a missed
    day is written late, never skipped.
    """
    if not p.spec.enabled:
        return None
    fire = last_fire(p.spec.schedule, now)
    last = _parse((state or {}).get("scheduled_for"))
    if last is None:
        return Due(p.id, fire, 1) if now - fire <= FIRST_RUN_WINDOW else None
    if fire > last:
        return Due(p.id, fire, 1)
    if fire == last and state.get("conclusion") != "success":
        attempt = int(state.get("attempt") or 1)
        if attempt < MAX_ATTEMPTS:
            return Due(p.id, fire, attempt + 1)
    return None


def read_state(status_dir: Path | None, pipeline_id: str) -> dict | None:
    if not status_dir:
        return None
    path = Path(status_dir) / pipeline_id / "scheduler.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def due_all(status_dir: Path | None, now: datetime, cfg=None) -> list[Due]:
    """Every pipeline due now. One that cannot be read is reported and left
    out, so a bad pipeline.yaml stops only itself, not the others."""
    found = []
    for path in sorted(root(cfg).glob(f"*/{SPEC_FILE}")):
        try:
            p = load(path.parent.name, cfg)
        except (ValidationError, FileNotFoundError, yaml.YAMLError) as e:
            print(f"skipped {path.parent.name}: {e}", file=sys.stderr)
            continue
        if (d := due(p, read_state(status_dir, p.id), now)):
            found.append(d)
    return found


# ------------------------------------------------------------ checking them

def problems(cfg=None) -> list[str]:
    """What would make a pipeline fail or get in another's way. Needs no
    collection, so CI checks every commit, the app's included."""
    found: list[str] = []
    loaded: list[tuple[Pipeline, Profile]] = []
    for path in sorted(root(cfg).glob(f"*/{SPEC_FILE}")):
        pid = path.parent.name
        if not ID.match(pid):
            found.append(f"{pid}: an id is lowercase letters, digits and dashes")
            continue
        try:
            p = load(pid, cfg)
            profile = p.profile()
        except (ValidationError, FileNotFoundError, yaml.YAMLError) as e:
            found.append(f"{pid}: {e}")
            continue
        found += [f"{pid}: {problem}" for problem in profile.schedule_problems()]
        if p.spec.enabled and not profile.decks:
            found.append(f"{pid}: enabled, but its profile has no decks yet")
        loaded.append((p, profile))

    for i, (a, profile_a) in enumerate(loaded):
        for b, profile_b in loaded[i + 1:]:
            for x in profile_a.decks:
                for y in profile_b.decks:
                    if _overlap(x.deck, y.deck):
                        found.append(f"{a.id} and {b.id} both write to {x.deck!r} / {y.deck!r}: "
                                     "each pipeline needs decks of its own, or each would "
                                     "move the other's curriculum along")
    return found


def _overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "::") or b.startswith(a + "::")


# ------------------------------------------------------------ what happened

def curriculum(profile: Profile, next_day: date | None) -> dict:
    """Every topic and the curriculum day it falls on, so the app can show
    what is done and what is next without knowing how days are planned."""
    decks = []
    for target in profile.decks:
        dated: list[tuple[str, date | None]] = []
        if target.start and target.last_day:
            day = target.start
            while day <= target.last_day:
                dated += [(topic, day) for topic in target.topics_on(day)]
                day += timedelta(days=1)
        else:
            dated = [(topic, None) for topic in target.topics]
        topics = []
        for topic, day in dated:
            kind, level = target.kind_of(topic)
            if day is None:
                status = "rotating"
            elif next_day and day < next_day:
                status = "done"
            elif day == next_day:
                status = "next"
            else:
                status = "upcoming"
            topics.append({"topic": topic, "kind": kind, "level": level,
                           "day": _iso(day), "status": status})
        decks.append({"deck": target.deck, "start": _iso(target.start),
                      "last_day": _iso(target.last_day), "daily_quota": target.daily_quota,
                      "topics": topics})
    return {"next_day": _iso(next_day), "decks": decks}


def summarize(report: dict | None) -> dict:
    """The parts of a run's report the app shows on its list of runs."""
    if not report:
        return {"cards": None, "guide": None, "llm_calls": 0, "tokens": None, "stages": [],
                "error": None}
    details = report.get("stage_details", {})
    outcomes = report.get("outcomes", {})
    guide = details.get("guide")
    error = None
    for stage in report.get("stages", []):
        if stage.get("status") == "failed":
            error = f"{stage['stage']}: {(details.get(stage['stage']) or {}).get('error', 'failed')}"
            break
    return {
        "cards": {"kept": outcomes.get("kept", 0),
                  "dropped": sum(n for k, n in outcomes.items() if k.startswith("dropped_")),
                  "by_deck": report.get("by_deck", [])},
        "guide": None if not guide else {
            k: guide.get(k) for k in ("chapters", "sections", "disputed", "unchecked", "missing")
        } | {"pdf": bool(guide.get("pdf"))},
        "llm_calls": sum(d.get("llm_calls", 0) for d in details.values() if isinstance(d, dict)),
        "tokens": report.get("tokens"),
        "stages": [{"stage": s["stage"], "status": s["status"], "seconds": s.get("seconds")}
                   for s in report.get("stages", [])],
        "error": error,
    }


def record(pipeline_id: str, status_dir: Path, run: dict, report: dict | None = None,
           next_day: date | None = None, cfg=None, cards_file: Path | None = None) -> dict:
    """Write what a run did into a checkout of the status branch.

    `run` is what the workflow knows: run_id, run_url, trigger, scheduled_for,
    attempt, conclusion, started_at, finished_at, curriculum_date, artifact.
    `cards_file` is the run's cards.json, kept as cards/<day>.json so the app
    can show the cards without unzipping an artifact.
    Written even for a pipeline whose files no longer load: an unrecorded
    scheduled run would be tried again every hour.
    """
    try:
        p: Pipeline | None = load(pipeline_id, cfg)
    except (ValidationError, FileNotFoundError, yaml.YAMLError):
        p = None
    folder = Path(status_dir) / pipeline_id
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    entry = {"pipeline": pipeline_id, "name": p.spec.name if p else pipeline_id,
             **run, **summarize(report)}
    if run.get("conclusion") != "success" and not entry["error"]:
        entry["error"] = "the run stopped before the pipeline reported anything; see its log"

    def write(name: str, data: dict) -> None:
        (folder / name).write_text(json.dumps(data, indent=2, default=str) + "\n",
                                   encoding="utf-8")

    write("latest.json", entry)
    write(f"runs/{run.get('curriculum_date') or 'none'}-{run.get('run_id') or 'local'}.json", entry)
    if run.get("trigger") == "schedule":
        write("scheduler.json", {k: run.get(k) for k in
                                 ("scheduled_for", "attempt", "conclusion", "run_id")})
    if cards_file and Path(cards_file).exists() and run.get("curriculum_date"):
        (folder / "cards").mkdir(exist_ok=True)
        shutil.copyfile(cards_file, folder / "cards" / f"{run['curriculum_date']}.json")
    try:
        if p:
            write("curriculum.json", curriculum(p.profile(), next_day))
    except (ValidationError, FileNotFoundError, yaml.YAMLError):
        pass                        # a broken profile is the run's error, already recorded
    return entry


# ------------------------------------------------------------ command line

def _now(value: str | None) -> datetime:
    return _parse(value) if value else datetime.now(timezone.utc)


def _record(args) -> int:
    """`record` reads the run's warehouse and collection, so it imports the
    pipeline itself; the other commands stay light."""
    if not ID.match(args.pipeline):
        print(f"{args.pipeline!r} is not a pipeline id", file=sys.stderr)
        return 2
    cfg = config.settings.model_copy(update={"ankigen_pipeline": args.pipeline})
    run_date = date.fromisoformat(args.date) if args.date else None
    report, next_day = None, None
    if (cfg.work_path / "warehouse.duckdb").exists():
        # Whatever goes wrong reading the run, the record is still written.
        try:
            from ankigen import pipeline as runner
            from ankigen.export import build_report

            ctx = runner.open_context(cfg=cfg)
            try:
                if run_date and ctx.wh.scalar(
                        "SELECT COUNT(*) FROM pipeline_runs WHERE run_date = ?", [run_date]):
                    report = build_report(ctx.wh, run_date)
                if Path(cfg.anki_collection_path).exists():
                    next_day = runner.next_day(ctx, date.today())
            finally:
                ctx.wh.close()
        except Exception as e:      # noqa: BLE001 - reported in the record itself
            print(f"could not read the run: {e}", file=sys.stderr)
    run = {
        "run_id": args.run_id, "run_url": args.run_url, "trigger": args.trigger,
        "scheduled_for": args.scheduled_for or None,
        "attempt": int(args.attempt) if args.attempt else 1,
        "conclusion": args.conclusion, "curriculum_date": args.date or None,
        "started_at": args.started_at or None, "finished_at": _iso(datetime.now(timezone.utc)),
        "artifact": artifact_name(args.pipeline, args.date) if args.date else None,
    }
    cards_file = cfg.work_path / "out" / args.date / "cards.json" if args.date else None
    entry = record(args.pipeline, Path(args.status_dir), run, report, next_day, cfg, cards_file)
    print(f"{args.pipeline}: {entry['conclusion']}, "
          f"{(entry['cards'] or {}).get('kept', 0)} card(s) kept, next day {next_day}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ankigen pipelines",
        description="The pipelines in pipelines/: list, check, schedule and record them.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Every pipeline, its schedule and next run.")
    sub.add_parser("validate", help="Check every pipeline; exit 1 on a problem.")
    p_due = sub.add_parser("due", help="The pipelines the tick should run now.")
    p_due.add_argument("--status-dir", help="A checkout of the ankigen-status branch.")
    p_due.add_argument("--now", help="ISO time instead of now, to test a schedule.")
    p_due.add_argument("--json", action="store_true", help="One JSON list, for a workflow.")
    p_env = sub.add_parser("env", help="A pipeline's settings as KEY=VALUE lines.")
    p_env.add_argument("pipeline")
    p_schema = sub.add_parser("schema", help="pipeline.yaml's JSON Schema.")
    p_schema.add_argument("--write", action="store_true", help="Write it to pipelines/schema.json.")
    p_rec = sub.add_parser("record", help="Write a run's outcome into the status branch.")
    p_rec.add_argument("pipeline")
    p_rec.add_argument("--status-dir", required=True)
    p_rec.add_argument("--date", help="The curriculum day the run wrote.")
    p_rec.add_argument("--conclusion", required=True)
    p_rec.add_argument("--trigger", choices=("schedule", "manual"), default="manual")
    p_rec.add_argument("--scheduled-for", default="")
    p_rec.add_argument("--attempt", default="")
    p_rec.add_argument("--run-id", default="")
    p_rec.add_argument("--run-url", default="")
    p_rec.add_argument("--started-at", default="")
    args = parser.parse_args(argv)

    if args.command == "list":
        now = datetime.now(timezone.utc)
        for p in all():
            s = p.spec.schedule
            when = next_fire(s, now).astimezone(ZoneInfo(s.timezone))
            flags = ", ".join(f for f, on in (("guide", p.spec.outputs.guide_pdf),
                                              ("push", p.spec.outputs.push_to_ankiweb)) if on)
            print(f"{p.id:<20} {'on ' if p.spec.enabled else 'off'}  {s.cron:<16} "
                  f"{s.timezone:<20} next {when:%Y-%m-%d %H:%M}  [{flags or 'apkg only'}]  "
                  f"{p.spec.name}")
        return 0
    if args.command == "validate":
        found = problems()
        for problem in found:
            print(f"  x {problem}")
        if found:
            return 1
        print(f"{len(all())} pipeline(s) OK.")
        return 0
    if args.command == "due":
        found = due_all(Path(args.status_dir) if args.status_dir else None, _now(args.now))
        if args.json:
            print(json.dumps([d.as_json() for d in found]))
        else:
            for d in found:
                print(f"{d.pipeline:<20} for {_iso(d.scheduled_for)}, attempt {d.attempt}")
            if not found:
                print("Nothing is due.")
        return 0
    if args.command == "env":
        for key, value in env(load(args.pipeline)).items():
            print(f"{key}={value}")
        return 0
    if args.command == "schema":
        text = schema()
        if args.write:
            (root() / "schema.json").write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    return _record(args)


if __name__ == "__main__":
    sys.exit(main())
