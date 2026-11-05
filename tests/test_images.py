"""Image search, download and placement. No network: both sources are faked."""
import io as _io
import json
import sqlite3
import zipfile
from unittest.mock import MagicMock

import pytest
from conftest import RUN_DATE
from PIL import Image

from ankigen import export, generate, images, llm, pipeline
from ankigen.profile import Profile


def _png_bytes(size=(900, 400), mode="RGBA") -> bytes:
    buf = _io.BytesIO()
    Image.new(mode, size, (200, 30, 30, 255) if mode == "RGBA" else (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, content=b"", content_type="image/png", status=200):
        self.content, self.headers, self.status_code = content, {"content-type": content_type}, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, n):
        yield self.content


# ---------------------------------------------------------------- download

def test_png_is_converted_to_jpeg_and_resized(tmp_path, monkeypatch):
    """Anki imports JPEG reliably; PNG and WebP caused failures in v1."""
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(_png_bytes()))
    name = images.download_as_jpeg("https://x/y.png", "kubernetes architecture", tmp_path)
    assert name.endswith(".jpg")
    with Image.open(tmp_path / name) as img:
        assert img.format == "JPEG" and img.mode == "RGB"   # alpha dropped
        assert img.width == images.MAX_WIDTH                # 900 -> 800


def test_same_query_and_url_is_not_downloaded_twice(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(images.requests, "get",
                        lambda *a, **k: (calls.append(1), _Resp(_png_bytes()))[1])
    first = images.download_as_jpeg("https://x/y.png", "airflow dag", tmp_path)
    second = images.download_as_jpeg("https://x/y.png", "airflow dag", tmp_path)
    assert first == second and len(calls) == 1


def test_non_image_responses_leave_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(images.requests, "get",
                        lambda *a, **k: _Resp(b"<html>404</html>", "text/html"))
    assert images.download_as_jpeg("https://x/y", "q", tmp_path) is None
    assert list(tmp_path.glob("*")) == []


def test_corrupt_image_is_deleted(tmp_path, monkeypatch):
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(b"not an image"))
    assert images.download_as_jpeg("https://x/y.jpg", "q", tmp_path) is None
    assert list(tmp_path.glob("*")) == []


def test_duckduckgo_wins_when_it_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "search_duckduckgo",
                        lambda q, **k: [("https://ddg/x.png", "https://docs/p")])
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(_png_bytes()))
    assert images.fetch("u1", "airflow dag", tmp_path).source == "duckduckgo"


def test_both_sources_failing_is_recorded_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "search_duckduckgo", lambda q, **k: [])
    result = images.fetch("u1", "something unillustratable", tmp_path)
    assert not result.found and "no usable image" in result.detail


# ---------------------------------------------------------------- duckduckgo

class _FakeDDGS:
    """Stands in for ddgs.DDGS. `script` is one entry per call: an exception to
    raise, or a list of result dicts to return."""

    script: list = []
    calls = 0

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def images(self, query, max_results=5):
        step = type(self).script[min(type(self).calls, len(type(self).script) - 1)]
        type(self).calls += 1
        if isinstance(step, Exception):
            raise step
        return step


@pytest.fixture
def ddg(monkeypatch):
    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)
    monkeypatch.setattr(images.time, "sleep", lambda s: None)    # no real backoff in tests
    _FakeDDGS.script, _FakeDDGS.calls = [], 0
    return _FakeDDGS


def test_duckduckgo_keeps_trying_until_it_answers(ddg):
    """The search fails in several ways and none of them are permanent, so a
    daily batch waits rather than giving up on the better source."""
    ddg.script = [
        RuntimeError("RequestError: malformed headers"),
        [],                                                      # engine answered with nothing
        [{"image": "https://ddg/good.png", "width": 900,
          "title": "Kubernetes control plane components"}],
    ]
    assert images.search_duckduckgo("kubernetes control plane") == [("https://ddg/good.png", "")]
    assert ddg.calls == 3


def test_duckduckgo_gives_up_after_its_last_attempt(ddg):
    ddg.script = [RuntimeError("ratelimit")]
    assert images.search_duckduckgo("anything", attempts=4) == []
    assert ddg.calls == 4


