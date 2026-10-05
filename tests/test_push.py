"""Pushing cards into Anki over AnkiWeb sync.

The collection these touch is a real one belonging to a real person, with
years of review history in it, so the tests here are mostly about what the
code refuses to do.
"""
import pytest

from ankigen import push

pytest.importorskip("anki", reason="the push extra is not installed")

from anki.sync_pb2 import SyncAuth, SyncCollectionResponse as Response  # noqa: E402


def _auth(endpoint=""):
    return SyncAuth(hkey="k", endpoint=endpoint)


class _Note:
    def __init__(self, fields=None):
        self.guid = ""
        self.tags = []
        self.fields = dict(fields or {})

    def __setitem__(self, k, v): self.fields[k] = v
    def __getitem__(self, k): return self.fields[k]
    def keys(self): return list(self.fields)


class _Col:
    """Just enough collection to drive sync and add_note.

    `existing` is a set of GUIDs already in the collection, or a mapping of
    GUID to the fields of the note that has it.
    """

    def __init__(self, required=Response.NORMAL_SYNC, existing=(), endpoint="", note_count=0):
        self.required = required
        self.endpoint = endpoint
        self.note_count = note_count
        existing = existing if isinstance(existing, dict) else {g: {} for g in existing}
        self.notes = {i: _Note(f) for i, f in enumerate(existing.values(), start=1)}
        self.ids = {g: i for i, g in enumerate(existing, start=1)}
        self.existing = set(existing)
        self.added = []
        self.updated = []
        self.uploads = []
        self.media_asked = []
        self.db = self
        self.decks = self
        self.media = self

    # --- sync
    def sync_collection(self, auth, media):
        self.media_asked.append(media)
        return Response(required=self.required)

    def sync_status(self, auth):
        from anki.sync_pb2 import SyncStatusResponse
        return SyncStatusResponse(required=SyncStatusResponse.NORMAL_SYNC,
                                  new_endpoint=self.endpoint)

    def full_upload_or_download(self, *, auth, server_usn, upload):
        self.uploads.append(upload)

    # --- collection bits used by push_cards
    def scalar(self, sql, guid=None):
        if "COUNT(*) FROM notes" in sql:
            return self.note_count
        return self.ids.get(guid)

    def get_note(self, nid):
        return self.notes[nid]

    def update_note(self, note):
        self.updated.append(note)

    def id(self, name):
        return 1

    def add_file(self, path):
        return "stored.jpg"

    def new_note(self, notetype):
        return _Note()

    def add_note(self, note, deck_id):
        self.added.append(note)


def test_a_one_way_sync_is_refused_rather_than_guessed():
    """Satisfying it means declaring one side the winner, and choosing ours
    would throw away however much review history this copy does not have."""
    for required in (Response.FULL_SYNC, Response.FULL_UPLOAD, Response.FULL_DOWNLOAD):
        with pytest.raises(push.SyncRefused, match="one-way sync"):
            push.sync(_Col(required), auth=_auth())


def test_an_ordinary_sync_goes_through():
    assert push.sync(_Col(Response.NORMAL_SYNC), _auth())[0] == "synced"
    assert push.sync(_Col(Response.NO_CHANGES), _auth())[0] == "in sync"


def test_the_account_s_own_sync_host_is_found_before_transferring(tmp_path, monkeypatch):
    """AnkiWeb spreads accounts over hosts; using the default one fails a full
    download with "missing original size", which explains nothing."""
    col = _Col(endpoint="https://sync7.ankiweb.net/")
    monkeypatch.setattr("anki.collection.Collection", lambda path: col)
    monkeypatch.setattr(push.settings, "data_dir", str(tmp_path), raising=False)

    _opened, auth = push.open_collection(_auth())
    assert auth.endpoint == "https://sync7.ankiweb.net/"


def test_a_card_already_in_the_collection_is_not_added_twice(tmp_path, monkeypatch):
    """The GUID is the one the .apkg export uses, so importing the package and
    pushing the same run cannot produce two copies of a card."""
    import genanki

    card = {"card_uid": "u1", "deck": "DS::SQL", "card_type": "basic",
            "fields_json": '{"Question": "Q?", "Answer": "A."}', "tags": ["ankigen"]}
    monkeypatch.setattr(push, "_notetype", lambda col, ct: {"name": "AnkiGen Basic"})

    fresh = _Col()
    assert push.push_cards(fresh, [card], tmp_path).added == 1

    known = _Col(existing={genanki.guid_for("u1")})
    result = push.push_cards(known, [card], tmp_path)
    assert (result.added, result.skipped) == (0, 1)
    assert known.added == []


