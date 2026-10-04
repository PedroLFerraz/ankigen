"""Read an Anki collection into the warehouse.

The live collection is never opened for reading directly: it is snapshotted
first with SQLite's backup API, which (unlike a file copy) also captures pages
still sitting in the `-wal` file — i.e. your most recent reviews.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

FIELD_SEP = "\x1f"
# Modern Anki stores deck hierarchy with \x1f; the UI (and this project) uses "::".
DECK_SEP = "\x1f"
PARTS_TABLE = re.compile(r'<table class="ankigen-parts".*?</table>', re.DOTALL)
CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.DOTALL)


class CollectionLocked(RuntimeError):
    pass


@dataclass(frozen=True)
class Note:
    note_id: int
    deck: str
    notetype: str
    front: str
    back: str
    tags: str
    is_cloze: bool
    lapses: int
    ease: int
    reps: int
    interval_days: int
    queue: int
    modified_at: int

    @property
    def content_hash(self) -> str:
        return content_hash(self.front, self.back)


def content_hash(front: str, back: str) -> str:
    return hashlib.sha1(f"{front}{FIELD_SEP}{back}".encode("utf-8")).hexdigest()


def strip_html(text: str) -> str:
    """Plain text from an Anki field: no tags, media refs or entities."""
    text = re.sub(r"<img[^>]*>", " ", text)
    text = re.sub(r"\[sound:[^\]]*\]", " ", text)
    text = re.sub(r"<br\s*/?>|</(div|p|li)>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def strip_cloze(text: str) -> str:
    return CLOZE_RE.sub(r"\1", text)


# ---------------------------------------------------------------- snapshot

def _backup(collection: Path, dest: Path) -> None:
    src = sqlite3.connect(f"{collection.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _backup_within(collection: Path, dest: Path, timeout_s: float) -> bool:
    """Run the backup on a daemon thread and give up after `timeout_s`.

    A daemon thread matters: SQLite's backup blocks rather than failing when
    Anki holds the lock, and a normal thread would keep the interpreter alive
    at exit — a scheduled run would never finish.
    """
    outcome: list = []

    def work():
        try:
            _backup(collection, dest)
            outcome.append(None)
        except BaseException as e:        # reported, not raised, off-thread
            outcome.append(e)

    worker = threading.Thread(target=work, daemon=True, name="ankigen-backup")
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        return False                      # wedged on the lock; abandon it
    if outcome and isinstance(outcome[0], BaseException):
        logger.info("Backup failed (%s).", outcome[0])
        return False
    return bool(outcome)


def _copy_with_wal(collection: Path, dest: Path) -> None:
    """Copy the database plus its sidecars, then let SQLite recover the WAL.

    Slightly less safe than the backup API — Anki could write mid-copy — but it
    does not need a lock, so it still works while Anki is open.
    """
    shutil.copy2(collection, dest)
    for suffix in ("-wal", "-shm"):
        side = collection.with_name(collection.name + suffix)
        if side.exists():
            shutil.copy2(side, dest.with_name(dest.name + suffix))
    # Opening the copy replays the WAL into the main file.
    con = sqlite3.connect(dest)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()


def _flatten(dest: Path) -> None:
    """Turn the snapshot into a single self-contained file.

    A copy of a WAL database is still a WAL database, so every read of it
    recreates `-wal` and `-shm` beside it and the snapshot becomes three files
    that have to travel together — awkward when one of them gets committed to
    a repo. Switching the copy's journal mode leaves one file that stays one
    file. The live collection is untouched; this only ever runs on the copy.
    """
    con = sqlite3.connect(dest)
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.commit()
    finally:
        con.close()
    for suffix in ("-wal", "-shm"):
        with suppress(OSError):
            dest.with_name(dest.name + suffix).unlink(missing_ok=True)


def _sanity_check(dest: Path) -> int:
    con = sqlite3.connect(f"{dest.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return con.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    finally:
        con.close()


def snapshot(collection: str | Path, dest: str | Path, timeout_s: float = 10.0) -> Path:
    """Point-in-time copy of the collection, WAL included.

    Anki keeps the live collection locked while it is open, and SQLite's backup
    API *blocks* rather than failing when it cannot get a read lock — which once
    hung a whole run. So the backup runs with a deadline, and falls back to
    copying the files directly, which needs no lock.
    """
    collection, dest = Path(collection), Path(dest)
    if not collection.exists():
        raise FileNotFoundError(
            f"Anki collection not found: {collection}. Set ANKI_COLLECTION_PATH."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    for path in (dest, dest.with_name(dest.name + "-wal"), dest.with_name(dest.name + "-shm")):
        path.unlink(missing_ok=True)

    # The backup writes to its own file and is promoted only on success: a
    # thread wedged on Anki's lock keeps its handle open, and Windows will not
    # let us delete or overwrite a file another handle still holds.
    staging = dest.with_name(f"{dest.name}.backup-tmp")
    with suppress(OSError):
        staging.unlink(missing_ok=True)

    if _backup_within(collection, staging, timeout_s):
        try:
            os.replace(staging, dest)
            _flatten(dest)
            return dest
        except OSError as e:
            logger.info("Could not promote the backup (%s); copying instead.", e)

    # The backup thread may still hold its handle, so this can fail on Windows;
    # it is tidiness, not correctness, and the next run overwrites it anyway.
    with suppress(OSError):
        staging.unlink(missing_ok=True)

    try:
        _copy_with_wal(collection, dest)
        notes = _sanity_check(dest)
        _flatten(dest)
    except Exception as e:
        raise CollectionLocked(
            f"Could not read the Anki collection at {collection}: {e}. "
            "Close Anki desktop and try again."
        ) from e

    if notes == 0:
        raise CollectionLocked(
            f"The snapshot of {collection} came out empty. Close Anki desktop and try again."
        )
    logger.warning(
        "Anki is open, so the snapshot was copied rather than backed up. It is "
        "consistent unless Anki wrote to the collection during the copy."
    )
    return dest


# ---------------------------------------------------------------- parsing

def _is_modern(con: sqlite3.Connection) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notetypes'"
    ).fetchone() is not None


def _decks(con: sqlite3.Connection, modern: bool) -> dict[int, str]:
    if modern:
        return {did: name.replace(DECK_SEP, "::") for did, name in con.execute("SELECT id, name FROM decks")}
    raw = json.loads(con.execute("SELECT decks FROM col").fetchone()[0])
    return {int(did): d["name"] for did, d in raw.items()}


def _notetypes(con: sqlite3.Connection, modern: bool) -> dict[int, tuple[str, list[str]]]:
    if modern:
        names = dict(con.execute("SELECT id, name FROM notetypes"))
        fields: dict[int, list[str]] = {}
        for ntid, _ord, fname in con.execute("SELECT ntid, ord, name FROM fields ORDER BY ntid, ord"):
            fields.setdefault(ntid, []).append(fname)
        return {mid: (name, fields.get(mid, [])) for mid, name in names.items()}
    raw = json.loads(con.execute("SELECT models FROM col").fetchone()[0])
    return {int(mid): (m["name"], [f["name"] for f in m.get("flds", [])]) for mid, m in raw.items()}


def _front_back(raw_fields: list[str]) -> tuple[str, str, bool]:
    """Infer front and back without knowing the note type's semantics.

    Real collections mix dozens of note types (Front/Back, Frente/Verso,
    Vorderseite/Rückseite, image-first art cards...). The first two fields that
    still carry text after stripping media are the best general guess.
    """
    # The breakdown of a command under its answer is study material, not the
    # answer: read with it, the back of `dig example.com` stops being that
    # command to dedup.
    raw_fields = [PARTS_TABLE.sub("", f) for f in raw_fields]
    for raw in raw_fields:
        if CLOZE_RE.search(raw):
            text = strip_html(strip_cloze(raw))
            return text, text, True

    texts = [t for t in (strip_html(f) for f in raw_fields) if t]
    if not texts:
        return "", "", False
    return texts[0], texts[1] if len(texts) > 1 else "", False


def read_notes(collection: str | Path) -> list[Note]:
    con = sqlite3.connect(f"{Path(collection).resolve().as_uri()}?mode=ro", uri=True)
    try:
        modern = _is_modern(con)
        decks = _decks(con, modern)
        notetypes = _notetypes(con, modern)

        # One row per note. A note's cards can live in different decks; its home is
        # the deck of its first card. Cards temporarily in a filtered deck carry
        # their real deck in `odid`.
        stats = {}
        for row in con.execute(
            """SELECT nid,
                      CASE WHEN odid != 0 THEN odid ELSE did END AS home_deck,
                      ord, lapses, factor, reps, ivl, queue
               FROM cards ORDER BY nid, ord"""
        ):
            nid, did, _ord, lapses, factor, reps, ivl, queue = row
            s = stats.get(nid)
            if s is None:
                stats[nid] = {"did": did, "lapses": lapses, "ease": factor or 0,
                              "reps": reps, "ivl": ivl, "queue": queue}
            else:
                s["lapses"] = max(s["lapses"], lapses)
                if factor and (not s["ease"] or factor < s["ease"]):
                    s["ease"] = factor
                s["reps"] += reps
                s["ivl"] = max(s["ivl"], ivl)

        notes = []
        for nid, mid, flds, tags, mod in con.execute("SELECT id, mid, flds, tags, mod FROM notes"):
            s = stats.get(nid)
            if s is None:  # orphaned note with no cards
                continue
            front, back, is_cloze = _front_back(flds.split(FIELD_SEP))
            if not front:
                continue
            notes.append(Note(
                note_id=nid,
                deck=decks.get(s["did"], "Unknown"),
                notetype=notetypes.get(mid, ("Unknown", []))[0],
                front=front,
                back=back,
                tags=(tags or "").strip(),
                is_cloze=is_cloze,
                lapses=s["lapses"],
                ease=s["ease"],
                reps=s["reps"],
                interval_days=s["ivl"],
                queue=s["queue"],
                modified_at=mod,
            ))
        return notes
    finally:
        con.close()


# Every card a plan run writes is tagged with the curriculum day it belongs to.
# Cards written for one deck by hand carry the ad-hoc tag as well, and are not
# a day of the curriculum: counting them would skip the day they share a date
# with.
RUN_TAG = re.compile(r"(?<!\S)ankigen::run_(\d{4}-\d{2}-\d{2})(?!\S)", re.IGNORECASE)
AD_HOC_TAG = "ankigen::ad-hoc"


def run_dates(collection: str | Path, raw_dir: str | Path,
              within: Callable[[str], bool] | None = None) -> set[date]:
    """The curriculum days the collection already has cards for.

    With `within`, only cards in the decks it accepts count. Several pipelines
    share one collection, and each one's curriculum is its own: counted across
    the whole collection, a second pipeline's day would move the first's along.
    """
    snap = snapshot(collection, Path(raw_dir) / "next" / "collection.anki2")
    con = sqlite3.connect(f"{snap.resolve().as_uri()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, tags FROM notes WHERE tags LIKE '%ankigen::run%'").fetchall()
        homes = _home_decks(con, {nid for nid, _ in rows}) if within else {}
    finally:
        con.close()
    days = set()
    for nid, tags in rows:
        if AD_HOC_TAG in (tags or "").lower().split():
            continue
        if within and not within(homes.get(nid, "")):
            continue
        for match in RUN_TAG.finditer(tags or ""):
            with suppress(ValueError):
                days.add(date.fromisoformat(match.group(1)))
    return days


def _home_decks(con: sqlite3.Connection, note_ids: set[int]) -> dict[int, str]:
    """Each note's deck: its first card's, or the one a filtered deck took it from."""
    decks = _decks(con, _is_modern(con))
    homes: dict[int, str] = {}
    for nid, did in con.execute(
            "SELECT nid, CASE WHEN odid != 0 THEN odid ELSE did END FROM cards ORDER BY nid, ord"):
        if nid in note_ids and nid not in homes:
            homes[nid] = decks.get(did, "")
    return homes


def next_run_date(collection: str | Path, raw_dir: str | Path, today: date,
                  within: Callable[[str], bool] | None = None,
                  also_done: set[date] = frozenset()) -> date:
    """The curriculum day a plan run writes: the one after the last day the
    collection has cards for, whenever the run happens.

    The curriculum is a queue, not a calendar. A run by hand takes the next day
    rather than redoing today's, so the scheduled run after it carries on from
    there instead of repeating it, and every deck after the current one moves
    up. A day the schedule missed is written by the next run, not skipped.

    `also_done` adds days known from elsewhere: a pipeline that never pushes
    has no cards in the collection to show which days it wrote.
    """
    done = run_dates(collection, raw_dir, within) | set(also_done)
    return max(done) + timedelta(days=1) if done else today


def read_revlog(collection: str | Path, after_id: int = 0) -> list[tuple]:
    con = sqlite3.connect(f"{Path(collection).resolve().as_uri()}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT id, cid, ease, ivl, time, type FROM revlog WHERE id > ? ORDER BY id",
            [after_id],
        ).fetchall()
    finally:
        con.close()


# ---------------------------------------------------------------- stage

NOTE_COLUMNS = (
    "run_date", "note_id", "deck", "notetype", "front", "back", "tags", "is_cloze",
    "content_hash", "lapses", "ease", "reps", "interval_days", "queue", "modified_at",
)


# Every stage after ingest reads only its own run_date's snapshot, so older
# ones are kept only long enough to re-run a recent day without ingesting it
# again. Kept forever, a full snapshot of every day is the one thing in the
# warehouse that grows without bound — and the warehouse rides in the Actions
# cache, restored and saved on every run.
SNAPSHOT_RETENTION_DAYS = 14


def prune_snapshots(wh, run_date: date, keep_days: int = SNAPSHOT_RETENTION_DAYS) -> int:
    """Drop note snapshots older than `keep_days` before `run_date`. Returns
    how many days' snapshots went."""
    cutoff = run_date - timedelta(days=keep_days)
    gone = wh.scalar("SELECT COUNT(DISTINCT run_date) FROM raw_notes WHERE run_date < ?",
                     [cutoff]) or 0
    if gone:
        wh.con.execute("DELETE FROM raw_notes WHERE run_date < ?", [cutoff])
    return gone


