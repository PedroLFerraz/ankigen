"""Cards from a book: splitting it by its bookmarks, reading its printed page
numbers, and turning the model's answer into notes."""
import json
from datetime import date
from pathlib import Path

from PIL import Image

from ankigen import book


class Mark:
    def __init__(self, title, page):
        self.title, self.page = title, page


class Page:
    def __init__(self, text=""):
        self.text = text

    def extract_text(self):
        return self.text


class Reader:
    """Shaped like pypdf's PdfReader, as far as book.py reads it."""

    def __init__(self, pages, outline=(), labels=()):
        self.pages, self.outline, self.page_labels = pages, list(outline), list(labels)

    def get_destination_page_number(self, mark):
        return mark.page


def test_chapters_split_at_their_sections_and_skip_reading_lists():
    reader = Reader([Page()] * 20, [
        Mark("Preface", 0), Mark("Part I", 2), [
            Mark("Chapter 1. One", 3), [Mark("A", 3), Mark("B", 6), Mark("Additional Resources", 8)],
            Mark("Chapter 2. Two", 10)],
        Mark("Index", 15)])
    one, two = book.chapters(reader)
    assert (one.title, one.first, one.last) == ("Chapter 1. One", 3, 9)
    assert [(s.title, s.first, s.last) for s in one.sections] == [("A", 3, 5), ("B", 6, 7)]
    # No sections of its own: the whole chapter is one.
    assert [(s.title, s.first, s.last) for s in two.sections] == [("Chapter 2. Two", 10, 14)]


def test_page_numbers_come_from_the_printed_footer():
    # The PDF's own labels are off by one from the print in the real book.
    reader = Reader([Page("Chapter 1"), Page("body\n4 | Chapter 1: One"),
                     Page("body\nA section | 5"), Page("")], labels="wxyz")
    assert [book.label(reader, i) for i in range(4)] == ["3", "4", "5", "6"]


def test_title_drops_the_authors_and_the_source():
    assert book.title_of(Path("Fundamentals of Data Engineering (Reis, Joe) (Z-Library).pdf")) \
        == "Fundamentals of Data Engineering"


def test_cards_cite_their_page_and_carry_their_figure(tmp_path):
    Image.new("RGB", (40, 20), "white").save(tmp_path / "f.png")
    fig = book.Figure("1-1", "The lifecycle", "5", tmp_path / "f.png")
    chapter = book.Chapter(1, "Chapter 1. One", 0, 3)
    section = book.Section("What Is It?", 0, 3)
    data = {"cards": [
        {"question": "What does a data engineer build?", "answer": "Systems that move data.",
         "context": "Upstream of analysts.", "page": "4", "figure": "1-1"},
        {"question": "Why?", "answer": "x"},                        # too short to keep
        {"question": "What is the lifecycle's first stage?", "answer": "Generation.",
         "page": "5", "figure": "9-9"},                             # no such figure
    ]}
    first, last = book.parse(data, "The Book", chapter, section, {"1-1": fig},
                             "The Book::01 One", date(2026, 10, 17))

    fields = json.loads(first["fields_json"])
    assert fields["Summary"] == "Systems that move data."
    assert "The Book · ch. 1 · What Is It? · p. 4" in fields["Explanation"]
    assert first["visual_html"].startswith('<img src="data:image/jpeg;base64,')
    assert first["request_reason"] == "book::the-book::ch01"
    assert last["visual_html"] is None
    # Stable, so writing the chapter again updates the same notes.
    assert first["card_uid"] == book.card_uid("The Book", "what does a data engineer build?")
