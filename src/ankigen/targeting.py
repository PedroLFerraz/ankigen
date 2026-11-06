"""Decide what to generate today, and render the prompts that will ask for it.

Pure and deterministic: the same collection, profile and `run_date` always
produce the same requests. Rotation through topics and weak cards is seeded by
the date, so consecutive days cover different ground but a rerun of one day
reproduces it exactly. No LLM or embedding calls happen here, which keeps
`ankigen plan` instant.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from string import Template

from ankigen.ingest import Note
from ankigen.profile import CARDS_PER_TOPIC, DeckTarget, Profile

# v2.0 filed cards under a separate top-level inbox. They now go straight into
# the deck (or a subdeck of it), but decks exported by the old layout must still
# count as "already studied" for dedup.
LEGACY_INBOX = "AnkiGen Inbox"
AVOID_LIMIT = 40

_STOPWORDS = frozenset(
    "a an and are as at be by does do for from how in is it of on or that the this "
    "to was what when where which who why with you your".split()
)


@dataclass
class GenerationRequest:
    """One prompt's worth of cards: a deck, why it is being asked for, and
    the prompt itself once `render_prompt` has filled it in.

    `request_id` hashes what the request is (date, deck, reason, topic or
    focus card), so the same plan gives the same ids on a rerun.
    """
    request_id: str
    deck: str
    card_type: str
    n: int
    reason: str  # "topic" | "weak_card" | "gap"
    topic: str | None = None
    focus: str | None = None  # the weak card being re-approached
    style_examples: list[tuple[str, str]] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    prompt: str = ""
    # Free-text steer for a one-off run, from `ankigen run --prompt`.
    extra: str = ""
    # For a topic from a deck's `levels`: the shape of its cards and how much
    # the learner is assumed to know. None for a plain `topics` list.
    kind: str | None = None
    level: int | None = None
    # A build request asks for one snippet (n=1) with this many parts hidden,
    # each of which Anki shows as its own card.
    parts: int = 0
    # Asked for by hand for one deck: not a day of the curriculum.
    ad_hoc: bool = False

    @property
    def cards(self) -> int:
        """Anki cards this request should produce: gaps for a snippet, else notes."""
        return self.parts or self.n


def _shape(kind: str | None, n: int) -> dict:
    """A request's size: n notes, or for a build topic one note with n gaps."""
    return {"n": 1, "parts": n} if kind == "build" else {"n": n}


# The note type each kind of card is filed as.
KIND_NOTE = {"command": "command", "shortcut": "command", "concept": "basic",
             "build": "cloze", "scenario": "detailed"}
# What each level may assume. Level 1 is the one that matters most: without it
# a model told the learner is "a data scientist" writes an interview question
# about SIGTERM for someone who has never opened a terminal.
LEVELS = {
    1: "Level 1 of 4, basics: the learner has never used this. Everyday usage and "
       "vocabulary only; no production, security or failure scenarios.",
    2: "Level 2 of 4, how it works: the learner uses it day to day and now learns "
       "what happens underneath.",
    3: "Level 3 of 4, production: running it for real, where it fails, security "
       "and performance.",
    4: "Level 4 of 4, advanced: trade-offs and edge cases, the way an experienced "
       "engineer would be asked in an interview.",
}
# Rules for every kind; each kind adds its own shape in prompts/kind_*.txt.
KIND_RULES = [
    "One fact per card. Short beats complete: a card should be answerable in a few seconds.",
    "Use only words the learner has met at this level, and explain nothing the card "
    "does not ask.",
    "Be precise. If you are not certain something is correct, pick something else.",
    "Use `backticks` for commands, keys, file names and code.",
]


# ------------------------------------------------------------ helpers

def in_deck(note_deck: str, target: str) -> bool:
    """A target covers its subdecks, and cards filed by any earlier layout."""
    for root in (target, f"{LEGACY_INBOX}::{target}"):
        if note_deck == root or note_deck.startswith(root + "::"):
            return True
    return False


def _rng(run_date: date, *parts: str) -> random.Random:
    seed = hashlib.sha1("|".join([run_date.isoformat(), *parts]).encode()).hexdigest()
    return random.Random(int(seed[:16], 16))


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _relevance(query: str, text: str) -> float:
    q, t = _tokens(query), _tokens(text)
    return len(q & t) / len(q | t) if q and t else 0.0


