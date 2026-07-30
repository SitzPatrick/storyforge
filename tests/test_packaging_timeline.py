from __future__ import annotations

from app.packaging.timeline import build_chapter_timeline


def test_build_chapter_timeline_uses_integer_microseconds_without_drift():
    chapters = [
        {"book_id": "book-1", "chapter_id": "c1", "chapter_order": 1, "chapter_title": "One", "mastered_chapter_id": "m1", "duration_seconds": 1.5},
        {"book_id": "book-1", "chapter_id": "c2", "chapter_order": 2, "chapter_title": "Two", "mastered_chapter_id": "m2", "duration_seconds": 2.25},
        {"book_id": "book-1", "chapter_id": "c3", "chapter_order": 3, "chapter_title": "Three", "mastered_chapter_id": "m3", "duration_seconds": 3.0},
    ]

    timeline = build_chapter_timeline(chapters, timebase=1_000_000)

    assert [entry.start_time for entry in timeline] == [0, 1_500_000, 3_750_000]
    assert [entry.end_time for entry in timeline] == [1_500_000, 3_750_000, 6_750_000]
    assert [entry.duration_ticks for entry in timeline] == [1_500_000, 2_250_000, 3_000_000]
    assert [entry.chapter_order for entry in timeline] == [1, 2, 3]


def test_build_chapter_timeline_rejects_duplicate_orders():
    chapters = [
        {"book_id": "book-1", "chapter_id": "c1", "chapter_order": 1, "chapter_title": "One", "mastered_chapter_id": "m1", "duration_seconds": 1.0},
        {"book_id": "book-1", "chapter_id": "c2", "chapter_order": 1, "chapter_title": "Two", "mastered_chapter_id": "m2", "duration_seconds": 1.0},
    ]

    try:
        build_chapter_timeline(chapters, timebase=1_000_000)
    except ValueError as exc:
        assert "duplicate chapter order" in str(exc)
    else:
        raise AssertionError("expected duplicate chapter order rejection")
