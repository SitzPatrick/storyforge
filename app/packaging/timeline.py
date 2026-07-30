from __future__ import annotations

from typing import Any, Mapping, Sequence

from .models import ChapterTimelineEntry


def build_chapter_timeline(chapters: Sequence[Mapping[str, Any]], *, timebase: int = 1_000_000, rounding: str = "round") -> list[ChapterTimelineEntry]:
    if timebase <= 0:
        raise ValueError("timebase must be positive")
    if rounding not in {"round", "floor"}:
        raise ValueError("unsupported rounding rule")

    ordered = sorted((_coerce_chapter_record(chapter) for chapter in chapters), key=lambda item: (item["chapter_order"], item["chapter_id"], item["mastered_chapter_id"]))
    for left, right in zip(ordered, ordered[1:]):
        if left["chapter_order"] == right["chapter_order"]:
            raise ValueError(f"duplicate chapter order: {left['chapter_order']}")

    timeline: list[ChapterTimelineEntry] = []
    current_ticks = 0
    for record in ordered:
        duration_ticks = _duration_to_ticks(record["duration_seconds"], timebase=timebase, rounding=rounding)
        entry = ChapterTimelineEntry(
            book_id=record["book_id"],
            chapter_id=record["chapter_id"],
            chapter_order=record["chapter_order"],
            chapter_title=record.get("chapter_title"),
            mastered_chapter_id=record["mastered_chapter_id"],
            start_time=current_ticks,
            end_time=current_ticks + duration_ticks,
            duration_ticks=duration_ticks,
            timebase=timebase,
            optional=bool(record.get("optional", False)),
        )
        timeline.append(entry)
        current_ticks = entry.end_time
    return timeline


def _coerce_chapter_record(chapter: Mapping[str, Any]) -> dict[str, Any]:
    required = ("book_id", "chapter_id", "chapter_order", "mastered_chapter_id", "duration_seconds")
    missing = [field for field in required if field not in chapter]
    if missing:
        raise ValueError(f"chapter record missing required fields: {', '.join(sorted(missing))}")
    duration_seconds = float(chapter["duration_seconds"])
    if duration_seconds <= 0:
        raise ValueError("chapter duration must be positive")
    return {
        "book_id": str(chapter["book_id"]),
        "chapter_id": str(chapter["chapter_id"]),
        "chapter_order": int(chapter["chapter_order"]),
        "chapter_title": chapter.get("chapter_title"),
        "mastered_chapter_id": str(chapter["mastered_chapter_id"]),
        "duration_seconds": duration_seconds,
        "optional": bool(chapter.get("optional", False)),
    }


def _duration_to_ticks(seconds: float, *, timebase: int, rounding: str) -> int:
    raw = seconds * timebase
    if rounding == "floor":
        ticks = int(raw // 1)
    else:
        ticks = int(round(raw))
    return max(1, ticks)