def nearest(query: str, notes: list[Note], k: int) -> list[str]:
    """Existing questions most related to what is about to be generated.

    The model can't be shown a 400-card deck without blowing the provider's
    token budget, and the cards it's likeliest to duplicate are the related
    ones, so those are what it sees.
    """
    # Searching the whole collection rather than one deck means a single word
    # in common is no longer evidence of anything: "what makes a backfill
    # cheap or expensive" turned up a card about painters. Ask for two.
    q = _tokens(query)
    need = min(2, len(q))
    scored = [
        (_relevance(query, text), n)
        for n, text in ((n, f"{n.front} {n.back}") for n in notes)
        if len(q & _tokens(text)) >= need
    ]
    # Unrelated cards only spend tokens; the model can't duplicate what it's
    # not writing about.
    ranked = sorted((s for s in scored if s[0] > 0), key=lambda s: (-s[0], s[1].note_id))
    return [n.front for _, n in ranked[:k]]


def is_weak(note: Note, profile: Profile) -> bool:
    """A card the learner keeps failing: enough lapses, or an ease at or below
    the profile's limit. Ease 0 is a card never reviewed, not a weak one."""
    rule = profile.weak_cards
    return note.lapses >= rule.min_lapses or (0 < note.ease <= rule.max_ease)


def _style_examples(pool: list[Note], k: int, rng: random.Random) -> list[tuple[str, str]]:
    """Cards the learner has kept around long enough to be representative."""
    candidates = [n for n in pool if n.back and not n.is_cloze and 8 <= len(n.front) <= 250]
    mature = [n for n in candidates if n.interval_days >= 21]
    source = mature if len(mature) >= k else candidates
    picked = rng.sample(source, min(k, len(source)))
    return [(n.front, n.back) for n in sorted(picked, key=lambda n: n.note_id)]


# ------------------------------------------------------------ prompts

def _load(name: str) -> Template:
    return Template(resources.files("ankigen.prompts").joinpath(name).read_text(encoding="utf-8"))


def _bullets(items: list[str], empty: str) -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def render_prompt(req: GenerationRequest, profile: Profile, target: DeckTarget) -> str:
    """The full prompt for one request, from prompts/generate.txt.

    The task depends on why the request exists (a weak card, a gap, a topic),
    and the answer shape on the card's kind: a kind from the deck's `levels`
    brings its own contract and rules, anything else the note type's format.
    """
    learner, style = profile.learner, profile.style
    wants_images = profile.wants_images(target.deck)

    if req.reason == "weak_card":
        task = (
            f"The learner keeps forgetting this card:\n{req.focus}\n\n"
            f"Write exactly {req.n} new card(s) that teach the same fact from a different "
            "angle: a concrete example, a contrast with a concept it gets confused with, "
            "or the reason it is true. Do not simply rephrase the original."
        )
    elif req.reason == "gap":
        task = (
            f"Write exactly {req.n} new cards on important concepts this deck does not "
            "cover yet. Prefer concepts a practitioner uses often over trivia."
        )
    elif req.kind == "build":
        task = (f'Write exactly {req.n} snippet about: "{req.topic}", with exactly '
                f"{req.parts} parts hidden.")
        if req.level:
            task += f"\n{LEVELS[req.level]}"
    else:
        task = f'Write exactly {req.n} new cards about: "{req.topic}".'
        if req.level:
            task += f"\n{LEVELS[req.level]}"

    if getattr(req, "extra", ""):
        # What the person asked for on the command line, after the standing
        # instructions so it can override them.
        task += f"\n\nAlso, specifically for this request:\n{req.extra.strip()}"

    if req.kind:
        rules = [*KIND_RULES, *style.rules]
        contract = _load(f"kind_{req.kind}.txt").substitute(
            picture_fields=', "visual": null, "image_query": ""' if wants_images else "")
        if wants_images and req.kind in ("concept", "scenario"):
            contract += "\n" + "\n\n".join((_load("picture_hint.txt").template.rstrip(),
                                             _load("image_hint.txt").template.rstrip()))
    else:
        rules, contract = None, _format_contract(req.card_type, wants_images)

    rules = rules or [
        "Each card tests ONE specific concept.",
        f"Answers are at most {style.max_answer_words} words.",
        "Be factually precise. If you are not certain a detail is correct, choose a "
        "different concept rather than guess.",
        "Plain text. Use `backticks` for code, identifiers and commands.",
        *style.rules,
    ]

    examples = "\n".join(f"Q: {q}\nA: {a}\n" for q, a in req.style_examples)
    return _load("generate.txt").substitute(
        level=learner.level,
        language=learner.language,
        goals="; ".join(learner.goals) or "not specified",
        deck=req.deck,
        task=task,
        deck_instructions=f"\nDECK-SPECIFIC INSTRUCTIONS\n{target.instructions.strip()}\n"
        if target.instructions.strip() else "",
        examples=examples.strip() or "(no examples available — use a clear, concise style)",
        avoid=_bullets(req.avoid, "(nothing yet — this is a new subject)"),
        rules=_bullets(rules, ""),
        format=contract,
    )


