"""Several pipelines in one repository: their settings, their schedules, and
keeping each one's curriculum its own."""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from conftest import RUN_DATE
from pydantic import ValidationError

from ankigen import export, ingest, llm, pipeline, pipelines
from ankigen.config import Settings
from ankigen.pipelines import PipelineSpec

REPO = Path(__file__).parent.parent
SPEC = {"name": "German", "schedule": {"cron": "17 5 * * *", "timezone": "America/Sao_Paulo"}}
PROFILE = {"learner": {"level": "beginner"},
           "decks": [{"deck": "German::Verbs", "new_deck": True, "topics": ["sein"]}]}


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    """A pipelines/ folder of our own, and settings pointing at it."""
    root = tmp_path / "pipelines"

    def add(pid: str, spec: dict | None = None, profile: dict | None = None):
        folder = root / pid
        folder.mkdir(parents=True)
        (folder / "pipeline.yaml").write_text(yaml.safe_dump(spec or SPEC), encoding="utf-8")
        (folder / "profile.yaml").write_text(yaml.safe_dump(profile or PROFILE), encoding="utf-8")
        return folder

    add.cfg = Settings(_env_file=None, ankigen_pipelines_dir=str(root),
                       data_dir=str(tmp_path / "data"))
    return add


# ------------------------------------------------------------ the spec

def test_the_shipped_pipelines_are_valid_and_apart():
    assert pipelines.problems(Settings(_env_file=None,
                                       ankigen_pipelines_dir=str(REPO / "pipelines"))) == []


def test_the_schema_the_app_checks_against_is_current():
    """The app validates pipeline.yaml against this file before committing.
    `ankigen pipelines schema --write` regenerates it."""
    assert (REPO / "pipelines" / "schema.json").read_text(encoding="utf-8") == pipelines.schema()


def test_the_data_platform_pipeline_keeps_the_old_schedule():
    """05:17 in São Paulo is 08:17 UTC, the cron daily-cards.yml had."""
    p = pipelines.load("data-platform", Settings(_env_file=None,
                                                 ankigen_pipelines_dir=str(REPO / "pipelines")))
    assert pipelines.last_fire(p.spec.schedule, utc("2026-10-05T09:00:00")) == \
        utc("2026-10-05T08:17:00")


@pytest.mark.parametrize("change, message", [
    ({"schedule": {"cron": "61 5 * * *"}}, "not a cron"),
    ({"schedule": {"cron": "17 5 * *"}}, "five fields"),
    ({"schedule": {"cron": "17 5 * * *", "timezone": "Mars/Olympus"}}, "unknown time zone"),
    ({"models": {"writer": "gpt5"}}, "not a provider"),
    ({"models": {"writer": "gemini", "fallback": "gemini"}}, "another provider"),
    ({"models": {"writer": "groq", "fallback": "openrouter"}}, "LLM_MODEL"),
    ({"budget": {"max_llm_calls_per_run": -1}}, "greater than or equal"),
    ({"surprise": True}, "Extra inputs"),
])
def test_a_bad_spec_says_what_is_wrong(change, message):
    with pytest.raises(ValidationError, match=message):
        PipelineSpec.model_validate({**SPEC, **change})


def test_two_pipelines_on_one_deck_are_refused(repo):
    repo("german")
    repo("german-too", profile={"decks": [{"deck": "German", "new_deck": True,
                                           "topics": ["haben"]}]})
    [problem] = pipelines.problems(repo.cfg)
    assert "german and german-too" in problem and "'German::Verbs' / 'German'" in problem


def test_an_enabled_pipeline_needs_decks(repo):
    repo("empty", profile={"decks": []})
    repo("parked", spec={**SPEC, "enabled": False}, profile={"decks": []})
    assert pipelines.problems(repo.cfg) == ["empty: enabled, but its profile has no decks yet"]


def test_env_puts_each_models_chain_where_its_provider_reads_it(repo):
    repo("german", spec={**SPEC, "models": {
        "writer": "gemini", "writer_models": ["gemini-3.6-flash"],
        "checker": "groq", "checker_models": ["openai/gpt-oss-120b"],
        "fallback": "claude", "fallback_models": ["claude-sonnet-5"],
        "fallback_checker_models": []},
        "budget": {"max_llm_calls_per_run": 40},
        "outputs": {"push_to_ankiweb": False}})
    env = pipelines.env(pipelines.load("german", repo.cfg))
    assert env == {
        "ANKIGEN_PIPELINE": "german", "ANKIGEN_PUSH": "false",
        "LLM_PROVIDER": "gemini", "GEMINI_MODEL": "gemini-3.6-flash",
        "VERIFY_PROVIDER": "groq", "VERIFY_MODEL": "openai/gpt-oss-120b",
        "FALLBACK_PROVIDER": "claude", "CLAUDE_MODEL": "claude-sonnet-5",
        "MAX_LLM_CALLS": "40",
    }


