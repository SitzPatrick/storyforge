from __future__ import annotations

from app.packaging.metadata import build_book_metadata_hash, normalize_book_metadata
from app.packaging.models import BookMetadata
from app.packaging.timeline import build_chapter_timeline


def test_normalize_book_metadata_trims_and_normalizes_unicode():
    metadata = BookMetadata(
        title="  Café ",
        author=" Patrick Sitz ",
        narrator="  Narrator  ",
        language=" en ",
        subtitle="  An – Audio Book  ",
        series="  Series One  ",
        series_position=" 2 ",
        publisher="  Example Press  ",
        publication_year=" 2024 ",
        description="  Line one\r\nLine two  ",
        copyright="  © 2024 Example  ",
        genre="  Fiction  ",
        identifier=" 9781234567890 ",
        comment="  note  ",
    )

    normalized = normalize_book_metadata(metadata)

    assert normalized.title == "Café"
    assert normalized.author == "Patrick Sitz"
    assert normalized.language == "en"
    assert normalized.description == "Line one\nLine two"
    assert normalized.series_position == 2
    assert normalized.publication_year == 2024
    assert normalized.identifier == "9781234567890"

    assert build_book_metadata_hash(metadata) == build_book_metadata_hash(normalized)


def test_normalize_book_metadata_rejects_control_characters():
    metadata = BookMetadata(title="Bad\x07Title")

    try:
        normalize_book_metadata(metadata)
    except ValueError as exc:
        assert "control character" in str(exc)
    else:
        raise AssertionError("expected control character validation to fail")


def test_build_chapter_timeline_is_canonical_and_drift_free():
    chapters = [
        {"book_id": "book-1", "chapter_id": "c2", "chapter_order": 2, "chapter_title": "Second", "mastered_chapter_id": "m2", "duration_seconds": 1.234567},
        {"book_id": "book-1", "chapter_id": "c1", "chapter_order": 1, "chapter_title": "First", "mastered_chapter_id": "m1", "duration_seconds": 2.000001},
        {"book_id": "book-1", "chapter_id": "c3", "chapter_order": 3, "chapter_title": "Third", "mastered_chapter_id": "m3", "duration_seconds": 3.499999},
    ]

    timeline = build_chapter_timeline(chapters, timebase=1_000_000)

    assert [entry.chapter_id for entry in timeline] == ["c1", "c2", "c3"]
    assert [entry.start_time for entry in timeline] == [0, 2_000_001, 3_234_568]
    assert [entry.end_time for entry in timeline] == [2_000_001, 3_234_568, 6_734_567]
    assert [entry.duration_ticks for entry in timeline] == [2_000_001, 1_234_567, 3_499_999]

    assert timeline[0].timebase == 1_000_000
    assert timeline[1].book_id == "book-1"