def _format_contract(card_type: str, wants_images: bool) -> str:
    """The JSON shape the model must answer with.

    `image_query` goes *inside* the shape rather than being described after it:
    asked for in a trailing paragraph, the model answered with the three keys
    the shape listed and silently dropped the fourth, every time.
    """
    shape = _load(f"format_{card_type}.txt").substitute(
        picture_fields=', "visual": null, "image_query": ""' if wants_images else ""
    ).strip()
    if not wants_images:
        return shape
    hints = (_load("picture_hint.txt").template.rstrip(),
             _load("image_hint.txt").template.rstrip())
    return shape + "\n" + "\n\n".join(hints)


def ad_hoc_request(profile: Profile, notes: list[Note], run_date: date, deck: str,
                   topic: str = "", n: int = 0, extra: str = "",
                   kind: str = "") -> list[GenerationRequest]:
    """One request, asked for by hand, instead of today's plan.

    Everything the profile provides still applies — the learner, the style
    rules, examples drawn from that deck, the do-not-duplicate list — because
    what is being overridden is only *what to write about*, not how. A deck
    that is not in the profile still works; it just has no deck-specific
    instructions to add.
    """
    target = next((t for t in profile.decks if t.deck == deck), None)
    if target is None:
        target = DeckTarget(deck=deck, new_deck=True, topics=[topic] if topic else [])

    rng = _rng(run_date, deck)
    deck_notes = [n_ for n_ in notes if in_deck(n_.deck, deck)]
    style_pool = deck_notes or [n_ for n_ in notes
                                if any(in_deck(n_.deck, t.deck) for t in profile.decks)]
    query = topic or deck
    # A topic from the deck's curriculum keeps its kind and level; any other
    # topic is written the deck's plain way, unless a kind is asked for.
    planned, level = target.kind_of(" ".join(topic.split())) if topic else (None, None)
    kind = kind or planned
    req = GenerationRequest(
        request_id=hashlib.sha1(
            f"{run_date}|{deck}|ad_hoc|{topic}|{extra}|{kind or ''}".encode()
        ).hexdigest()[:12],
        deck=deck,
        card_type=KIND_NOTE[kind] if kind else target.card_type,
        **_shape(kind, n or min(target.daily_quota or 5, CARDS_PER_TOPIC)),
        reason="topic" if topic else "gap",
        topic=topic or deck.split("::")[-1],
        style_examples=_style_examples(style_pool, profile.style.examples_per_prompt, rng),
        avoid=nearest(query, notes, AVOID_LIMIT),
        extra=extra,
        kind=kind,
        level=level,
        ad_hoc=True,
    )
    req.prompt = render_prompt(req, profile, target)
    return [req]


# ------------------------------------------------------------ targeting

