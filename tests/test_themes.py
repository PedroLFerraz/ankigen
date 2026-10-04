"""Starting a new subject: the model plans the topics, the profile gains a deck."""
from datetime import date

import pytest
import yaml

from ankigen import llm, themes
from ankigen.profile import DeckTarget, load_profile

PROFILE = """# My profile. This comment has to survive.
learner:
  level: "working data scientist"
global_quota: 6
decks:
  # --- existing
  - deck: DS::SQL
    daily_quota: 3
    topics: [window functions, NULL semantics]
  - deck: Data Platform::Airflow
    new_deck: true
    daily_quota: 3
    topics: ["DAGs, tasks and operators: the vocabulary"]
"""

PLAN = {
    "topics": ["what Spark is for", "RDDs, DataFrames and Datasets", "lazy evaluation and the DAG",
               "narrow versus wide transformations: shuffles", "partitioning and skew",
               "caching and persistence", "joins: broadcast versus sort-merge",
               "what Spark is for"],                     # a repeat, to be dropped
    "instructions": "Prefer PySpark examples.",
    "image_context": "Apache Spark",
    "card_type": "detailed",
}


@pytest.fixture
def profile_file(tmp_path):
    path = tmp_path / "profile.yaml"
    path.write_text(PROFILE, encoding="utf-8")
    return path


@pytest.fixture
def planned(monkeypatch):
    prompts = []
    monkeypatch.setattr(llm, "call_json",
                        lambda prompt, **k: (prompts.append(prompt), llm.LLMResult(PLAN, "m"))[1])
    return prompts


def test_the_model_plans_the_topics_in_order(profile_file, planned):
    target = themes.plan(load_profile(profile_file), "Data Platform :: Spark",
                         about="Apache Spark for batch processing")
    assert target.deck == "Data Platform::Spark" and target.new_deck
    assert target.topics[0] == "what Spark is for" and len(target.topics) == 7   # deduplicated
    assert target.image_context == "Apache Spark" and target.card_type == "detailed"
    [prompt] = planned
    assert "Apache Spark for batch processing" in prompt
    assert "Data Platform::Airflow" in prompt          # it sees what already exists


def test_a_deck_already_in_the_profile_is_refused(profile_file, planned):
    with pytest.raises(ValueError, match="already in the profile"):
        themes.plan(load_profile(profile_file), "DS::SQL")
    assert planned == []                               # no request spent on it


def test_too_thin_a_plan_is_refused(profile_file, monkeypatch):
    monkeypatch.setattr(llm, "call_json",
                        lambda prompt, **k: llm.LLMResult({"topics": ["one", "two"]}, "m"))
    with pytest.raises(ValueError, match="at least"):
        themes.plan(load_profile(profile_file), "Data Platform::Spark")


def test_the_deck_is_appended_and_every_comment_survives(profile_file, planned):
    target = themes.plan(load_profile(profile_file), "Data Platform::Spark")
    proposal = themes.add_to_profile(profile_file, target, today=date(2026, 9, 24))
    text = profile_file.read_text(encoding="utf-8")
    assert "# My profile. This comment has to survive." in text
    assert "# --- existing" in text
    assert "added with `ankigen add-theme` on 2026-09-24" in text
    decks = load_profile(profile_file).decks
    assert [d.deck for d in decks][-1] == "Data Platform::Spark"
    assert decks[-1].topics == target.topics and decks[-1].image_context == "Apache Spark"
    # A topic with ": " in it is quoted, so it reads back as one string.
    assert decks[-1].topics[3] == "narrow versus wide transformations: shuffles"
    assert proposal.block in text


def test_the_daily_total_goes_up_when_the_decks_already_use_it_all(profile_file, planned):
    target = themes.plan(load_profile(profile_file), "Data Platform::Spark", quota=2)
    proposal = themes.add_to_profile(profile_file, target)
    assert proposal.global_quota == 8                  # 3 + 3 already, plus 2
    assert load_profile(profile_file).global_quota == 8


def test_the_daily_total_is_left_alone_when_there_is_room(tmp_path, planned):
    path = tmp_path / "p.yaml"
    path.write_text(PROFILE.replace("global_quota: 6", "global_quota: 20"), encoding="utf-8")
    proposal = themes.add_to_profile(path, themes.plan(load_profile(path), "Data Platform::Spark"))
    assert proposal.global_quota is None and load_profile(path).global_quota == 20


def test_a_profile_where_decks_is_not_last_is_left_untouched(tmp_path):
    path = tmp_path / "p.yaml"
    original = PROFILE + "inbox: \"\"\n"
    path.write_text(original, encoding="utf-8")
    target = DeckTarget(deck="X::Y", new_deck=True, topics=["a topic"])
    with pytest.raises(ValueError, match="last section"):
        themes.add_to_profile(path, target)
    assert path.read_text(encoding="utf-8") == original


def test_the_summary_lists_the_topics_and_the_quota_change(profile_file, planned):
    target = themes.plan(load_profile(profile_file), "Data Platform::Spark")
    proposal = themes.add_to_profile(profile_file, target)
    text = themes.summary(proposal, "Apache Spark for batch processing")
    assert "1. what Spark is for" in text and "global_quota" in text
    assert "> Apache Spark for batch processing" in text


PHASED = """global_quota: 10
decks:
  - deck: DP::One
    new_deck: true
    start: 2026-10-01
    daily_quota: 10
    topics: [t1, t2, t3, t4]
"""


def test_a_new_subject_joins_the_end_of_a_curriculum(tmp_path, planned):
    path = tmp_path / "p.yaml"
    path.write_text(PHASED, encoding="utf-8")
    target = themes.plan(load_profile(path), "DP::Two")
    assert target.daily_quota == 10                    # the curriculum's pace
    proposal = themes.add_to_profile(path, target, today=date(2026, 9, 24))
    added = load_profile(path).decks[-1]
    assert added.start == date(2026, 10, 3)            # the day after DP::One ends
    assert proposal.global_quota is None               # they never share a day
    assert "from **2026-10-03**" in themes.summary(proposal)


def test_a_curriculum_that_has_ended_restarts_tomorrow(tmp_path, planned):
    path = tmp_path / "p.yaml"
    path.write_text(PHASED, encoding="utf-8")
    target = themes.plan(load_profile(path), "DP::Two")
    themes.add_to_profile(path, target, today=date(2026, 12, 1))
    assert load_profile(path).decks[-1].start == date(2026, 12, 2)


def test_every_shipped_profile_can_take_a_new_deck(tmp_path, planned):
    """Each pipeline's profile must stay appendable: decks last, valid after.

    The deck is one no real profile has: this test once used Spark, and
    failed on the very pull request that proposed adding Spark.
    """
    from pathlib import Path
    reals = sorted((Path(__file__).parent.parent / "pipelines").glob("*/profile.yaml"))
    assert reals
    for real in reals:
        path = tmp_path / f"{real.parent.name}.yaml"
        path.write_text(real.read_text(encoding="utf-8"), encoding="utf-8")
        themes.add_to_profile(path, themes.plan(load_profile(path), "Zz Test::Appendability"))
        assert load_profile(path).decks[-1].deck == "Zz Test::Appendability"
        assert yaml.safe_load(path.read_text(encoding="utf-8"))["decks"][-1]["new_deck"] is True
