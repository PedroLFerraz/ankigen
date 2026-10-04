from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from conftest import RUN_DATE

from ankigen.ingest import read_notes
from ankigen.profile import Profile, load_profile
from ankigen.targeting import LEGACY_INBOX, build_requests, in_deck, nearest


# ------------------------------------------------------------------ profile

def test_profile_loads_and_normalises_deck(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("decks:\n  - deck: ' DS :: SQL '\n    topics: [joins]\n", encoding="utf-8")
    assert load_profile(p).decks[0].deck == "DS::SQL"


def test_unquoted_colon_in_yaml_is_a_clear_error(tmp_path):
    p = tmp_path / "p.yaml"
    p.write_text("learner:\n  goals:\n    - Interviews: SQL and stats\ndecks: []\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="goals"):
        load_profile(p)


def test_validate_against_collection(profile):
    assert profile.validate_against({"DS::SQL", "Deutsch"}) == []          # new_deck is allowed
    problems = profile.validate_against({"Deutsch"})
    assert any("DS::SQL" in p for p in problems)


def test_new_deck_without_topics_is_rejected():
    p = Profile.model_validate({"decks": [{"deck": "X", "new_deck": True}]})
    assert "needs `topics:`" in p.validate_against(set())[0]


def test_subdeck_counts_as_existing(profile):
    assert profile.validate_against({"DS::SQL::Advanced"}) == []


# ------------------------------------------------------------------ targeting

@pytest.fixture
def notes(modern_collection):
    return read_notes(modern_collection)


def test_in_deck_covers_subdecks_and_inbox():
    assert in_deck("DS::SQL", "DS::SQL")
    assert in_deck("DS::SQL::Advanced", "DS::SQL")
    assert in_deck(f"{LEGACY_INBOX}::DS::SQL", "DS::SQL")   # exported by v2.0
    assert not in_deck("DS::SQLite", "DS::SQL")


def test_same_date_same_requests(profile, notes):
    a = build_requests(profile, notes, RUN_DATE)
    b = build_requests(profile, notes, RUN_DATE)
    assert [(r.request_id, r.prompt) for r in a] == [(r.request_id, r.prompt) for r in b]


def test_topics_rotate_across_days(profile, notes):
    topics = {
        r.topic
        for d in range(3)
        for r in build_requests(profile, notes, RUN_DATE + timedelta(days=d))
        if r.deck == "DS::SQL" and r.reason == "topic"
    }
    assert topics == {"joins", "indexes", "NULLs"}


def test_quotas_respected(profile, notes):
    reqs = build_requests(profile, notes, RUN_DATE)
    assert sum(r.n for r in reqs if r.deck == "DS::SQL") == 4
    assert sum(r.n for r in reqs if r.deck == "Data Platform::Airflow") == 2
    profile.global_quota = 3
    assert sum(r.n for r in build_requests(profile, notes, RUN_DATE)) == 3


def test_weak_card_gets_its_own_request(profile, notes):
    weak = [r for r in build_requests(profile, notes, RUN_DATE) if r.reason == "weak_card"]
    assert len(weak) == 1
    assert "What is a CTE?" in weak[0].focus           # lapses=3, ease=1900
    assert "different angle" in weak[0].prompt


def test_new_deck_borrows_style_from_other_targeted_decks(profile, notes):
    req = next(r for r in build_requests(profile, notes, RUN_DATE) if r.deck == "Data Platform::Airflow")
    assert req.style_examples                        # nothing of its own to imitate
    assert "(nothing yet" in req.prompt


# ------------------------------------------------------------------ curriculum

TOPICS = ["t1", "t2", "t3", "t4", "t5"]


def _phased(**deck):
    return Profile.model_validate({
        "global_quota": 10,
        "decks": [{"deck": "DP::One", "new_deck": True, "daily_quota": 10,
                   "start": "2026-10-01", "topics": TOPICS, **deck}],
    })


def _topics(profile, notes, day):
    return [(r.topic, r.n) for r in build_requests(profile, notes, day)]


def test_a_phased_deck_waits_for_its_start(notes):
    assert _topics(_phased(), notes, date(2026, 9, 30)) == []


def test_a_phased_deck_starts_at_its_first_topic_and_advances(notes):
    p = _phased()
    assert _topics(p, notes, date(2026, 10, 1)) == [("t1", 5), ("t2", 5)]
    assert _topics(p, notes, date(2026, 10, 2)) == [("t3", 5), ("t4", 5)]


def test_the_last_day_of_an_odd_list_writes_one_topic(notes):
    assert _topics(_phased(), notes, date(2026, 10, 3)) == [("t5", 5)]


def test_a_phased_deck_stops_after_one_pass(notes):
    p = _phased()
    assert p.decks[0].last_day == date(2026, 10, 3)
    assert _topics(p, notes, date(2026, 10, 4)) == []


def test_a_small_quota_takes_one_topic_a_day(notes):
    p = _phased(daily_quota=3)
    assert _topics(p, notes, date(2026, 10, 2)) == [("t2", 3)]
    assert p.decks[0].last_day == date(2026, 10, 5)


def test_decks_one_after_another_fit_the_daily_total():
    p = Profile.model_validate({"global_quota": 10, "decks": [
        {"deck": "A", "new_deck": True, "daily_quota": 10, "start": "2026-10-01", "topics": TOPICS},
        {"deck": "B", "new_deck": True, "daily_quota": 10, "start": "2026-10-04", "topics": TOPICS},
    ]})
    assert p.validate_against(set()) == []
    assert p.next_free_day() == date(2026, 10, 7)


def test_overlapping_decks_past_the_daily_total_are_a_problem():
    p = Profile.model_validate({"global_quota": 10, "decks": [
        {"deck": "A", "new_deck": True, "daily_quota": 10, "start": "2026-10-01", "topics": TOPICS},
        {"deck": "B", "new_deck": True, "daily_quota": 10, "start": "2026-10-03", "topics": TOPICS},
    ]})
    [problem] = p.validate_against(set())
    assert "2026-10-03" in problem and "A and B" in problem


def test_the_shipped_curriculum_runs_back_to_back():
    p = load_profile("pipelines/data-platform/profile.yaml")
    phased = p.phased()
    assert len(phased) == 15 and p.schedule_problems() == []
    for a, b in zip(phased, phased[1:]):
        assert b.start == a.last_day + timedelta(days=1), (a.deck, b.deck)
    # Every subject starts from the basics, and the first opens with a kickoff.
    assert all(t.kind_of(t.topics[0])[1] == 1 for t in phased)
    assert phased[0].first_day_quota == 100


def test_gap_request_when_no_topics(notes):
    p = Profile.model_validate({"decks": [{"deck": "DS::SQL", "daily_quota": 2}],
                                "weak_cards": {"enabled": False}})
    [req] = build_requests(p, notes, RUN_DATE)
    assert req.reason == "gap" and req.n == 2


def test_prompt_is_personalised(profile, notes):
    req = next(r for r in build_requests(profile, notes, RUN_DATE)
               if r.deck == "DS::SQL" and r.reason == "topic")
    assert "Use backticks for SQL." in req.prompt              # deck instructions
    assert "Be concise." in req.prompt                          # style rules
    assert "interviews" in req.prompt                           # learner goals
    assert "Q: " in req.prompt                                  # few-shot examples
    assert '{"cards": [{"question"' in req.prompt               # format contract


def test_nearest_ignores_unrelated_cards(notes):
    sql = [n for n in notes if n.deck.startswith("DS::SQL")]
    assert nearest("window functions over partitions", sql, 10) == [
        "How do window functions differ from GROUP BY?"
    ]


# ------------------------------------------------------------------ ad hoc

def test_ad_hoc_request_overrides_the_plan_but_keeps_the_profile(profile, notes):
    from ankigen.targeting import ad_hoc_request

    [req] = ad_hoc_request(profile, notes, RUN_DATE, "DS::SQL",
                           topic="window frames", n=2,
                           extra="Use a concrete ORDER BY example.")
    assert (req.deck, req.n, req.topic) == ("DS::SQL", 2, "window frames")
    assert 'Write exactly 2 new cards about: "window frames".' in req.prompt
    assert "Use a concrete ORDER BY example." in req.prompt
    assert "Use backticks for SQL." in req.prompt          # deck instructions still apply
    assert "Be concise." in req.prompt                      # and the style rules
    assert "Q: " in req.prompt                              # and examples from your cards


def test_ad_hoc_works_for_a_deck_the_profile_never_heard_of(profile, notes):
    from ankigen.targeting import ad_hoc_request

    [req] = ad_hoc_request(profile, notes, RUN_DATE, "Data Platform::Terraform")
    assert req.deck == "Data Platform::Terraform"
    assert req.reason == "gap"                              # no topic given
    assert req.style_examples                               # borrows your voice from elsewhere


def test_ad_hoc_is_reproducible(profile, notes):
    from ankigen.targeting import ad_hoc_request

    a = ad_hoc_request(profile, notes, RUN_DATE, "DS::SQL", topic="joins")
    b = ad_hoc_request(profile, notes, RUN_DATE, "DS::SQL", topic="joins")
    assert a[0].request_id == b[0].request_id and a[0].prompt == b[0].prompt


def test_ad_hoc_can_ask_for_a_kind(profile, notes):
    """A topic outside the curriculum is written as plain cards unless a kind
    is asked for; with one it gets that shape, e.g. commands learned both ways."""
    from ankigen.targeting import ad_hoc_request

    [plain] = ad_hoc_request(profile, notes, RUN_DATE, "DS::SQL", topic="psql basics")
    [cmd] = ad_hoc_request(profile, notes, RUN_DATE, "DS::SQL", topic="psql basics",
                           n=3, kind="command")
    assert plain.kind is None and plain.card_type == "basic"
    assert cmd.kind == "command" and cmd.card_type == "command" and cmd.n == 3
    assert '"parts"' in cmd.prompt
    assert cmd.request_id != plain.request_id