def test_width_reported_as_a_string_is_still_usable(ddg):
    """ddgs falls back to other engines, and Bing reports width as a string —
    comparing it raised, which quietly threw away every result."""
    ddg.script = [[{"image": "https://bing/x.png", "width": "900", "title": "Docker layers"},
                   {"image": "https://bing/small.png", "width": "80", "title": "Docker layers"}]]
    assert images.search_duckduckgo("docker layers") == [("https://bing/x.png", "")]


def test_one_failing_job_does_not_sink_the_batch(tmp_path, monkeypatch):
    def flaky(uid, query, media_dir, card="", verifier=None, **kwargs):
        if query == "bad":
            raise RuntimeError("boom")
        return images.ImageResult(uid, query, filename="ok.jpg", source="duckduckgo")

    monkeypatch.setattr(images, "fetch", flaky)
    results = images.fetch_many([("u1", "bad"), ("u2", "good")], tmp_path)
    assert [r.found for r in results] == [False, True]


# ---------------------------------------------------------------- prompt + parsing

def test_image_query_only_requested_when_the_deck_wants_images(profile, modern_collection):
    from ankigen.ingest import read_notes
    from ankigen.targeting import build_requests

    notes = read_notes(modern_collection)
    assert any("image_query" in r.prompt for r in build_requests(profile, notes, RUN_DATE))
    profile.images = False
    assert not any("image_query" in r.prompt for r in build_requests(profile, notes, RUN_DATE))


def test_image_query_is_parsed_and_bounded():
    [card] = generate.parse_cards("basic", {"cards": [
        {"question": "What is a pod?", "answer": "The unit of scheduling.",
         "image_query": "kubernetes pod diagram"}]})
    assert card.image_query == "kubernetes pod diagram"
    # Omitted, empty or absurd queries are treated as "no image".
    [plain] = generate.parse_cards("basic", {"cards": [
        {"question": "What is a pod?", "answer": "The unit of scheduling."}]})
    assert plain.image_query == ""
    # A rambling query is trimmed to something a search engine can match.
    [long] = generate.parse_cards("basic", {"cards": [
        {"question": "What is a pod?", "answer": "x.", "image_query": "word " * 40}]})
    assert long.image_query == "word word word word word word"


def test_image_query_is_part_of_the_json_shape(profile, modern_collection):
    """Asked for in a trailing paragraph, the model answers with the keys the
    shape lists and drops this one."""
    from ankigen.ingest import read_notes
    from ankigen.targeting import build_requests

    req = next(r for r in build_requests(profile, read_notes(modern_collection), RUN_DATE)
               if "image_query" in r.prompt)
    shape = next(line for line in req.prompt.splitlines() if line.startswith('{"cards"'))
    assert '"image_query"' in shape


# ---------------------------------------------------------------- export

def _kept_card(wh, image_filename=None, card_type="basic", fields=None):
    fields = fields or {"Question": "What is a pod?", "Answer": "The unit of scheduling."}
    wh.replace_partition("requests", RUN_DATE,
                         ("run_date", "request_id", "deck", "topic", "card_type", "n", "reason", "focus", "prompt"),
                         [(RUN_DATE, "r1", "Data Platform::Kubernetes", "pods", card_type, 1, "topic", None, "p")])
    wh.replace_partition("generated_cards", RUN_DATE, generate.GENERATED_COLUMNS, [
        (RUN_DATE, "u1", "r1", "Data Platform::Kubernetes", card_type, fields.get("Question", "front"),
         "back", json.dumps(fields), "kubernetes pod diagram", "m", 0, 0)])
    wh.replace_partition("verified_cards", RUN_DATE, ("run_date", "card_uid", "passed", "score", "reason"),
                         [(RUN_DATE, "u1", True, 0.9, "")])
    wh.replace_partition("dedup_results", RUN_DATE,
                         ("run_date", "card_uid", "is_dup", "reason", "similarity"),
                         [(RUN_DATE, "u1", False, None, None)])
    wh.replace_partition("card_images", RUN_DATE,
                         ("run_date", "card_uid", "query", "filename", "source", "detail"),
                         [(RUN_DATE, "u1", "kubernetes pod diagram", image_filename, "duckduckgo", "")])


def _package(apkg):
    with zipfile.ZipFile(apkg) as z:
        names = z.namelist()
        media = json.loads(z.read("media")) if "media" in names else {}
        z.extract("collection.anki2", apkg.parent / "x")
    con = sqlite3.connect(apkg.parent / "x" / "collection.anki2")
    try:
        decks = {d["name"] for d in json.loads(con.execute("SELECT decks FROM col").fetchone()[0]).values()}
        flds = [r[0] for r in con.execute("SELECT flds FROM notes")]
    finally:
        con.close()
    return decks, flds, media


