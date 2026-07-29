from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

from .audio import concatenate_wavs, probe_audio
from .config import StoryforgeSettings
from .epub_utils import (
    ChapterEntry,
    BookMetadata,
    clean_html_text,
    extract_book_metadata,
    extract_chapter_html_text,
    extract_cover_image,
    get_chapter_document,
    list_chapters,
    read_epub,
)
from .kokoro_client import KokoroClient
from .m4b import create_m4b
from .manifest import ConversionManifest, load_manifest, save_manifest
from .chunking import split_into_chunks

try:
    import fcntl
except ImportError:  # pragma: no cover - platform specific fallback
    fcntl = None


class BookConversionError(RuntimeError):
    pass


class BookConversionRunner:
    def __init__(self, settings: StoryforgeSettings, client_cls=None) -> None:
        self.settings = settings
        self.client_cls = client_cls or KokoroClient
        self.settings.paths.output_dir.mkdir(parents=True, exist_ok=True)
        self.settings.paths.temp_dir.mkdir(parents=True, exist_ok=True)
        self.settings.paths.log_dir.mkdir(parents=True, exist_ok=True)

    def resume_unfinished_jobs(self) -> list[ConversionManifest]:
        resumed: list[ConversionManifest] = []
        for manifest_path in self.settings.paths.output_dir.glob("*/manifest.json"):
            try:
                manifest = load_manifest(manifest_path)
            except Exception:
                continue
            if manifest.status == "complete":
                continue
            epub_path = Path(manifest.source_epub)
            if epub_path.exists():
                resumed.append(self.run_book(epub_path))
        return resumed

    def run_book(self, epub_path: Path) -> ConversionManifest:
        epub_path = Path(epub_path)
        book = read_epub(epub_path)
        metadata = extract_book_metadata(book)
        cover = extract_cover_image(book)
        chapters = list_chapters(book)
        if not chapters:
            raise BookConversionError(f"No readable chapters were found in {epub_path}")

        output_dir = self.settings.paths.output_dir / _safe_name(metadata.title)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "logs").mkdir(parents=True, exist_ok=True)

        manifest_path = output_dir / "manifest.json"
        if manifest_path.exists():
            manifest = load_manifest(manifest_path)
        else:
            manifest = ConversionManifest.create(
                title=metadata.title,
                author=", ".join(metadata.authors) or "",
                chapters_total=len(chapters),
                voice=self.settings.kokoro.voice,
                speed=self.settings.kokoro.speed,
                output_directory=str(output_dir),
                source_epub=str(epub_path),
            )
            save_manifest(manifest, manifest_path)

        manifest.title = metadata.title
        manifest.author = ", ".join(metadata.authors) or manifest.author
        manifest.chapters_total = len(chapters)
        manifest.voice = self.settings.kokoro.voice
        manifest.speed = self.settings.kokoro.speed
        manifest.output_directory = str(output_dir)
        manifest.source_epub = str(epub_path)
        manifest.status = "running"
        save_manifest(manifest, manifest_path)

        logger = self._build_logger(output_dir / "logs" / "conversion.log")
        logger.info("Startup: epub=%s output=%s", epub_path, output_dir)
        logger.info("Book: %s by %s", metadata.title, manifest.author)
        logger.info("Chapters total: %s", len(chapters))

        client = self.client_cls(
            base_url=self.settings.kokoro.api_url,
            api_key=self.settings.kokoro.api_key,
            model=self.settings.kokoro.model,
            voice=self.settings.kokoro.voice,
            speed=self.settings.kokoro.speed,
            timeout=self.settings.kokoro.timeout_seconds,
            retry_delays=tuple(self.settings.kokoro.retry_delays_seconds),
        )
        client.validate_voice(self.settings.kokoro.voice)

        if cover is not None:
            cover_path = output_dir / "cover.jpg"
            cover_path.write_bytes(cover.data)
        else:
            cover_path = None

        metadata_json = self._metadata_payload(metadata, cover)
        (output_dir / "metadata.json").write_text(_json_dumps(metadata_json), encoding="utf-8")

        chapters_json_path = output_dir / "chapters.json"
        chapter_records: list[dict] = []
        if chapters_json_path.exists():
            try:
                import json

                loaded_records = json.loads(chapters_json_path.read_text(encoding="utf-8"))
                if isinstance(loaded_records, list):
                    chapter_records = loaded_records
            except Exception:
                chapter_records = []
        total_generation_seconds = 0.0
        total_audio_seconds = 0.0
        total_words = 0
        completed_wavs: list[Path] = []
        any_failures = False
        gpu_or_cpu = "unknown"

        for chapter_entry in chapters:
            chapter_number = chapter_entry.number
            chapter_title = chapter_entry.display_title
            chapter_output = output_dir / self.settings.conversion.chapter_filename_format.format(chapter=chapter_number)
            if not manifest.should_process(chapter_number):
                manifest.mark_chapter_skipped(chapter_number, chapter_title, "already completed")
                if not chapter_output.exists():
                    raise BookConversionError(f"Completed chapter is missing from disk: {chapter_output}")
                completed_wavs.append(chapter_output)
                if not any(int(record.get("chapter", -1)) == chapter_number for record in chapter_records):
                    record = next((item for item in manifest.chapter_results if int(item.get("chapter", -1)) == chapter_number), None)
                    if isinstance(record, dict) and record.get("status") == "completed":
                        chapter_records.append(record)
                    else:
                        audio_info = probe_audio(chapter_output)
                        chapter_records.append(
                            {
                                "chapter": chapter_number,
                                "title": chapter_title,
                                "words": 0,
                                "chunks": 0,
                                "duration": _seconds_to_timestamp(float(audio_info["duration"])),
                                "estimated_narration_time": _seconds_to_timestamp(float(audio_info["duration"])),
                                "actual_narration_time": _seconds_to_timestamp(float(audio_info["duration"])),
                                "generation_duration": _seconds_to_timestamp(0),
                                "duration_seconds": float(audio_info["duration"]),
                                "generation_seconds": 0.0,
                                "status": "completed",
                            }
                        )
                logger.info("Skipping already completed chapter %s: %s", chapter_number, chapter_title)
                continue

            if chapter_output.exists():
                chapter_output.unlink()

            logger.info("Chapter start: %s %s", chapter_number, chapter_title)
            started = time.perf_counter()
            try:
                chapter_result = self._render_chapter(
                    book=book,
                    chapter_entry=chapter_entry,
                    client=client,
                    output_path=chapter_output,
                    output_dir=output_dir,
                    logger=logger,
                )
            except Exception as exc:
                any_failures = True
                manifest.mark_chapter_failed(chapter_number, chapter_title, f"{type(exc).__name__}: {exc}")
                save_manifest(manifest, manifest_path)
                logger.exception("Chapter failed: chapter=%s title=%s", chapter_number, chapter_title)
                continue

            duration = time.perf_counter() - started
            total_generation_seconds += duration
            total_audio_seconds += chapter_result["duration_seconds"]
            total_words += chapter_result["words"]
            completed_wavs.append(chapter_output)
            manifest.mark_chapter_completed(
                chapter_number,
                chapter_title,
                chapter_result["words"],
                chapter_result["chunks"],
                chapter_result["duration_seconds"],
                duration,
            )
            chapter_records.append(
                {
                    "chapter": chapter_number,
                    "title": chapter_title,
                    "words": chapter_result["words"],
                    "chunks": chapter_result["chunks"],
                    "duration": _seconds_to_timestamp(chapter_result["duration_seconds"]),
                    "estimated_narration_time": _seconds_to_timestamp(chapter_result["duration_seconds"]),
                    "actual_narration_time": _seconds_to_timestamp(chapter_result["duration_seconds"]),
                    "generation_duration": _seconds_to_timestamp(duration),
                    "duration_seconds": chapter_result["duration_seconds"],
                    "generation_seconds": duration,
                    "status": "completed",
                }
            )
            manifest.update_rollups(total_generation_seconds, total_audio_seconds, total_words, gpu_or_cpu)
            save_manifest(manifest, manifest_path)
            (output_dir / "chapters.json").write_text(_json_dumps(chapter_records), encoding="utf-8")
            logger.info(
                "Chapter complete: %s/%s overall=%.0f%% elapsed=%s estimate_remaining=%s",
                chapter_number,
                len(chapters),
                (len(manifest.completed_chapters) / len(chapters)) * 100.0,
                _seconds_to_timestamp(total_generation_seconds),
                _seconds_to_timestamp(max(0.0, total_generation_seconds / max(1, len(manifest.completed_chapters)) * (len(chapters) - len(manifest.completed_chapters)))),
            )

        manifest.chapters_completed = len(manifest.completed_chapters)
        manifest.finalize()
        manifest.update_rollups(total_generation_seconds, total_audio_seconds, total_words, gpu_or_cpu)
        save_manifest(manifest, manifest_path)
        (output_dir / "chapters.json").write_text(_json_dumps(chapter_records), encoding="utf-8")

        if manifest.status == "complete":
            m4b_path = output_dir / f"{_safe_name(metadata.title)}.m4b"
            logger.info("M4B creation start: %s", m4b_path)
            create_m4b(
                completed_wavs,
                m4b_path,
                metadata={
                    "title": metadata.title,
                    "artist": ", ".join(metadata.authors) or metadata.author if hasattr(metadata, "author") else ", ".join(metadata.authors),
                    "album": metadata.title,
                    "date": metadata.publication_date,
                    "language": metadata.language,
                    "description": metadata.description,
                },
                chapters=chapter_records,
                cover_path=output_dir / "cover.jpg" if cover is not None else None,
                bitrate=self.settings.conversion.m4b_bitrate,
            )
            manifest.m4b_path = str(m4b_path)
            save_manifest(manifest, manifest_path)
            logger.info("M4B creation complete: %s", m4b_path)

        logger.info("Final status: %s", manifest.status)
        logger.info("Chapters completed: %s", manifest.completed_chapters)
        logger.info("Chapters failed: %s", manifest.failed_chapters)
        logger.info("Chapters skipped: %s", manifest.skipped_chapters)
        return manifest

    def _render_chapter(
        self,
        book,
        chapter_entry: ChapterEntry,
        client: KokoroClient,
        output_path: Path,
        output_dir: Path,
        logger: logging.Logger,
    ) -> dict:
        chapter, item = get_chapter_document(book, chapter_entry.number, list_chapters(book))
        html_text = extract_chapter_html_text(item.get_content())
        words = len(html_text.split())
        chunks = split_into_chunks(html_text, max_chars=self.settings.conversion.chunk_chars)
        if not chunks:
            raise BookConversionError(f"Chapter {chapter_entry.number} contained no narratable text")

        temp_dir = Path(tempfile.mkdtemp(prefix=f"chapter-{chapter_entry.number:03d}-", dir=self.settings.paths.temp_dir))
        chunk_paths: list[Path] = []
        try:
            logger.info("Chunk count: %s", len(chunks))
            for index, chunk in enumerate(chunks, start=1):
                logger.info("Chunk start: chapter=%s chunk=%s/%s", chapter_entry.number, index, len(chunks))
                chunk_path = temp_dir / f"chunk-{index:03d}.wav"
                self._synthesize_chunk(client, chunk, chunk_path, logger, chapter_entry.number, index)
                chunk_paths.append(chunk_path)
                logger.info("Chunk finish: chapter=%s chunk=%s/%s", chapter_entry.number, index, len(chunks))

            concatenate_wavs(chunk_paths, output_path)
            audio_info = probe_audio(output_path)
            logger.info("Chapter audio verified: %s", audio_info)
            return {
                "words": words,
                "chunks": len(chunks),
                "duration_seconds": float(audio_info["duration"]),
            }
        except Exception:
            if output_path.exists():
                output_path.unlink()
            raise
        finally:
            if self.settings.conversion.cleanup_temp_on_success:
                shutil.rmtree(temp_dir, ignore_errors=True)

    def _synthesize_chunk(
        self,
        client: KokoroClient,
        text: str,
        output_path: Path,
        logger: logging.Logger,
        chapter: int,
        chunk_index: int,
    ) -> None:
        client.synthesize(text, output_path)

    def _build_logger(self, log_file: Path) -> logging.Logger:
        logger = logging.getLogger(f"storyforge.{log_file.stem}.{id(log_file)}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        logger.propagate = False

        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        return logger

    def _metadata_payload(self, metadata: BookMetadata, cover) -> dict:
        return {
            "title": metadata.title,
            "author": ", ".join(metadata.authors),
            "series": metadata.series,
            "publisher": metadata.publisher,
            "language": metadata.language,
            "description": metadata.description,
            "publication_date": metadata.publication_date,
            "cover_image": getattr(cover, "filename", "") if cover else "",
        }


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {" ", "-", "_", "."} else "_" for ch in value).strip()
    return safe or "Storyforge Book"


def _seconds_to_timestamp(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _json_dumps(data) -> str:
    import json

    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