def test_a_run_with_env_applied_resolves_to_the_pipelines_models(repo):
    repo("german", spec={**SPEC, "models": {"writer": "gemini", "writer_models": ["g-1"],
                                            "checker": "gemini", "checker_models": ["g-lite"],
                                            "fallback": ""}})
    env = {k.lower(): v for k, v in pipelines.env(pipelines.load("german", repo.cfg)).items()}
    s = Settings(_env_file=None, google_api_key="k", gemini_fallback_models="",
                 verify_fallback_models="", **env)
    assert s.resolve_llm()["models"] == ["g-1"]
    assert s.resolve_verify()["models"] == ["g-lite"]


def test_settings_give_each_pipeline_its_own_folder_and_profile(tmp_path):
    s = Settings(_env_file=None, data_dir=str(tmp_path), ankigen_pipeline="german",
                 ankigen_pipelines_dir=str(tmp_path / "p"))
    assert s.work_path == tmp_path / "pipelines" / "german"
    assert s.profile_path() == tmp_path / "p" / "german" / "profile.yaml"
    legacy = Settings(_env_file=None, data_dir=str(tmp_path))
    assert legacy.work_path == tmp_path                 # the layout from before pipelines
    assert legacy.profile_path().parts[-2:] == ("data-platform", "profile.yaml")
    assert Settings(_env_file=None, ankigen_profile="x.yaml").profile_path() == Path("x.yaml")


# ------------------------------------------------------------ the schedule

def load(repo, spec=None, pid="german"):
    repo(pid, spec=spec)
    return pipelines.load(pid, repo.cfg)


def state(fire: str, conclusion: str = "success", attempt: int = 1) -> dict:
    return {"scheduled_for": fire, "conclusion": conclusion, "attempt": attempt}


def test_a_new_pipeline_waits_for_its_time(repo):
    p = load(repo)
    # 08:17 UTC fired 13 minutes ago: run it.
    assert pipelines.due(p, None, utc("2026-10-05T08:30:00")).attempt == 1
    # It fired 23 hours ago, before the pipeline existed: wait for tomorrow's.
    assert pipelines.due(p, None, utc("2026-10-06T07:30:00")) is None


def test_a_time_that_ran_is_not_run_again(repo):
    p = load(repo)
    assert pipelines.due(p, state("2026-10-05T08:17:00Z"), utc("2026-10-05T09:23:00")) is None


def test_a_failed_time_is_retried_up_to_the_cap(repo):
    p = load(repo)
    now = utc("2026-10-05T09:23:00")
    again = pipelines.due(p, state("2026-10-05T08:17:00Z", "failure", 1), now)
    assert (again.scheduled_for, again.attempt) == (utc("2026-10-05T08:17:00"), 2)
    assert pipelines.due(p, state("2026-10-05T08:17:00Z", "cancelled", 2), now).attempt == 3
    assert pipelines.due(p, state("2026-10-05T08:17:00Z", "failure",
                                  pipelines.MAX_ATTEMPTS), now) is None


def test_missed_times_collapse_into_one_run(repo):
    """Three days without a tick: one run, for the latest time. The curriculum
    is a queue, so the days are written late rather than skipped."""
    p = load(repo)
    d = pipelines.due(p, state("2026-10-02T08:17:00Z"), utc("2026-10-05T10:23:00"))
    assert (d.scheduled_for, d.attempt) == (utc("2026-10-05T08:17:00"), 1)


def test_a_disabled_pipeline_is_never_due(repo):
    p = load(repo, spec={**SPEC, "enabled": False})
    assert pipelines.due(p, None, utc("2026-10-05T08:30:00")) is None


def test_the_cron_is_read_in_the_pipelines_time_zone(repo):
    """New York changes clocks on 2026-11-01: 06:00 local is 10:00 UTC before
    and 11:00 UTC after."""
    p = load(repo, spec={**SPEC, "schedule": {"cron": "0 6 * * *",
                                              "timezone": "America/New_York"}})
    assert pipelines.last_fire(p.spec.schedule, utc("2026-10-31T12:00:00")) == \
        utc("2026-10-31T10:00:00")
    assert pipelines.last_fire(p.spec.schedule, utc("2026-11-02T12:00:00")) == \
        utc("2026-11-02T11:00:00")


def test_a_broken_pipeline_does_not_stop_the_others(repo, capsys):
    repo("german")
    repo("broken", spec={"name": "x", "schedule": {"cron": "nope"}})
    due = pipelines.due_all(None, utc("2026-10-05T08:30:00"), repo.cfg)
    assert [d.pipeline for d in due] == ["german"]
    assert "skipped broken" in capsys.readouterr().err