def test_image_is_attached_and_packaged(wh, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "pod.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    _kept_card(wh, "pod.jpg")

    result = export.run(wh, RUN_DATE, tmp_path / "out", media_dir=media_dir)
    assert result["images"] == 1
    decks, flds, media = _package(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")
    assert "Data Platform::Kubernetes" in decks          # the real deck, no inbox
    assert '<img src="pod.jpg">' in flds[0]
    assert "pod.jpg" in media.values()


def test_detailed_cards_use_the_image_field(wh, tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "pod.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
    _kept_card(wh, "pod.jpg", card_type="detailed",
               fields={"Question": "Q?", "Summary": "S", "Explanation": "E", "Image": "", "Reference": ""})
    export.run(wh, RUN_DATE, tmp_path / "out", media_dir=media_dir)
    _, flds, _ = _package(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")
    parts = flds[0].split("\x1f")
    assert parts[3] == '<img src="pod.jpg">'            # the Image field, not the answer
    assert parts[1] == "S"


def test_missing_media_file_still_exports_the_card(wh, tmp_path):
    """A half-downloaded image must not cost us the card."""
    _kept_card(wh, "vanished.jpg")
    result = export.run(wh, RUN_DATE, tmp_path / "out", media_dir=tmp_path / "media")
    assert result["kept"] == 1 and result["images"] == 0
    _, flds, media = _package(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")
    assert "<img" not in flds[0] and media == {}


def test_card_without_an_image_exports_normally(wh, tmp_path):
    _kept_card(wh, None)
    export.run(wh, RUN_DATE, tmp_path / "out", media_dir=tmp_path / "media")
    _, flds, media = _package(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")
    assert "<img" not in flds[0] and media == {}


def test_inbox_subdeck_when_the_profile_asks_for_one(wh, tmp_path):
    _kept_card(wh, None)
    profile = Profile.model_validate({"inbox": "AnkiGen",
                                      "decks": [{"deck": "Data Platform::Kubernetes"}]})
    export.run(wh, RUN_DATE, tmp_path / "out", profile, media_dir=tmp_path / "media")
    decks, _, _ = _package(tmp_path / "out" / str(RUN_DATE) / f"ankigen_{RUN_DATE}.apkg")
    assert "Data Platform::Kubernetes::AnkiGen" in decks


# ---------------------------------------------------------------- stage

def test_images_are_only_fetched_for_surviving_cards(wh, tmp_path, monkeypatch, cfg, profile):
    """Fetching before dedup would download pictures for cards about to be binned."""
    wh.replace_partition("requests", RUN_DATE,
                         ("run_date", "request_id", "deck", "topic", "card_type", "n", "reason", "focus", "prompt"),
                         [(RUN_DATE, "r1", "DS::SQL", "t", "basic", 2, "topic", None, "p")])
    wh.replace_partition("generated_cards", RUN_DATE, generate.GENERATED_COLUMNS, [
        (RUN_DATE, "keep", "r1", "DS::SQL", "basic", "Kept?", "y", "{}", "index diagram", "m", 0, 0),
        (RUN_DATE, "drop", "r1", "DS::SQL", "basic", "Dropped?", "y", "{}", "join diagram", "m", 0, 0)])
    wh.replace_partition("verified_cards", RUN_DATE, ("run_date", "card_uid", "passed", "score", "reason"),
                         [(RUN_DATE, "keep", True, 0.9, ""), (RUN_DATE, "drop", True, 0.9, "")])
    wh.replace_partition("dedup_results", RUN_DATE,
                         ("run_date", "card_uid", "is_dup", "reason", "similarity"),
                         [(RUN_DATE, "keep", False, None, None), (RUN_DATE, "drop", True, "dup", 0.99)])

    asked = []
    monkeypatch.setattr(images, "fetch_many",
                        lambda jobs, media_dir, **k: (asked.extend(jobs), [
                            images.ImageResult(j[0], j[1], filename="x.jpg", source="duckduckgo")
                            for j in jobs])[1])
    ctx = pipeline.Context(cfg, profile, wh)
    result = pipeline.stage_images(ctx, RUN_DATE)
    assert [j[0] for j in asked] == ["keep"]
    assert asked[0][2].startswith("Kept?")          # the card text goes along, to check the picture against
    assert result["found"] == 1 and result["wanted"] == 1


def test_images_stage_skipped_for_decks_that_opt_out(wh, tmp_path, monkeypatch, cfg, profile):
    wh.replace_partition("requests", RUN_DATE,
                         ("run_date", "request_id", "deck", "topic", "card_type", "n", "reason", "focus", "prompt"),
                         [(RUN_DATE, "r1", "DS::SQL", "t", "basic", 1, "topic", None, "p")])
    wh.replace_partition("generated_cards", RUN_DATE, generate.GENERATED_COLUMNS, [
        (RUN_DATE, "u1", "r1", "DS::SQL", "basic", "Q?", "A.", "{}", "index diagram", "m", 0, 0)])
    wh.replace_partition("verified_cards", RUN_DATE, ("run_date", "card_uid", "passed", "score", "reason"),
                         [(RUN_DATE, "u1", True, 0.9, "")])
    wh.replace_partition("dedup_results", RUN_DATE,
                         ("run_date", "card_uid", "is_dup", "reason", "similarity"),
                         [(RUN_DATE, "u1", False, None, None)])
    profile.decks[0].images = False        # DS::SQL opts out
    asked = []
    monkeypatch.setattr(images, "fetch_many",
                        lambda jobs, media_dir, **k: (asked.extend(jobs), [])[1])
    result = pipeline.stage_images(pipeline.Context(cfg, profile, wh), RUN_DATE)
    assert result["wanted"] == 0 and asked == []       # nothing requested, nothing downloaded


# ---------------------------------------------------------------- looking at the picture

def test_an_image_the_model_rejects_is_not_used(tmp_path, monkeypatch):
    """A card about S3's flat namespace was illustrated with a stock photo of a
    basketball player, from a page whose title matched the query exactly."""
    monkeypatch.setattr(images, "search_duckduckgo",
                        lambda q, **k: [("https://seo.farm/a.png", "https://a"),
                                                         ("https://ok/b.png", "https://b")])
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(_png_bytes()))

    looked = []

    def verifier(blob, card, query):
        looked.append(query)
        return (len(looked) > 1, "a basketball player" if len(looked) == 1 else "a diagram")

    result = images.fetch("u1", "s3 flat namespace", tmp_path, card="What is a flat namespace?",
                          verifier=verifier)
    assert result.found and result.detail == "shows a diagram"
    assert len(looked) == 2                       # the first candidate was thrown away
    assert len(list(tmp_path.glob("*.jpg"))) == 1  # and its file with it


def test_giving_up_rather_than_checking_the_whole_result_page(tmp_path, monkeypatch):
    """Each look costs a request on a metered free tier."""
    monkeypatch.setattr(images, "search_duckduckgo",
                        lambda q, **k: [(f"https://x/{i}.png", "") for i in range(9)])
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(_png_bytes()))
    looks = []

    result = images.fetch("u1", "kubernetes pods", tmp_path, card="c",
                          verifier=lambda b, c, q: (looks.append(1), (False, "junk"))[1],
                          max_checks=3)
    assert not result.found and "first 3 checked" in result.detail
    assert len(looks) == 3
    assert list(tmp_path.glob("*.jpg")) == []      # nothing left behind


def test_a_picture_nobody_could_check_is_not_used(tmp_path, monkeypatch):
    """The four pictures that once went out unchecked, because the checker was
    busy, were a poster and three costume photos."""
    monkeypatch.setattr(images, "search_duckduckgo",
                        lambda q, **k: [("https://x/a.png", "")])
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(_png_bytes()))

    def down(blob, card, query):
        raise RuntimeError("503 UNAVAILABLE")

    result = images.fetch("u1", "kubernetes pods", tmp_path, card="c", verifier=down)
    assert not result.found and result.detail.startswith(images.UNCHECKED)
    assert list(tmp_path.glob("*.jpg")) == []      # the unchecked download is gone too


def test_a_spent_checker_stops_the_rest_of_the_run_from_asking(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "search_duckduckgo",
                        lambda q, **k: [("https://x/a.png", ""), ("https://x/b.png", "")])
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(_png_bytes()))
    calls = []

    def spent(blob, card, query):
        calls.append(query)
        raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")

    budget = images.CheckBudget(10)
    images.fetch("u1", "kubernetes pods", tmp_path, card="c", verifier=spent, budget=budget)
    later = images.fetch("u2", "kubernetes nodes", tmp_path, card="c", verifier=spent, budget=budget)
    assert calls == ["kubernetes pods"]            # the second card never asked
    assert not later.found and "budget" in later.detail


def test_the_run_budget_is_shared_across_cards(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "search_duckduckgo",
                        lambda q, **k: [(f"https://x/{q}{i}.png", "") for i in range(5)])
    monkeypatch.setattr(images.requests, "get", lambda *a, **k: _Resp(_png_bytes()))
    looks = []
    budget = images.CheckBudget(4)
    for q in ("pods", "nodes", "services"):
        images.fetch("u", q, tmp_path, card="c", budget=budget,
                     verifier=lambda b, c, q: (looks.append(q), (False, "junk"))[1])
    assert len(looks) == 4                         # 3 for the first card, 1 left for the rest


def test_stock_libraries_and_scraper_buckets_are_skipped():
    assert images._looks_like_junk("https://media.gettyimages.com/photos/x.jpg")
    assert images._looks_like_junk("https://storage.googleapis.com/djiuedjsglnrce/flat-file.jpg")
    assert not images._looks_like_junk("https://docs.aws.amazon.com/images/s3-namespace.png")


def test_documentation_is_tried_before_a_random_blog(monkeypatch):
    """Search relevance put a duck ahead of airflow.apache.org, and taking the
    first result that downloaded is how it reached a card."""
    class _DDGS:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def images(self, query, max_results=10):
            return [
                {"image": "https://cdn/blog.png", "url": "https://someblog.example/post",
                 "width": 800, "title": "Airflow triggerer explained"},
                {"image": "https://cdn/docs.png", "url": "https://airflow.apache.org/docs/",
                 "width": 800, "title": "Deferrable operators & triggers"},
                {"image": "https://cdn/so.png", "url": "https://stackoverflow.com/q/1",
                 "width": 800, "title": "How does the Airflow triggerer work?"},
            ]

    monkeypatch.setattr("ddgs.DDGS", _DDGS)
    pages = [page for _img, page in images.search_duckduckgo("airflow triggerer")]
    assert pages[0].startswith("https://airflow.apache.org")
    assert pages[1].startswith("https://stackoverflow.com")
    assert pages[2].startswith("https://someblog")      # kept, just tried last


def test_deck_context_goes_in_front_of_the_query_once():
    assert (images.with_context("airflow pool slots diagram", "Apache Airflow")
            == "Apache Airflow pool slots diagram")
    assert (images.with_context("S3 lifecycle diagram", "AWS")
            == "AWS S3 lifecycle diagram")


def test_no_context_leaves_the_query_alone():
    assert images.with_context("kubernetes pod lifecycle", "") == "kubernetes pod lifecycle"
    assert images.with_context("", "Apache Airflow") == ""


def test_profile_finds_the_context_for_a_deck():
    profile = Profile.model_validate({"decks": [
        {"deck": "Data Platform::Airflow", "image_context": "Apache Airflow"},
        {"deck": "DS::SQL"},
    ]})
    assert profile.image_context_for("Data Platform::Airflow") == "Apache Airflow"
    assert profile.image_context_for("DS::SQL") == ""
    assert profile.image_context_for("Not::A::Deck") == ""


# ---------------------------------------------------------------- relevance before looking

def _results(*titles, url="https://example.com/p"):
    return [{"image": f"https://cdn/{i}.png", "url": url, "width": 900, "title": t}
            for i, t in enumerate(titles)]


def test_results_that_share_no_word_with_the_subject_are_skipped(ddg):
    """On a bad day the runner got an attack helicopter for Airflow pools: it
    matched "Apache", which is the deck's context, not the subject."""
    ddg.script = [_results("AH-64 Apache attack helicopter",
                           "Apache Airflow: Pools. Optimizing workflow concurrency",
                           "Famous Apache Indians")]
    found = images.search_duckduckgo("Apache Airflow task queue pools diagram",
                                     context="Apache Airflow")
    assert found == [("https://cdn/1.png", "https://example.com/p")]


def test_a_page_of_trending_junk_counts_as_a_failed_try(ddg):
    ddg.script = [_results("Paw Patrol Halloween costumes", "Star Trek: Strange New Worlds"),
                  _results("Amazon S3 storage classes compared")]
    found = images.search_duckduckgo("Amazon S3 storage classes comparison table", context="AWS")
    assert len(found) == 1 and ddg.calls == 2     # searched again rather than looking at junk


def test_the_address_counts_as_well_as_the_title(ddg):
    ddg.script = [[{"image": "https://docs.aws.amazon.com/images/lifecycle-transitions-v4.png",
                    "url": "https://hyeon9mak.github.io/aws-s3-glacier/", "width": 900,
                    "title": "S3 Glacier 정리"}]]
    assert images.search_duckduckgo("AWS S3 lifecycle transition timeline", context="AWS")


def test_topic_terms_leave_out_the_kind_of_picture_and_the_context():
    assert images.topic_terms("Apache Airflow deferrable operator lifecycle diagram",
                              "Apache Airflow") == {"deferrable", "operator"}
    assert images.topic_terms("diagram", "") == set()
    assert images.looks_relevant(set(), "anything")          # nothing to match: no gate


def test_endings_do_not_stop_a_match():
    assert images.looks_relevant({"pools"}, "Apache Airflow: Pool slots")
    assert images.looks_relevant({"classes"}, "S3 storage class comparison")
    assert not images.looks_relevant({"pools"}, "Kelp forest, underwater")


def test_preferred_sources_are_matched_by_host():
    """".edu" anywhere in the address once ranked an SEO farm first."""
    assert images._rank(("i", "https://airflow.apache.org/docs/")) == 0
    assert images._rank(("i", "https://cs.stanford.edu/notes")) == 0
    assert images._rank(("i", "https://design.udlvirtual.edu.pe/en/docker")) == 1
    assert images._rank(("i", "https://notapache.org.example.com/")) == 1
    assert images._rank(("i", "")) == 1


def test_hosts_that_refuse_downloads_are_skipped():
    assert images._looks_like_junk("https://www.researchgate.net/figure/x.png")
    assert images._looks_like_junk("https://exowdvyzb.blob.core.windows.net/a.html")


def test_a_cloze_hint_keeps_its_first_letter():
    """`.lstrip("<br>")` strips characters, not a prefix: "remember" lost its r."""
    values = export._with_image("cloze", {"Text": "t", "Extra": "remember the port"}, "x.jpg")
    assert values["Extra"] == 'remember the port<br><img src="x.jpg">'
    assert export._with_image("cloze", {"Text": "t", "Extra": ""}, "x.jpg")["Extra"] \
        == '<img src="x.jpg">'


def test_looking_at_pictures_counts_on_the_images_stage(wh, monkeypatch, cfg, profile):
    wh.replace_partition("requests", RUN_DATE,
                         ("run_date", "request_id", "deck", "topic", "card_type", "n", "reason", "focus", "prompt"),
                         [(RUN_DATE, "r1", "DS::SQL", "t", "basic", 1, "topic", None, "p")])
    wh.replace_partition("generated_cards", RUN_DATE, generate.GENERATED_COLUMNS, [
        (RUN_DATE, "keep", "r1", "DS::SQL", "basic", "Kept?", "y", "{}", "index diagram", "m", 0, 0)])
    wh.replace_partition("verified_cards", RUN_DATE, ("run_date", "card_uid", "passed", "score", "reason"),
                         [(RUN_DATE, "keep", True, 0.9, "")])
    wh.replace_partition("dedup_results", RUN_DATE,
                         ("run_date", "card_uid", "is_dup", "reason", "similarity"),
                         [(RUN_DATE, "keep", False, None, None)])
    answer = MagicMock()
    answer.choices = [MagicMock(message=MagicMock(content='{"shows": "a b-tree", "helps": true}'))]
    answer.usage = MagicMock(prompt_tokens=900, completion_tokens=9)
    client = MagicMock()
    client.chat.completions.create.return_value = answer
    monkeypatch.setattr(llm, "_get_openai_client", lambda *a: client)
    monkeypatch.setattr(images, "fetch_many", lambda jobs, media_dir, verifier=None, **k: [
        (verifier(b"jpeg", j[2], j[1]), images.ImageResult(j[0], j[1], filename="x.jpg"))[1]
        for j in jobs])

    detail = pipeline.run_stage(pipeline.Context(cfg, profile, wh), "images", RUN_DATE)
    assert detail["tokens"] == {cfg.resolve_verify()["model"]: {"prompt": 900, "completion": 9}}