def build_requests(profile: Profile, notes: list[Note], run_date: date) -> list[GenerationRequest]:
    """The day's plan: for each deck in the profile's order, weak cards first,
    then the day's topics, else one request for what the deck is missing.

    Each deck takes at most its quota for the day, and all of them together
    at most the profile's daily budget; decks after the budget runs out get
    nothing that day.
    """
    requests: list[GenerationRequest] = []
    budget = profile.budget_on(run_date)
    all_targeted = [n for n in notes if any(in_deck(n.deck, t.deck) for t in profile.decks)]

    for target in profile.decks:
        quota = min(target.quota_on(run_date), budget)
        if quota <= 0:
            continue
        # A phased deck outside its window is not studied at all that day,
        # weak cards included: its turn is either still coming or over.
        todays = target.topics_on(run_date) if target.start else None
        if todays == []:
            continue
        rng = _rng(run_date, target.deck)
        deck_notes = [n for n in notes if in_deck(n.deck, target.deck)]
        # A brand-new subject has no cards of its own to imitate; borrow the
        # learner's voice from the rest of what they study.
        style_pool = deck_notes or all_targeted
        k = profile.style.examples_per_prompt
        deck_reqs: list[GenerationRequest] = []

        def add(reason: str, n: int, topic: str | None = None, focus: Note | None = None):
            query = focus.front if focus else (topic or target.deck)
            kind, level = target.kind_of(topic) if reason == "topic" else (None, None)
            deck_reqs.append(GenerationRequest(
                request_id=hashlib.sha1(
                    f"{run_date}|{target.deck}|{reason}|{topic}|{focus.note_id if focus else ''}".encode()
                ).hexdigest()[:12],
                deck=target.deck,
                card_type=KIND_NOTE[kind] if kind else target.card_type,
                **_shape(kind, n),
                reason=reason,
                kind=kind,
                level=level,
                topic=topic,
                focus=f"Q: {focus.front}\nA: {focus.back}" if focus else None,
                style_examples=_style_examples(style_pool, k, rng),
                # Drawn from the whole collection, not just this deck: dedup
                # would drop a card that repeats something filed elsewhere, so
                # asking for it at all wastes a request, and free tiers ration
                # those by the day.
                avoid=nearest(query, notes, AVOID_LIMIT),
            ))

        # 1. Weak cards first: fixing what the learner keeps failing beats new material.
        if profile.weak_cards.enabled and target.card_type != "cloze":
            weak = sorted(
                (n for n in deck_notes if is_weak(n, profile) and n.back),
                key=lambda n: (-n.lapses, n.ease or 9999, n.note_id),
            )
            # Rotate through the weakest dozen so it isn't the same card daily.
            pool = weak[:12]
            rng.shuffle(pool)
            for note in pool[: min(profile.weak_cards.max_per_deck, quota)]:
                add("weak_card", 1, focus=note)
            quota -= len(deck_reqs)

        # 2. Topics, rotated by date; a few cards each so a topic gets depth.
        if quota > 0 and target.topics:
            n_topics = min(len(target.topics), math.ceil(quota / CARDS_PER_TOPIC))
            if todays is not None:
                # In order from the first topic, each covered once. The last
                # day of an odd-length list has one topic, and gets its share.
                chosen = todays[:n_topics]
                quota = min(quota, len(chosen) * CARDS_PER_TOPIC)
            else:
                start = run_date.toordinal() % len(target.topics)
                chosen = [target.topics[(start + i) % len(target.topics)] for i in range(n_topics)]
            share, extra = divmod(quota, len(chosen))
            for i, topic in enumerate(chosen):
                if share + (i < extra):
                    add("topic", share + (i < extra), topic=topic)
        # 3. No topics given: let the model find what the deck is missing.
        elif quota > 0:
            add("gap", quota, topic=target.deck.split("::")[-1])

        for req in deck_reqs:
            req.prompt = render_prompt(req, profile, target)
        budget -= sum(r.cards for r in deck_reqs)
        requests.extend(deck_reqs)
        if budget <= 0:
            break

    return requests


REQUEST_COLUMNS = ("run_date", "request_id", "deck", "topic", "card_type", "n", "reason",
                   "focus", "prompt", "kind", "level", "ad_hoc")


def save_requests(wh, run_date: date, requests: list[GenerationRequest]) -> int:
    """Replace the day's requests partition with these."""
    return wh.replace_partition("requests", run_date, REQUEST_COLUMNS, [
        (run_date, r.request_id, r.deck, r.topic, r.card_type, r.n, r.reason, r.focus, r.prompt,
         r.kind, r.level, r.ad_hoc)
        for r in requests
    ])


def load_requests(wh, run_date: date) -> list[dict]:
    """The day's requests as rows, in a stable order for the generate stage."""
    return wh.query(
        "SELECT * FROM requests WHERE run_date = ? ORDER BY deck, request_id", [run_date]
    )
