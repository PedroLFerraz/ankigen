"""Shared fixtures: real SQLite collections in both Anki schemas, a temp
warehouse, and deterministic stand-ins for the LLM and embedding provider."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from ankigen import llm
from ankigen.config import Settings
from ankigen.profile import Profile
from ankigen.warehouse import Warehouse

RUN_DATE = date(2026, 9, 22)

# (note_id, deck, fields, lapses, factor, ivl)
SAMPLE_NOTES = [
    (1, "DS::SQL", ["What does <b>COALESCE</b> return?", "The first non-NULL argument."], 0, 2500, 40),
    (2, "DS::SQL", ["What is a CTE?", "A named subquery defined with WITH."], 3, 1900, 3),
    (3, "DS::SQL", ["How do window functions differ from GROUP BY?", "They keep every row."], 0, 2500, 60),
    (4, "DS::SQL::Advanced", ["What is a LATERAL join?", "A join whose right side can reference the left."], 0, 2500, 30),
    (5, "Deutsch", ["der Hund", '<img src="dog.jpg">', "[sound:hund.mp3]", "the dog"], 0, 2500, 90),
    (6, "Biology", ["The {{c1::mitochondria}} makes ATP.", ""], 0, 2500, 10),
]


def _modern(path: Path, notes=SAMPLE_NOTES, wal: bool = False) -> Path:
    con = sqlite3.connect(path)
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE col (id INTEGER PRIMARY KEY, ver INTEGER);
        CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE notetypes (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, mid INTEGER, flds TEXT, tags TEXT, mod INTEGER);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, odid INTEGER,
                            ord INTEGER, lapses INTEGER, factor INTEGER, reps INTEGER,
                            ivl INTEGER, queue INTEGER);
        CREATE TABLE revlog (id INTEGER PRIMARY KEY, cid INTEGER, ease INTEGER, ivl INTEGER,
                             time INTEGER, type INTEGER);
        INSERT INTO col VALUES (1, 18);
        INSERT INTO notetypes VALUES (10, 'Basic'), (11, 'Basico');
        INSERT INTO fields VALUES (10, 0, 'Front'), (10, 1, 'Back'),
                                  (11, 0, 'Frente'), (11, 1, 'Imagem'), (11, 2, 'Audio'), (11, 3, 'Verso');
    """)
    deck_ids = {}
    for nid, deck, flds, lapses, factor, ivl in notes:
        # Modern Anki separates deck levels with \x1f.
        did = deck_ids.setdefault(deck, 100 + len(deck_ids))
        con.execute("INSERT OR IGNORE INTO decks VALUES (?, ?)", (did, deck.replace("::", "\x1f")))
        mid = 11 if len(flds) == 4 else 10
        con.execute("INSERT INTO notes VALUES (?, ?, ?, '', 0)", (nid, mid, "\x1f".join(flds)))
        con.execute("INSERT INTO cards VALUES (?, ?, ?, 0, 0, ?, ?, 5, ?, 2)",
                    (nid * 10, nid, did, lapses, factor, ivl))
        con.execute("INSERT INTO revlog VALUES (?, ?, 3, ?, 5000, 1)", (nid * 1000, nid * 10, ivl))
    con.commit()
    con.close()
    return path


def _legacy(path: Path) -> Path:
    con = sqlite3.connect(path)
    decks = {"1": {"name": "Default"}, "200": {"name": "DS::SQL"}}
    models = {"10": {"name": "Basic", "flds": [{"name": "Front"}, {"name": "Back"}]}}
    con.executescript("""
        CREATE TABLE col (id INTEGER PRIMARY KEY, ver INTEGER, decks TEXT, models TEXT);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, mid INTEGER, flds TEXT, tags TEXT, mod INTEGER);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, odid INTEGER,
                            ord INTEGER, lapses INTEGER, factor INTEGER, reps INTEGER,
                            ivl INTEGER, queue INTEGER);
        CREATE TABLE revlog (id INTEGER PRIMARY KEY, cid INTEGER, ease INTEGER, ivl INTEGER,
                             time INTEGER, type INTEGER);
    """)
    con.execute("INSERT INTO col VALUES (1, 11, ?, ?)", (json.dumps(decks), json.dumps(models)))
    con.execute("INSERT INTO notes VALUES (1, 10, ?, 'tag1', 0)", ("What is an index?\x1fA lookup structure.",))
    con.execute("INSERT INTO cards VALUES (10, 1, 200, 0, 0, 1, 2300, 4, 12, 2)")
    con.commit()
    con.close()
    return path


@pytest.fixture(autouse=True)
def _provider_independent_of_your_env(monkeypatch):
    """Pin the provider so tests never read the developer's .env.

    They did, and it bit: switching .env to Gemini sent the retry tests down
    the native-SDK branch, past their mocked OpenAI client, and into live
    calls against Google's API — which then failed on a retired model name.
    """
    from ankigen.config import settings

    monkeypatch.setattr(settings, "llm_provider", "openrouter", raising=False)
    monkeypatch.setattr(settings, "llm_api_key", "test-key-not-real", raising=False)
    monkeypatch.setattr(settings, "google_api_key", "", raising=False)
    monkeypatch.setattr(settings, "fallback_provider", "", raising=False)
    monkeypatch.setattr("ankigen.llm._down", {})


@pytest.fixture
def modern_collection(tmp_path) -> Path:
    return _modern(tmp_path / "collection.anki2")


@pytest.fixture
def legacy_collection(tmp_path) -> Path:
    return _legacy(tmp_path / "legacy.anki2")