def test_due_reads_the_status_branch(repo, tmp_path):
    repo("german")
    status = tmp_path / "status" / "german"
    status.mkdir(parents=True)
    (status / "scheduler.json").write_text(json.dumps(state("2026-10-05T08:17:00Z")))
    assert pipelines.due_all(tmp_path / "status", utc("2026-10-05T08:30:00"), repo.cfg) == []


# ------------------------------------------------------------ one curriculum each

def _tag(collection: Path, note_id: int, tags: str) -> None:
    con = sqlite3.connect(collection)
    con.execute("UPDATE notes SET tags = ? WHERE id = ?", (tags, note_id))
    con.commit()
    con.close()


def test_a_pipelines_queue_counts_only_its_own_decks(modern_collection, tmp_path):
    _tag(modern_collection, 1, "ankigen ankigen::run_2026-10-01")       # DS::SQL
    _tag(modern_collection, 4, "ankigen ankigen::run_2026-10-02")       # DS::SQL::Advanced
    _tag(modern_collection, 5, "ankigen ankigen::run_2026-10-09")       # Deutsch
    raw = tmp_path / "raw"
    sql = lambda deck: deck == "DS::SQL" or deck.startswith("DS::SQL::")  # noqa: E731
    assert ingest.run_dates(modern_collection, raw, sql) == {date(2026, 10, 1), date(2026, 10, 2)}
    assert ingest.next_run_date(modern_collection, raw, date(2026, 9, 1), sql) == date(2026, 10, 3)
    # Without a scope, the other pipeline's day decides, as it used to.
    assert ingest.next_run_date(modern_collection, raw, date(2026, 9, 1)) == date(2026, 10, 10)


@pytest.fixture
def ctx(cfg, profile, fake_llm, fake_embeddings):
    with open(cfg.ankigen_profile, "w", encoding="utf-8") as f:
        yaml.safe_dump(profile.model_dump(), f)
    c = pipeline.open_context(cfg=cfg)
    yield c
    c.wh.close()


def test_a_pipeline_that_does_not_push_counts_its_own_days(ctx):
    """Its cards never reach the collection, which therefore cannot say which
    days it wrote: without the warehouse, every run would redo one day."""
    ctx.spec = PipelineSpec.model_validate({**SPEC, "outputs": {"push_to_ankiweb": False}})
    pipeline.run(ctx, RUN_DATE)
    assert pipeline.next_day(ctx, date(2026, 1, 1)) == RUN_DATE + timedelta(days=1)
    # One that pushes trusts the collection, where nothing has arrived yet.
    ctx.spec = PipelineSpec.model_validate(SPEC)
    assert pipeline.next_day(ctx, date(2026, 1, 1)) == date(2026, 1, 1)


def test_a_pipeline_without_pdfs_skips_the_guide(ctx):
    ctx.spec = PipelineSpec.model_validate({**SPEC, "outputs": {"guide_pdf": False}})
    results = pipeline.run(ctx, RUN_DATE)
    assert "guide" not in results and "export" in results


# ------------------------------------------------------------ what a run leaves

def test_notes_say_which_pipeline_made_them():
    card = {"request_reason": "topic", "ad_hoc": False}
    assert "ankigen::pipeline::german" in export.tags_for(card, RUN_DATE, "german")
    assert not any("pipeline" in t for t in export.tags_for(card, RUN_DATE))


def test_export_writes_the_cards_for_the_app(ctx, cfg):
    pipeline.run(ctx, RUN_DATE)
    cards = json.loads((cfg.data_path / "out" / str(RUN_DATE) / "cards.json").read_text())
    kept = ctx.wh.scalar("SELECT COUNT(*) FROM card_outcomes WHERE run_date = ? "
                         "AND outcome = 'kept'", [RUN_DATE])
    assert cards["run_date"] == str(RUN_DATE) and len(cards["cards"]) == kept
    assert {"deck", "front", "back", "guide_ref", "picture"} <= set(cards["cards"][0])


def test_each_stage_reports_its_model_calls(ctx, monkeypatch):
    # The fake replaces call_json, so count through it as the real one does.
    real = llm.call_json
    monkeypatch.setattr(llm, "call_json", lambda *a, **k: (llm._spend(), real(*a, **k))[1])
    monkeypatch.setattr(llm, "_calls", 0)
    results = pipeline.run(ctx, RUN_DATE)
    assert results["generate"]["llm_calls"] > 0 and results["dedup"]["llm_calls"] == 0
    assert sum(r["llm_calls"] for r in results.values()) == llm.calls_made()


def test_a_spent_budget_is_a_quota_to_every_stage(monkeypatch):
    monkeypatch.setattr(llm.settings, "max_llm_calls", 2)
    monkeypatch.setattr(llm, "_calls", 0)
    monkeypatch.setattr(llm, "_call_chain", lambda prompt, retries, cfg: llm.LLMResult({}, "m"))
    llm.call_json("one")
    llm.call_json("two")
    with pytest.raises(llm.QuotaExhausted, match="budget of 2"):
        llm.call_json("three")
    assert issubclass(llm.BudgetExhausted, llm.QuotaExhausted)


