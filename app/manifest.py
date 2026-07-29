from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass
class ChapterResult:
    chapter: int
    title: str
    words: int
    chunks: int
    estimated_narration_seconds: float
    actual_narration_seconds: float
    generation_seconds: float
    status: str = "completed"
    error: str = ""


@dataclass
class ConversionManifest:
    title: str
    author: str
    chapters_total: int
    chapters_completed: int
    status: str
    voice: str
    speed: float
    started: str
    updated: str
    output_directory: str
    source_epub: str
    completed_chapters: list[int] = field(default_factory=list)
    skipped_chapters: list[int] = field(default_factory=list)
    failed_chapters: list[int] = field(default_factory=list)
    chapter_results: list[dict[str, Any]] = field(default_factory=list)
    m4b_path: str = ""
    total_generation_seconds: float = 0.0
    total_audio_seconds: float = 0.0
    average_seconds_per_chapter: float = 0.0
    average_seconds_per_1000_words: float = 0.0
    gpu_or_cpu: str = "unknown"

    @classmethod
    def create(
        cls,
        title: str,
        author: str,
        chapters_total: int,
        voice: str,
        speed: float,
        output_directory: str,
        source_epub: str,
        status: str = "running",
    ) -> "ConversionManifest":
        now = _now()
        return cls(
            title=title,
            author=author,
            chapters_total=chapters_total,
            chapters_completed=0,
            status=status,
            voice=voice,
            speed=speed,
            started=now,
            updated=now,
            output_directory=output_directory,
            source_epub=source_epub,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversionManifest":
        return cls(
            title=str(data.get("title", "")),
            author=str(data.get("author", "")),
            chapters_total=int(data.get("chapters_total", 0)),
            chapters_completed=int(data.get("chapters_completed", 0)),
            status=str(data.get("status", "running")),
            voice=str(data.get("voice", "af_bella")),
            speed=float(data.get("speed", 1.0)),
            started=str(data.get("started", _now())),
            updated=str(data.get("updated", _now())),
            output_directory=str(data.get("output_directory", "")),
            source_epub=str(data.get("source_epub", "")),
            completed_chapters=[int(v) for v in data.get("completed_chapters", [])],
            skipped_chapters=[int(v) for v in data.get("skipped_chapters", [])],
            failed_chapters=[int(v) for v in data.get("failed_chapters", [])],
            chapter_results=list(data.get("chapter_results", [])),
            m4b_path=str(data.get("m4b_path", "")),
            total_generation_seconds=float(data.get("total_generation_seconds", 0.0)),
            total_audio_seconds=float(data.get("total_audio_seconds", 0.0)),
            average_seconds_per_chapter=float(data.get("average_seconds_per_chapter", 0.0)),
            average_seconds_per_1000_words=float(data.get("average_seconds_per_1000_words", 0.0)),
            gpu_or_cpu=str(data.get("gpu_or_cpu", "unknown")),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["completed_chapters"] = list(self.completed_chapters)
        data["skipped_chapters"] = list(self.skipped_chapters)
        data["failed_chapters"] = list(self.failed_chapters)
        data["chapter_results"] = list(self.chapter_results)
        return data

    def mark_chapter_completed(
        self,
        chapter: int,
        title: str,
        words: int,
        chunks: int,
        duration_seconds: float,
        generation_seconds: float,
    ) -> None:
        if chapter not in self.completed_chapters:
            self.completed_chapters.append(chapter)
        self.failed_chapters = [item for item in self.failed_chapters if item != chapter]
        self.chapters_completed = len(self.completed_chapters)
        self.updated = _now()
        self.chapter_results = [result for result in self.chapter_results if int(result.get("chapter", -1)) != chapter]
        self.chapter_results.append(
            ChapterResult(
                chapter=chapter,
                title=title,
                words=words,
                chunks=chunks,
                estimated_narration_seconds=duration_seconds,
                actual_narration_seconds=duration_seconds,
                generation_seconds=generation_seconds,
            ).__dict__
        )

    def mark_chapter_failed(self, chapter: int, title: str, error: str, retry_count: int = 0) -> None:
        if chapter not in self.failed_chapters:
            self.failed_chapters.append(chapter)
        self.chapter_results = [result for result in self.chapter_results if int(result.get("chapter", -1)) != chapter]
        self.chapter_results.append(
            {
                "chapter": chapter,
                "title": title,
                "status": "failed",
                "error": error,
                "retry_count": retry_count,
            }
        )
        self.updated = _now()

    def mark_chapter_skipped(self, chapter: int, title: str, reason: str) -> None:
        if chapter not in self.skipped_chapters:
            self.skipped_chapters.append(chapter)
        self.chapter_results.append(
            {
                "chapter": chapter,
                "title": title,
                "status": "skipped",
                "reason": reason,
            }
        )
        self.updated = _now()

    def finalize(self) -> None:
        if self.chapters_completed >= self.chapters_total and not self.failed_chapters:
            self.status = "complete"
        elif self.failed_chapters:
            self.status = "failed"
        else:
            self.status = "running"
        self.chapters_completed = len(self.completed_chapters)
        self.updated = _now()

    def should_process(self, chapter: int) -> bool:
        return chapter not in self.completed_chapters

    def update_rollups(self, total_generation_seconds: float, total_audio_seconds: float, total_words: int, gpu_or_cpu: str) -> None:
        self.total_generation_seconds = total_generation_seconds
        self.total_audio_seconds = total_audio_seconds
        self.gpu_or_cpu = gpu_or_cpu
        if self.chapters_completed:
            self.average_seconds_per_chapter = total_generation_seconds / self.chapters_completed
        if total_words:
            self.average_seconds_per_1000_words = total_generation_seconds / (total_words / 1000.0)
        self.updated = _now()


def load_manifest(path: str | Path) -> ConversionManifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return ConversionManifest.from_dict(data)


def save_manifest(manifest: ConversionManifest, path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
