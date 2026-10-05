"""Put the day's cards into Anki itself, over AnkiWeb sync.

AnkiWeb has no API for adding notes. What it has is the sync protocol, which
the official `anki` package speaks, so the way in is to keep a collection of
our own, add notes to it, and sync it up. Your devices then sync down as
usual and the cards are simply there.

The order matters, and it is the whole safety story:

    sync down  ->  add notes  ->  sync up

Syncing down first means our copy is never behind yours, which is what would
otherwise make the server demand a one-way sync. If it demands one anyway we
stop, because the only way to satisfy it is to declare one side the winner,
and picking ours would overwrite however many years of review history with
whatever this copy happens to hold. `full_upload_or_download` is therefore
called in exactly one place: the first ever run, to fetch a copy when we have
none. It is never called with upload=True.

One wrinkle of the protocol: AnkiWeb spreads accounts over several sync hosts
and names the right one on first contact, so every session starts with a
handshake. Going straight to the default host fails a full download with
`400 missing original size`, which is not a hint about anything.

Media is never synced from here, and pictures travel inside the note instead,
as `data:` URIs. Media sync is what uploads a picture file, but it is also how
a client catches up on every file it lacks: for this working copy that is the
whole of your media folder, over a gigabyte, on every run. It also runs in the
background, and closing the collection cancels it — which is how the first
pushed pictures arrived on phones as broken-image icons: the notes went up,
the files never did.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ankigen.card_types import CARD_TYPES
from ankigen.config import settings

logger = logging.getLogger(__name__)

# The working copy. Not your collection: ours, kept in the data directory and
# reconciled with AnkiWeb on every push.
WORKING_COPY = "anki/collection.anki2"


class SyncRefused(RuntimeError):
    """The server wants a one-way sync, which we will not choose for you."""


class NotLoggedIn(RuntimeError):
    """No AnkiWeb credentials configured."""


@dataclass
class PushResult:
    added: int
    skipped: int
    media: int              # pictures carried inline, on new and updated notes
    decks: list[str]
    updated: int = 0        # notes already there whose picture changed

    def __str__(self) -> str:
        return (f"{self.added} added, {self.updated} updated, {self.skipped} already there, "
                f"{self.media} picture(s), decks: {', '.join(self.decks) or 'none'}")


def _auth():
    """A SyncAuth, from a stored key if there is one, otherwise by logging in."""
    from anki.sync_pb2 import SyncAuth

    endpoint = settings.ankiweb_endpoint or None
    if settings.ankiweb_key:
        return SyncAuth(hkey=settings.ankiweb_key, endpoint=endpoint)
    if not (settings.ankiweb_username and settings.ankiweb_password):
        raise NotLoggedIn(
            "Set ANKIWEB_KEY, or ANKIWEB_USERNAME and ANKIWEB_PASSWORD, to push to AnkiWeb. "
            "`ankigen push --login` trades the password for a key you can store instead."
        )
    from anki.collection import Collection

    scratch = Path(settings.data_dir) / "anki" / "_login.anki2"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    col = Collection(str(scratch))
    try:
        return col.sync_login(settings.ankiweb_username, settings.ankiweb_password, endpoint)
    finally:
        col.close()
        scratch.unlink(missing_ok=True)


def _at(auth, endpoint: str):
    from anki.sync_pb2 import SyncAuth

    if not endpoint or endpoint == auth.endpoint:
        return auth
    logger.info("AnkiWeb redirected us to %s", endpoint)
    return SyncAuth(hkey=auth.hkey, endpoint=endpoint)


def resolve_endpoint(col, auth):
    """Find which sync host this account lives on.

    AnkiWeb spreads accounts over several hosts and tells a client which one
    it belongs to on first contact. Skipping that handshake and going straight
    to the default host fails a full download with `400 missing original
    size`, which says nothing about the real problem.
    """
    return _at(auth, col.sync_status(auth).new_endpoint)


def open_collection(auth=None):
    """Our working copy, downloaded from AnkiWeb the first time.

    Returns the collection and the auth to keep using, which may now point at
    a different host than the one we started with.
    """
    from anki.collection import Collection

    path = Path(settings.data_dir) / WORKING_COPY
    path.parent.mkdir(parents=True, exist_ok=True)
    col = Collection(str(path))
    if auth is None:
        return col, auth

    # "Do we have a copy" is about content, not about the file existing.
    # Opening a path creates an empty collection there, and a failed run once
    # cached that empty file — after which every later run believed it already
    # had your collection and refused to sync against the real one.
    empty = not (col.db.scalar("SELECT COUNT(*) FROM notes") or 0)

    auth = resolve_endpoint(col, auth)
    if empty:
        logger.info("No working copy yet; downloading your collection from AnkiWeb.")
        # The only full transfer this module performs, and only ever downward.
        col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
    return col, auth


def sync(col, auth) -> tuple[str, object]:
    """Reconcile notes with AnkiWeb, or refuse to guess. Returns (what happened, auth).

    Never media: see the module docstring.
    """
    from anki.sync_pb2 import SyncCollectionResponse as Response

    out = col.sync_collection(auth, False)
    if out.new_endpoint:
        auth = _at(auth, out.new_endpoint)
        out = col.sync_collection(auth, False)
    required = out.required
    if required in (Response.FULL_SYNC, Response.FULL_UPLOAD, Response.FULL_DOWNLOAD):
        raise SyncRefused(
            "AnkiWeb wants a one-way sync, which means our copy and yours have "
            "diverged beyond what a normal sync can merge. Resolving it means "
            "choosing which side wins, and choosing ours could discard your "
            "review history — so it is yours to make. Sync from Anki on your "
            "own machine, then delete "
            f"{Path(settings.data_dir) / WORKING_COPY} so the next push starts "
            "from a fresh download."
        )
    # NO_CHANGES is what the server says once everything has been reconciled,
    # including changes we just sent — so this describes the state afterwards
    # rather than claiming nothing happened.
    state = {Response.NO_CHANGES: "in sync",
             Response.NORMAL_SYNC: "synced"}.get(required, "synced")
    return state, auth


def _notetype(col, card_type: str):
    """The notetype for one of our card types, created if the collection lacks it."""
    spec = CARD_TYPES[card_type]
    name = f"AnkiGen {card_type.capitalize()}"
    existing = col.models.by_name(name)
    if existing:
        return existing

    logger.info("Creating notetype %r", name)
    is_cloze = spec.get("model_type") == 1
    nt = col.models.new(name)
    if is_cloze:
        nt["type"] = 1
    for fieldname in spec["fields"]:
        col.models.add_field(nt, col.models.new_field(fieldname))
    from ankigen.export import templates
    for tname, front, back in templates(spec):
        template = col.models.new_template(tname)
        template["qfmt"], template["afmt"] = front, back
        col.models.add_template(nt, template)
    nt["css"] = spec["css"]
    col.models.add(nt)
    return col.models.by_name(name)


def push_cards(col, cards: list[dict], media_dir: Path, deck_for=None) -> PushResult:
    """Add cards the collection does not already have, and bring the pictures
    of the ones it does up to date.

    Identified by the same GUID the .apkg export uses, so a card pushed here
    and a card imported from the package are the same note rather than two.
    A note that is already there is otherwise left alone: only the picture
    this program put on it is replaced, so re-running a day's images stage
    and pushing again repairs that day's pictures without touching your edits.
    """
    import genanki

    added = skipped = updated = pictures = 0
    decks: set[str] = set()
    for card in cards:
        guid = genanki.guid_for(card["card_uid"])
        picture = _picture(card, media_dir)
        nid = col.db.scalar("SELECT id FROM notes WHERE guid = ?", guid)
        if nid:
            if picture is not None and _refresh_picture(col, nid, card["card_type"], picture):
                updated += 1
                pictures += bool(picture)
            else:
                skipped += 1
            continue

        deck_name = deck_for(card["deck"]) if deck_for else card["deck"]
        deck_id = col.decks.id(deck_name)          # creates it when missing
        decks.add(deck_name)

        notetype = _notetype(col, card["card_type"])
        note = col.new_note(notetype)
        note.guid = guid

        values = with_guide_ref(card["card_type"], json.loads(card["fields_json"]),
                                card.get("run_date"), card.get("guide_ref"))
        if picture:
            values = with_picture(card["card_type"], values, picture)
            pictures += 1

        for fieldname in CARD_TYPES[card["card_type"]]["fields"]:
            note[fieldname] = str(values.get(fieldname, ""))
        note.tags = [t for t in card.get("tags", []) if t]

        col.add_note(note, deck_id)
        added += 1

    return PushResult(added, skipped, pictures, sorted(decks), updated)


def _picture(card: dict, media_dir: Path) -> str | None:
    """The card's picture as HTML for the note: a drawn visual, or an inline
    `<img>`; "" for no picture; None when that cannot be known here.

    None matters. A push run without the images stage has no downloaded
    files, and reading that as "no picture" would strip every picture from
    the day's notes.
    """
    if card.get("visual_html"):
        return card["visual_html"]
    filename = card.get("image_filename")
    if not filename:
        return ""
    path = media_dir / filename
    if not path.exists():
        return None
    from ankigen.images import inline_src

    return f'<img src="{inline_src(path)}">'


# The field each card type carries its picture in, and the pictures this
# program has put there: drawn visuals, inline images, and files named the way
# images.py names them — which is what the first pushed notes carry, pointing
# at files that never reached AnkiWeb.
PICTURE_FIELD = {"detailed": "Image", "basic": "Answer", "cloze": "Extra", "command": "Note"}
_OUR_PICTURE = re.compile(
    r'(?:<br>)?<img src="(?:data:image/[a-z]+;base64,[A-Za-z0-9+/=]+|[a-z0-9_]+_[0-9a-f]{8}\.jpg)">'
    r'|<div class="ankigen-visual"[^>]*>.*?</div>',
    re.DOTALL,
)
# The only pictures removed without a replacement: files this program named,
# which never reached AnkiWeb and so can never show.
_BROKEN_PICTURE = re.compile(r'(?:<br>)?<img src="[a-z0-9_]+_[0-9a-f]{8}\.jpg">')


def _refresh_picture(col, nid, card_type: str, picture: str) -> bool:
    """Replace the picture on an existing note. Returns whether it changed."""
    field = PICTURE_FIELD.get(card_type)
    note = col.get_note(nid)
    if not field or field not in note.keys():
        return False
    current = note[field]
    if picture:
        want = with_picture(card_type, {field: _OUR_PICTURE.sub("", current)}, picture)[field]
    else:
        # "No picture" is only as current as the warehouse that says it, and
        # a run on another branch keeps a warehouse of its own: trusting one
        # here would have stripped the pictures a repair run had just put on
        # a day's notes. So a working picture stays; a broken one goes.
        want = _BROKEN_PICTURE.sub("", current)
    if want == current:
        return False
    note[field] = want
    col.update_note(note)
    return True


def with_guide_ref(card_type: str, values: dict, run_date, ref: str | None) -> dict:
    """Same reference line the .apkg export adds."""
    from ankigen.export import with_guide_ref as place

    return place(card_type, values, run_date, ref)


def with_picture(card_type: str, values: dict, picture: str) -> dict:
    """Same placement the .apkg export uses, so both routes look alike."""
    from ankigen.export import with_picture as place

    return place(card_type, values, picture)