def test_a_run_is_recorded_for_the_tick_and_the_app(ctx, repo, tmp_path):
    repo("german", profile={
        "decks": [{"deck": "German::Verbs", "new_deck": True, "start": "2026-10-01",
                   "daily_quota": 5, "topics": ["sein", "haben", "werden"]}]})
    pipeline.run(ctx, RUN_DATE)
    report = export.build_report(ctx.wh, RUN_DATE)
    run = {"run_id": "7", "trigger": "schedule", "scheduled_for": "2026-10-05T08:17:00Z",
           "attempt": 1, "conclusion": "success", "curriculum_date": "2026-10-02"}
    entry = pipelines.record("german", tmp_path / "status", run, report,
                             next_day=date(2026, 10, 3), cfg=repo.cfg,
                             cards_file=ctx.out_dir / str(RUN_DATE) / "cards.json")
    folder = tmp_path / "status" / "german"
    assert json.loads((folder / "latest.json").read_text()) == json.loads(
        json.dumps(entry, default=str))
    assert entry["cards"]["kept"] == report["outcomes"]["kept"] and entry["error"] is None
    assert entry["guide"]["chapters"] >= 1 and "llm_calls" in entry
    assert (folder / "runs" / "2026-10-02-7.json").exists()
    cards = json.loads((folder / "cards" / "2026-10-02.json").read_text(encoding="utf-8"))
    assert len(cards["cards"]) == entry["cards"]["kept"]
    assert json.loads((folder / "scheduler.json").read_text())["scheduled_for"] == \
        "2026-10-05T08:17:00Z"
    topics = json.loads((folder / "curriculum.json").read_text())["decks"][0]["topics"]
    assert [(t["topic"], t["status"]) for t in topics] == \
        [("sein", "done"), ("haben", "done"), ("werden", "next")]


def test_a_changed_plan_shows_before_the_next_run(repo, tmp_path):
    """The app's plan edits merge to master; the curriculum it shows follows
    at once, keeping the next day the last run recorded."""
    plan = {"decks": [{"deck": "German::Verbs", "new_deck": True, "start": "2026-10-01",
                       "daily_quota": 5, "topics": ["sein", "haben", "werden"]}]}
    repo("german", profile=plan)
    repo("french", spec={**SPEC, "name": "French"}, profile={
        "decks": [{"deck": "French::Verbs", "new_deck": True, "start": "2026-11-01",
                   "daily_quota": 5, "topics": ["être"]}]})
    status = tmp_path / "status"
    (status / "german").mkdir(parents=True)
    (status / "german" / "curriculum.json").write_text('{"next_day": "2026-10-02"}')

    assert pipelines.refresh_curricula(status, repo.cfg) == ["french", "german"]
    german = json.loads((status / "german" / "curriculum.json").read_text())
    assert german["next_day"] == "2026-10-02"
    assert [t["status"] for t in german["decks"][0]["topics"]] == ["done", "next", "upcoming"]
    french = json.loads((status / "french" / "curriculum.json").read_text(encoding="utf-8"))
    assert french["next_day"] == "2026-11-01"       # not run yet: its first day


def test_a_run_by_hand_does_not_count_as_the_schedule(repo, tmp_path):
    repo("german")
    pipelines.record("german", tmp_path, {"run_id": "8", "trigger": "manual",
                                          "conclusion": "failure"}, cfg=repo.cfg)
    entry = json.loads((tmp_path / "german" / "latest.json").read_text())
    assert entry["error"].startswith("the run stopped before")
    assert not (tmp_path / "german" / "scheduler.json").exists()


def test_a_run_of_a_pipeline_that_no_longer_loads_is_still_recorded(repo, tmp_path):
    """An unrecorded scheduled run is tried again every hour."""
    repo("broken", spec={"name": "x", "schedule": {"cron": "nope"}})
    pipelines.record("broken", tmp_path, {"trigger": "schedule", "conclusion": "failure",
                                          "scheduled_for": "2026-10-05T08:17:00Z"}, cfg=repo.cfg)
    assert (tmp_path / "broken" / "scheduler.json").exists()


def test_the_cli_runs_the_pipeline_commands(repo, monkeypatch, capsys):
    repo("german")
    monkeypatch.setattr("ankigen.config.settings", repo.cfg)
    assert pipelines.main(["validate"]) == 0
    assert pipelines.main(["due", "--json", "--now", "2026-10-05T08:30:00Z"]) == 0
    out = capsys.readouterr().out
    assert "1 pipeline(s) OK." in out and '"pipeline": "german"' in out
