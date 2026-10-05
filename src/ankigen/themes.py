"""Add a subject to the profile: a new deck, with a curriculum the model plans.

Writing a deck's topic list by hand is the slow part of starting a subject,
and the part that decides what the deck teaches, in what order, for weeks.
So the model drafts it — ordered the way the subject is taught, in the same
shape as the decks already in the profile — and it goes into the profile as
text appended under `decks:`, so every comment in the file survives. From CI
it arrives as a pull request: nothing joins the daily run until it is merged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from importlib import resources
from pathlib import Path
from string import Template

import yaml

from ankigen import llm
from ankigen.profile import DeckTarget, Profile

MIN_TOPICS = 6
MAX_TOPICS = 24
CARD_TYPES = ("basic", "cloze", "detailed")
DEFAULT_QUOTA = 3


@dataclass
class Proposal:
    target: DeckTarget
    block: str                      # the YAML appended to the profile
    global_quota: int | None = None  # the new global quota, when it had to go up


def plan(profile: Profile, deck: str, about: str = "", topics: int = 16, quota: int | None = None,
         card_type: str | None = None) -> DeckTarget:
    """Ask the model for the new deck's curriculum. One request.

    Without a quota, a deck joining a curriculum keeps the curriculum's pace.
    """
    if quota is None:
        phased = profile.phased()
        quota = phased[-1].daily_quota if phased else DEFAULT_QUOTA
    deck = "::".join(part.strip() for part in deck.split("::"))
    if any(t.deck == deck for t in profile.decks):
        raise ValueError(f"{deck!r} is already in the profile.")
    existing = "\n".join(
        f"- {t.deck}: " + "; ".join(t.topics[:4]) + (" ..." if len(t.topics) > 4 else "")
        for t in profile.decks
    ) or "(none yet)"
    prompt = Template(
        resources.files("ankigen.prompts").joinpath("theme.txt").read_text(encoding="utf-8")
    ).substitute(
        level=profile.learner.level,
        goals="; ".join(profile.learner.goals) or "not specified",
        deck=deck,
        about=f"What it should cover: {about.strip()}\n" if about.strip() else "",
        existing=existing,
        n=max(MIN_TOPICS, min(MAX_TOPICS, topics)),
    )
    data = llm.call_json(prompt).data
    data = data if isinstance(data, dict) else {}
    planned = _topics(data.get("topics"))
    if len(planned) < MIN_TOPICS:
        raise ValueError(f"The model planned {len(planned)} topic(s) for {deck!r}; "
                         f"a subject needs at least {MIN_TOPICS}. Try again, or add --about.")
    suggested = str(data.get("card_type") or "").strip().lower()
    return DeckTarget(
        deck=deck,
        new_deck=True,
        daily_quota=quota,
        card_type=card_type or (suggested if suggested in CARD_TYPES else "detailed"),
        topics=planned[:MAX_TOPICS],
        instructions=str(data.get("instructions") or "").strip(),
        image_context=str(data.get("image_context") or "").strip(),
    )


def _topics(raw) -> list[str]:
    seen, out = set(), []
    for item in raw if isinstance(raw, list) else []:
        topic = " ".join(str(item).split()).strip(" -•.")
        if 3 <= len(topic) <= 120 and topic.lower() not in seen:
            seen.add(topic.lower())
            out.append(topic)
    return out


def render(target: DeckTarget, today: date) -> str:
    """The deck as the YAML block that goes under `decks:`."""
    entry = {"deck": target.deck, "new_deck": True, "daily_quota": target.daily_quota,
             "card_type": target.card_type}
    if target.image_context:
        entry["image_context"] = target.image_context
    if target.start:
        entry["start"] = target.start
    entry["topics"] = list(target.topics)
    if target.instructions:
        entry["instructions"] = target.instructions
    body = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, width=4096)
    lines = [f"  # --- added with `ankigen add-theme` on {today}; edit freely"]
    lines += [f"  {line}" if line else "" for line in body.rstrip("\n").split("\n")]
    return "\n".join(lines) + "\n"


def add_to_profile(path: Path, target: DeckTarget, today: date | None = None) -> Proposal:
    """Append the deck to the profile file, raising the daily total if the
    decks already use all of it. Validated before anything is written."""
    text = path.read_text(encoding="utf-8")
    profile = Profile.model_validate(yaml.safe_load(text) or {})
    if any(t.deck == target.deck for t in profile.decks):
        raise ValueError(f"{target.deck!r} is already in the profile.")
    keys = re.findall(r"(?m)^([A-Za-z_][\w]*):", text)
    if not keys or keys[-1] != "decks":
        # Appending would land under some other key; a person has to place it.
        raise ValueError("`decks:` is not the last section of the profile, so the new "
                         "deck cannot be appended safely. Move `decks:` to the end.")

    today = today or date.today()
    free = profile.next_free_day()
    if free and target.start is None:
        # A curriculum runs one subject after another; this one joins the end.
        target = target.model_copy(update={"start": max(free, today + timedelta(days=1))})
    block = render(target, today)
    new_text = text.rstrip("\r\n") + "\n\n" + block
    # A deck beyond the daily total gets nothing: the planner stops once the
    # budget is spent, and a new deck is last in line. A phased deck only
    # shares the day with the decks that never stop.
    needed = sum(t.daily_quota for t in profile.decks
                 if target.start is None or t.start is None) + target.daily_quota
    raised = None
    if needed > profile.global_quota:
        raised = needed
        new_text = re.sub(r"(?m)^global_quota:\s*\d+", f"global_quota: {needed}", new_text,
                          count=1)

    check = Profile.model_validate(yaml.safe_load(new_text) or {})
    added = next((t for t in check.decks if t.deck == target.deck), None)
    if added is None or added.topics != target.topics:
        raise ValueError("The new deck did not read back as written; the profile was not changed.")
    path.write_text(new_text, encoding="utf-8")
    return Proposal(target, block, raised)


def summary(proposal: Proposal, about: str = "",
            profile_path: str = "pipelines/data-platform/profile.yaml") -> str:
    """Markdown describing the change, for the pull request."""
    t = proposal.target
    if t.start:
        lines = [f"Adds **{t.deck}** to the curriculum: {t.daily_quota} {t.card_type} "
                 f"card(s) a day from **{t.start}** to **{t.last_day}**, "
                 f"{t.topics_per_day} topic(s) a day, in this order.", ""]
    else:
        lines = [f"Adds **{t.deck}** to the daily rotation: {t.daily_quota} {t.card_type} "
                 f"card(s) a day, one topic per day, in this order.", ""]
    if about:
        lines += [f"> {about}", ""]
    lines += [f"{i}. {topic}" for i, topic in enumerate(t.topics, start=1)]
    lines += [""]
    if t.instructions:
        lines += [f"**Writing instructions:** {t.instructions}", ""]
    if proposal.global_quota:
        lines += [f"`global_quota` goes up to **{proposal.global_quota}** so the new deck "
                  "gets cards; lower another deck's `daily_quota` instead if you would "
                  "rather keep the daily total.", ""]
    lines += [f"Edit `{profile_path}` on this branch to reorder, drop or add "
              "topics before merging. Nothing changes until this is merged."]
    return "\n".join(lines)