def ingest(wh, run_date: date, collection: str | Path, raw_dir: str | Path) -> dict:
    """Snapshot the collection and load it: notes as a full daily snapshot,
    reviews incrementally (only ids newer than what's already loaded)."""
    snap = snapshot(collection, Path(raw_dir) / str(run_date) / "collection.anki2")
    notes = read_notes(snap)

    wh.replace_partition("raw_notes", run_date, NOTE_COLUMNS, [
        (run_date, n.note_id, n.deck, n.notetype, n.front, n.back, n.tags, n.is_cloze,
         n.content_hash, n.lapses, n.ease, n.reps, n.interval_days, n.queue, n.modified_at)
        for n in notes
    ])
    pruned = prune_snapshots(wh, run_date)

    last_review = wh.scalar("SELECT COALESCE(MAX(review_id), 0) FROM raw_revlog")
    reviews = read_revlog(snap, after_id=last_review)
    wh.insert(
        "raw_revlog",
        ("review_id", "card_id", "ease", "interval", "time_ms", "review_type", "loaded_on"),
        [(*r, run_date) for r in reviews],
        or_ignore=True,
    )

    decks = len({n.deck for n in notes})
    logger.info("Ingested %d notes across %d decks, %d new reviews", len(notes), decks, len(reviews))
    return {"notes": len(notes), "decks": decks, "new_reviews": len(reviews), "snapshot": str(snap),
            "pruned_snapshots": pruned}


def load_notes(wh, run_date: date) -> list[Note]:
    rows = wh.query(
        """SELECT note_id, deck, notetype, front, back, tags, is_cloze, lapses, ease,
                  reps, interval_days, queue, modified_at
           FROM raw_notes WHERE run_date = ?""",
        [run_date],
    )
    return [Note(**r) for r in rows]