@pytest.fixture
def make_collection(tmp_path):
    def factory(notes=SAMPLE_NOTES, wal=False, name="col.anki2"):
        return _modern(tmp_path / name, notes, wal)
    return factory


@pytest.fixture
def wh(tmp_path):
    w = Warehouse(tmp_path / "wh.duckdb")
    yield w
    w.close()


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate({
        "learner": {"level": "intermediate", "goals": ["interviews"]},
        "style": {"examples_per_prompt": 2, "rules": ["Be concise."]},
        "weak_cards": {"max_per_deck": 1},
        "global_quota": 10,
        "decks": [
            {"deck": "DS::SQL", "daily_quota": 4, "topics": ["joins", "indexes", "NULLs"],
             "instructions": "Use backticks for SQL."},
            {"deck": "Data Platform::Airflow", "new_deck": True, "daily_quota": 2,
             "topics": ["idempotency"]},
        ],
    })


@pytest.fixture
def cfg(tmp_path, modern_collection, monkeypatch) -> Settings:
    """Settings pointing everything at tmp_path; also swapped into the modules."""
    s = Settings(
        _env_file=None,
        llm_provider="ollama",
        anki_collection_path=str(modern_collection),
        data_dir=str(tmp_path / "data"),
        ankigen_profile=str(tmp_path / "profile.yaml"),
    )
    for mod in ("ankigen.config", "ankigen.llm", "ankigen.dedup", "ankigen.pipeline"):
        monkeypatch.setattr(f"{mod}.settings", s, raising=False)
    return s


# ---------------------------------------------------------------- fakes

class FakeLLM:
    """Answers generate prompts with deterministic cards, verify prompts with verdicts.

    `bad_words` marks any card containing them as factually incorrect, so tests
    can steer verification outcomes. `distinct` words each card apart from the
    others, so dedup drops nothing unless a test arranges it.
    """

    def __init__(self, bad_words=(), fail_verify=False, distinct=False):
        self.calls: list[str] = []
        self.bad_words = bad_words
        self.fail_verify = fail_verify
        self.distinct = distinct
        self.fail_guide: Exception | None = None
        self.fail_guide_check = False

    def __call__(self, prompt: str, max_retries: int = 5, cfg: dict | None = None) -> llm.LLMResult:
        self.calls.append(prompt)
        if "reviewing one chapter of a study guide" in prompt:
            if self.fail_guide_check:
                raise RuntimeError("429 rate limit")
            sections = re.findall(r"^(\d+)\. Card: (.*)$", prompt, flags=re.M)
            return llm.LLMResult({"primer_ok": True, "primer_issue": "", "results": [
                {"index": int(i), "correct": not any(w in s for w in self.bad_words),
                 "issue": "" if not any(w in s for w in self.bad_words) else "wrong flag"}
                for i, s in sections
            ]}, "fake-checker")
        if "writing one chapter of a study guide" in prompt:
            if self.fail_guide:
                raise self.fail_guide
            cards = re.findall(r"^(\d+)\. Q: (.*)$", prompt, flags=re.M)
            return llm.LLMResult({"primer": "How it works, with `code`.", "cards": [
                {"index": int(i), "why": f"Because of {q}.", "example": "```\nls -l\n```",
                 "mistakes": "Forgetting the flag.", "related": "- `ls -a`"}
                for i, q in cards
            ]}, "fake", prompt_tokens=200, completion_tokens=400)
        if "fact-checker" in prompt:
            if self.fail_verify:
                raise RuntimeError("429 rate limit")
            cards = re.findall(r"^(\d+)\. Q: (.*)$", prompt, flags=re.M)
            return llm.LLMResult({"results": [
                {"index": int(i), "correct": not any(w in q for w in self.bad_words),
                 "answerable": True, "score": 0.9, "issue": "wrong fact"}
                for i, q in cards
            ]}, "fake")

        n = int(re.search(r"Write exactly (\d+)", prompt).group(1))
        deck = re.search(r"^DECK: (.*)$", prompt, flags=re.M).group(1)
        topic = re.search(r'about: "(.*)"', prompt)
        subject = topic.group(1) if topic else "gap"
        tag = hashlib.md5(prompt.encode()).hexdigest()[:6]
        if "{{c1::term}}" in prompt:
            cards = [{"text": f"In {deck}, the {{{{c1::fact{i}}}}} of {subject} ({tag}).", "extra": ""}
                     for i in range(n)]
        elif self.distinct:
            words = [hashlib.md5(f"{tag}{i}".encode()).hexdigest()[:24] for i in range(n)]
            cards = [{"question": f"Question {i} {w}?", "answer": f"Answer {w}."}
                     for i, w in enumerate(words)]
        else:
            cards = [{"question": f"Question {i} about {subject} in {deck} ({tag})?",
                      "answer": f"Answer {i} about {subject}."} for i in range(n)]
        return llm.LLMResult({"cards": cards}, "fake", prompt_tokens=100, completion_tokens=50)


@pytest.fixture
def fake_llm(monkeypatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr("ankigen.llm.call_json", fake)
    return fake


def fake_vector(text: str, dims: int = 32) -> list[float]:
    """Bag-of-words vector: texts sharing words point in similar directions."""
    v = np.zeros(dims, dtype=np.float32)
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dims] += 1.0
    return v.tolist()


@pytest.fixture
def fake_embeddings(monkeypatch):
    calls = {"texts": 0}

    def embed(self, texts):
        calls["texts"] += len(texts)
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr("ankigen.dedup.Embedder._embed_batch", embed)
    return calls