def test_nothing_is_ever_uploaded_wholesale(tmp_path, monkeypatch):
    """full_upload_or_download exists to fetch a first copy. Called the other
    way it replaces the server's collection with ours."""
    col = _Col()
    monkeypatch.setattr(push, "WORKING_COPY", "anki/test.anki2")
    monkeypatch.setattr("anki.collection.Collection", lambda path: col)
    monkeypatch.setattr(push.settings, "data_dir", str(tmp_path), raising=False)

    push.open_collection(_auth())
    assert col.uploads == [False]          # downloaded, never uploaded


def test_an_empty_working_copy_is_downloaded_again(tmp_path, monkeypatch):
    """A failed run cached an empty collection file; every run after it then
    believed it already had yours."""
    monkeypatch.setattr(push.settings, "data_dir", str(tmp_path), raising=False)

    empty = _Col(note_count=0)
    monkeypatch.setattr("anki.collection.Collection", lambda path: empty)
    push.open_collection(_auth())
    assert empty.uploads == [False]        # noticed it was empty, fetched a copy

    full = _Col(note_count=8346)
    monkeypatch.setattr("anki.collection.Collection", lambda path: full)
    push.open_collection(_auth())
    assert full.uploads == []              # already had one, left it alone


# ---------------------------------------------------------------- pictures

def _jpeg(path):
    from PIL import Image
    Image.new("RGB", (900, 300), (30, 60, 200)).save(path, "JPEG")


def _detailed(card_uid="u1", image=None):
    return {"card_uid": card_uid, "deck": "Data Platform::Docker", "card_type": "detailed",
            "fields_json": '{"Question": "Q?", "Summary": "S", "Explanation": "E", '
                           '"Image": "", "Reference": ""}',
            "tags": ["ankigen"], "image_filename": image}


def test_media_is_never_synced(tmp_path):
    """Media sync would pull the whole media folder, over a gigabyte, down to
    the runner — and it runs in the background, so closing the collection
    cancelled it and the pictures never went up."""
    col = _Col()
    push.sync(col, _auth())
    assert col.media_asked == [False]


def test_a_new_card_carries_its_picture_inline(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "_notetype", lambda col, ct: {"name": "AnkiGen Detailed"})
    _jpeg(tmp_path / "docker_layers_1234abcd.jpg")
    col = _Col()
    result = push.push_cards(col, [_detailed(image="docker_layers_1234abcd.jpg")], tmp_path)
    [note] = col.added
    assert note.fields["Image"].startswith('<img src="data:image/jpeg;base64,')
    assert result.media == 1


def test_inline_pictures_are_kept_small(tmp_path):
    from ankigen import images
    _jpeg(tmp_path / "big.jpg")
    src = images.inline_src(tmp_path / "big.jpg")
    import base64, io
    from PIL import Image
    with Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))) as img:
        assert img.width == images.INLINE_WIDTH


def test_a_broken_picture_from_an_earlier_push_is_repaired(tmp_path, monkeypatch):
    """The first pushed notes point at files that never reached AnkiWeb."""
    import genanki
    guid = genanki.guid_for("u1")
    col = _Col(existing={guid: {"Question": "Q?", "Summary": "S", "Explanation": "E",
                                "Image": '<img src="docker_image_registry_tag_vs_57ff313d.jpg">',
                                "Reference": ""}})
    _jpeg(tmp_path / "docker_digest_0badf00d.jpg")

    result = push.push_cards(col, [_detailed(image="docker_digest_0badf00d.jpg")], tmp_path)
    assert result.updated == 1 and result.added == 0
    assert col.notes[1].fields["Image"].startswith('<img src="data:image/jpeg;base64,')


def test_a_picture_the_day_no_longer_has_is_taken_off(tmp_path):
    import genanki
    guid = genanki.guid_for("u1")
    col = _Col(existing={guid: {"Question": "Q?", "Summary": "S", "Explanation": "E",
                                "Image": '<img src="kelp_forest_0badf00d.jpg">', "Reference": ""}})
    result = push.push_cards(col, [_detailed(image=None)], tmp_path)
    assert result.updated == 1
    assert col.notes[1].fields["Image"] == ""


