from __future__ import annotations

import hashlib
import imghdr
from pathlib import Path
from typing import Iterable

from app.mastering.cache import load_mastering_sidecar
from app.mastering.measure import measure_audio

from .models import (
    BookMetadata,
    ChapterTimelineEntry,
    CoverArtInput,
    MasteredChapterInput,
    PackagingCompletionStatus,
    PackagingConfig,
    PackagingFailure,
    PackagingFailureType,
    PackagingValidationStatus,
)


class PackagingValidationError(RuntimeError):
    pass


def validate_mastered_chapter_inputs(
    chapters: Iterable[MasteredChapterInput],
    config: PackagingConfig,
) -> tuple[tuple[MasteredChapterInput, ...], tuple[str, ...], tuple[PackagingFailure, ...]]:
    validated: list[MasteredChapterInput] = []
    warnings: list[str] = []
    failures: list[PackagingFailure] = []
    for chapter in chapters:
        if not chapter.mastered_audio_path.exists():
            if chapter.required:
                failures.append(
                    PackagingFailure(
                        PackagingFailureType.SOURCE_CHAPTER_MISSING,
                        f"mastered audio missing: {chapter.mastered_audio_path}",
                        chapter_id=chapter.chapter_id,
                    )
                )
            else:
                warnings.append(f"optional chapter omitted: {chapter.chapter_id}")
            continue
        if not chapter.mastered_sidecar_path.exists():
            failures.append(
                PackagingFailure(
                    PackagingFailureType.SOURCE_SIDECAR_MISSING,
                    f"mastering sidecar missing: {chapter.mastered_sidecar_path}",
                    chapter_id=chapter.chapter_id,
                )
            )
            continue
        try:
            sidecar = load_mastering_sidecar(chapter.mastered_sidecar_path)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                PackagingFailure(
                    PackagingFailureType.SOURCE_SIDECAR_CORRUPT,
                    str(exc),
                    chapter_id=chapter.chapter_id,
                    backend_diagnostic_excerpt=str(exc),
                )
            )
            continue
        if sidecar.book_id != chapter.book_id:
            failures.append(
                PackagingFailure(
                    PackagingFailureType.SOURCE_HASH_MISMATCH,
                    "book id mismatch",
                    chapter_id=chapter.chapter_id,
                    details={"expected_book_id": chapter.book_id, "actual_book_id": sidecar.book_id},
                )
            )
            continue
        if sidecar.chapter_id != chapter.chapter_id or sidecar.mastered_chapter_id != chapter.mastered_chapter_id:
            failures.append(
                PackagingFailure(
                    PackagingFailureType.SOURCE_HASH_MISMATCH,
                    "chapter identity mismatch",
                    chapter_id=chapter.chapter_id,
                )
            )
            continue
        if sidecar.source_chapter_assembly_id != chapter.source_chapter_assembly_id:
            failures.append(
                PackagingFailure(
                    PackagingFailureType.SOURCE_HASH_MISMATCH,
                    "assembly id mismatch",
                    chapter_id=chapter.chapter_id,
                )
            )
            continue
        if sidecar.mastered_audio_content_hash != chapter.mastered_audio_content_hash:
            failures.append(
                PackagingFailure(
                    PackagingFailureType.SOURCE_HASH_MISMATCH,
                    "audio hash mismatch",
                    chapter_id=chapter.chapter_id,
                )
            )
            continue
        if sidecar.validation_result not in config.allowed_mastering_statuses:
            failures.append(
                PackagingFailure(
                    PackagingFailureType.INVALID_MASTERED_AUDIO,
                    f"mastering validation not accepted: {sidecar.validation_result}",
                    chapter_id=chapter.chapter_id,
                )
            )
            continue
        if (sidecar.sample_rate_hz, sidecar.channel_count, sidecar.sample_width_bytes) != (
            config.sample_rate_hz,
            config.channel_count,
            chapter.sample_width_bytes,
        ):
            failures.append(
                PackagingFailure(
                    PackagingFailureType.INCOMPATIBLE_CHAPTER_FORMAT,
                    "unexpected mastered audio format",
                    chapter_id=chapter.chapter_id,
                    details={
                        "sample_rate_hz": sidecar.sample_rate_hz,
                        "channel_count": sidecar.channel_count,
                        "sample_width_bytes": sidecar.sample_width_bytes,
                    },
                )
            )
            continue
        measurements = measure_audio(
            chapter.mastered_audio_path,
            target_sample_rate_hz=config.sample_rate_hz,
            target_channel_count=config.channel_count,
            target_sample_width_bytes=chapter.sample_width_bytes,
        )
        if measurements.audio_content_hash != chapter.mastered_audio_content_hash:
            failures.append(
                PackagingFailure(
                    PackagingFailureType.SOURCE_HASH_MISMATCH,
                    "mastered audio bytes changed",
                    chapter_id=chapter.chapter_id,
                )
            )
            continue
        validated.append(chapter)
    return tuple(validated), tuple(warnings), tuple(failures)