def test_a_picture_that_cannot_be_read_here_is_left_alone(tmp_path):
    """A push without the images stage has no files; that is not "no picture"."""
    import genanki
    guid = genanki.guid_for("u1")
    before = '<img src="data:image/jpeg;base64,AAAA">'
    col = _Col(existing={guid: {"Question": "Q?", "Summary": "S", "Explanation": "E",
                                "Image": before, "Reference": ""}})
    result = push.push_cards(col, [_detailed(image="not_downloaded_here_1234abcd.jpg")], tmp_path)
    assert (result.updated, result.skipped) == (0, 1)
    assert col.notes[1].fields["Image"] == before and col.updated == []


def test_only_our_picture_is_replaced_in_a_shared_field(tmp_path):
    """Basic cards carry the picture at the end of the answer, where your own
    edits live too."""
    import genanki
    guid = genanki.guid_for("u2")
    col = _Col(existing={guid: {"Question": "Q?",
                                "Answer": 'Edited by hand.<br><img src="pods_0badf00d.jpg">'}})
    card = {"card_uid": "u2", "deck": "DS::SQL", "card_type": "basic",
            "fields_json": '{"Question": "Q?", "Answer": "A."}', "tags": [], "image_filename": None}
    push.push_cards(col, [card], tmp_path)
    assert col.notes[1].fields["Answer"] == "Edited by hand."


def test_an_unchanged_note_is_not_rewritten(tmp_path):
    import genanki
    guid = genanki.guid_for("u1")
    col = _Col(existing={guid: {"Question": "Q?", "Summary": "S", "Explanation": "E",
                                "Image": "", "Reference": ""}})
    result = push.push_cards(col, [_detailed(image=None)], tmp_path)
    assert (result.updated, result.skipped) == (0, 1) and col.updated == []


def test_a_drawn_visual_replaces_the_one_before_it(tmp_path):
    import genanki
    guid = genanki.guid_for("u1")
    old = '<div class="ankigen-visual" style="x"><table><tr><td>old</td></tr></table></div>'
    col = _Col(existing={guid: {"Question": "Q?", "Summary": "S", "Explanation": "E",
                                "Image": old, "Reference": ""}})
    card = _detailed(image=None)
    card["visual_html"] = '<div class="ankigen-visual" style="x"><svg>new</svg></div>'
    result = push.push_cards(col, [card], tmp_path)
    assert result.updated == 1
    assert col.notes[1].fields["Image"] == card["visual_html"]


def test_a_visual_in_a_shared_field_leaves_your_text_alone(tmp_path):
    import genanki
    guid = genanki.guid_for("u2")
    col = _Col(existing={guid: {"Text": "t", "Extra": 'my note<div class="ankigen-visual">'
                                                      '<table></table></div>'}})
    card = {"card_uid": "u2", "deck": "DS::SQL", "card_type": "cloze",
            "fields_json": '{"Text": "t", "Extra": ""}', "tags": [], "image_filename": None,
            "visual_html": '<div class="ankigen-visual"><svg>new</svg></div>'}
    push.push_cards(col, [card], tmp_path)
    assert col.notes[1].fields["Extra"] == 'my note<div class="ankigen-visual"><svg>new</svg></div>'


def test_a_working_picture_survives_a_warehouse_that_knows_of_none(tmp_path):
    """A branch run repaired a day's pictures from its own warehouse; pushing
    the same day from master's older one must not take them back off."""
    import genanki
    inline = '<img src="data:image/jpeg;base64,AAAA">'
    drawn = '<div class="ankigen-visual"><svg></svg></div>'
    col = _Col(existing={
        genanki.guid_for("u1"): {"Question": "Q?", "Summary": "S", "Explanation": "E",
                                 "Image": inline, "Reference": ""},
        genanki.guid_for("u2"): {"Question": "Q?", "Summary": "S", "Explanation": "E",
                                 "Image": drawn, "Reference": ""},
    })
    result = push.push_cards(col, [_detailed("u1"), _detailed("u2")], tmp_path)
    assert result.updated == 0 and col.updated == []
    assert col.notes[1].fields["Image"] == inline and col.notes[2].fields["Image"] == drawn


def test_a_new_card_points_to_its_guide_section(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "_notetype", lambda col, ct: {"name": "AnkiGen Detailed"})
    col = _Col()
    card = {**_detailed(), "run_date": "2026-10-03", "guide_ref": "2.3"}
    push.push_cards(col, [card], tmp_path)
    [note] = col.added
    assert note.fields["Explanation"].startswith("E")
    assert "Guide 2026-10-03 · §2.3" in note.fields["Explanation"]