def validate_cover_art_input(
    cover_art: CoverArtInput | None,
    config: PackagingConfig,
) -> tuple[CoverArtInput | None, tuple[str, ...], PackagingFailure | None]:
    if cover_art is None:
        if config.embed_cover_art and config.cover_art_enabled:
            return None, (), PackagingFailure(PackagingFailureType.COVER_ART_MISSING, "cover art required but not provided")
        return None, (), None
    if not cover_art.enabled:
        return None, (), None
    path = cover_art.resolved_source_path
    if path is None or not path.exists() or path.stat().st_size <= 0:
        return None, (), PackagingFailure(PackagingFailureType.COVER_ART_INVALID, f"cover art missing or empty: {path}")
    kind = imghdr.what(path)
    if kind not in {"jpeg", "png"}:
        return None, (), PackagingFailure(PackagingFailureType.COVER_ART_INVALID, f"unsupported cover art type: {kind}")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if cover_art.source_content_hash and cover_art.source_content_hash != digest:
        return None, (), PackagingFailure(PackagingFailureType.COVER_ART_INVALID, "cover art hash mismatch")
    if config.cover_art_format and kind != config.cover_art_format:
        # allow png/jpg tolerance? keep strict to declared format when present
        if config.cover_art_format not in {"jpeg", "jpg", "png"}:
            return None, (), PackagingFailure(PackagingFailureType.COVER_ART_INVALID, "unsupported cover art configuration")
    updated = CoverArtInput(
        enabled=True,
        source_relative_path=cover_art.source_relative_path,
        resolved_source_path=path,
        source_content_hash=digest,
        source_format=kind,
        width=cover_art.width,
        height=cover_art.height,
        conversion_settings=cover_art.conversion_settings,
        expected_embedded=cover_art.expected_embedded,
    )
    return updated, (), None


def validate_backend_probe(
    probe: dict[str, object],
    *,
    config: PackagingConfig,
    expected_chapter_count: int,
    expected_duration_seconds: float,
    expected_cover_art_enabled: bool,
) -> PackagingFailure | None:
    if probe.get("output_container") != config.container_format:
        return PackagingFailure(PackagingFailureType.INVALID_OUTPUT_CONTAINER, "output container mismatch")
    if probe.get("audio_codec") != config.audio_codec:
        return PackagingFailure(PackagingFailureType.OUTPUT_VALIDATION_FAILURE, "audio codec mismatch")
    if int(probe.get("sample_rate_hz", 0) or 0) != config.sample_rate_hz:
        return PackagingFailure(PackagingFailureType.OUTPUT_VALIDATION_FAILURE, "sample rate mismatch")
    if int(probe.get("channel_count", 0) or 0) != config.channel_count:
        return PackagingFailure(PackagingFailureType.OUTPUT_VALIDATION_FAILURE, "channel count mismatch")
    if int(probe.get("chapter_count", 0) or 0) != expected_chapter_count:
        return PackagingFailure(PackagingFailureType.CHAPTER_METADATA_FAILURE, "chapter count mismatch")
    if abs(float(probe.get("duration_seconds", 0.0) or 0.0) - expected_duration_seconds) > 0.5:
        return PackagingFailure(PackagingFailureType.OUTPUT_VALIDATION_FAILURE, "duration mismatch")
    if expected_cover_art_enabled and not probe.get("cover_art_probe_state"):
        return PackagingFailure(PackagingFailureType.OUTPUT_VALIDATION_FAILURE, "cover art missing from output")
    return None
