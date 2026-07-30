from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from .backends.base import PackagingBackend
from .backends.fake import FakePackagingBackend
from .cache import (
    PACKAGE_REPORT_FILENAME,
    PACKAGE_SIDECAR_FILENAME,
    build_audiobook_package_id,
    build_package_input_hash,
    canonical_package_path,
    canonical_relative_package_path,
    load_package_sidecar,
    package_sidecar_matches,
    save_package_sidecar,
)
from .metadata import build_book_metadata_hash, normalize_book_metadata, normalize_metadata_mapping
from .models import (
    BookMetadata,
    ChapterTimelineEntry,
    CoverArtInput,
    MasteredChapterInput,
    PackageSidecar,
    PackagingBackendResult,
    PackagingCompletionStatus,
    PackagingConfig,
    PackagingFailure,
    PackagingFailureType,
    PackagingReport,
    PackagingRequest,
    PackagingResult,
    PackagingValidationStatus,
)
from .serialization import canonical_json, canonicalize
from .timeline import build_chapter_timeline
from .validation import validate_backend_probe, validate_cover_art_input, validate_mastered_chapter_inputs


class PackagingEngineError(RuntimeError):
    pass


def _coerce_mastered_chapter_input(chapter: Mapping[str, Any] | MasteredChapterInput) -> MasteredChapterInput:
    if isinstance(chapter, MasteredChapterInput):
        return chapter
    payload = dict(chapter)
    if "mastered_audio_path" not in payload and "audio_path" in payload:
        payload["mastered_audio_path"] = payload.pop("audio_path")
    if "mastered_sidecar_path" not in payload and "sidecar_path" in payload:
        payload["mastered_sidecar_path"] = payload.pop("sidecar_path")
    if "source_chapter_path" not in payload and "source_path" in payload:
        payload["source_chapter_path"] = payload.pop("source_path")
    if "mastering_validation_result" not in payload and "validation_result" in payload:
        payload["mastering_validation_result"] = payload.pop("validation_result")
    return MasteredChapterInput(**payload)


def _coerce_book_metadata(metadata: BookMetadata | Mapping[str, Any]) -> BookMetadata:
    if isinstance(metadata, BookMetadata):
        return metadata
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be BookMetadata or mapping")
    return BookMetadata(**dict(metadata))


def _coerce_cover_art(cover_art: CoverArtInput | Mapping[str, Any] | None) -> CoverArtInput | None:
    if cover_art is None or isinstance(cover_art, CoverArtInput):
        return cover_art
    return CoverArtInput(**dict(cover_art))


def _hash_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=path.suffix + ".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(payload))
            handle.write("\n")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def _backup_if_exists(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + ".bak")
    if backup.exists():
        backup.unlink()
    shutil.copy2(path, backup)
    return backup


def _restore_backup(path: Path, backup: Path | None) -> None:
    if backup is None:
        if path.exists():
            path.unlink()
        return
    shutil.copy2(backup, path)


def _cleanup_backup(backup: Path | None) -> None:
    if backup and backup.exists():
        backup.unlink()


def _build_failure_result(
    *,
    book_id: str,
    package_id: str,
    package_input_hash: str,
    output_artifact_path: Path,
    output_artifact_relative_path: str,
    sidecar_path: Path,
    report_path: Path,
    container_format: str,
    audio_codec: str,
    audio_bitrate_kbps: int,
    sample_rate_hz: int,
    channel_count: int,
    backend_name: str,
    backend_version: str,
    encoder_name: str,
    encoder_version: str | None,
    warnings: tuple[str, ...],
    errors: tuple[str, ...],
    failure: PackagingFailure,
    status: PackagingCompletionStatus,
) -> PackagingResult:
    return PackagingResult(
        book_id=book_id,
        package_id=package_id,
        package_input_hash=package_input_hash,
        output_artifact_path=output_artifact_path,
        output_artifact_relative_path=output_artifact_relative_path,
        sidecar_path=sidecar_path,
        report_path=report_path,
        container_format=container_format,
        audio_codec=audio_codec,
        audio_bitrate_kbps=audio_bitrate_kbps,
        sample_rate_hz=sample_rate_hz,
        channel_count=channel_count,
        duration_seconds=0.0,
        chapter_count=0,
        chapter_probe_data=(),
        metadata_probe_data={},
        cover_art_probe_state=None,
        backend_name=backend_name,
        backend_version=backend_version,
        encoder_name=encoder_name,
        encoder_version=encoder_version,
        file_size=0,
        warnings=warnings,
        errors=errors,
        status=status,
        cache_hit=False,
        newly_created=False,
        failure=failure,
    )


def _cache_hit_result(
    *,
    sidecar: PackageSidecar,
    output_path: Path,
    report_path: Path,
    backend_result: PackagingBackendResult,
) -> PackagingResult:
    validation_text = sidecar.validation_result.value if isinstance(sidecar.validation_result, PackagingValidationStatus) else str(sidecar.validation_result)
    report = PackagingReport(
        book_id=sidecar.book_id,
        package_id=sidecar.audiobook_package_id,
        completion_status=PackagingCompletionStatus.COMPLETE if validation_text == PackagingValidationStatus.PASSED.value else PackagingCompletionStatus.COMPLETE_WITH_WARNINGS,
        package_cache_hit=True,
        package_newly_created=False,
        chapters_expected=sidecar.chapter_count,
        chapters_packaged=sidecar.chapter_count,
        optional_chapters_omitted=(),
        blocked_chapters=(),
        expected_duration_seconds=sidecar.total_duration_seconds,
        actual_duration_seconds=sidecar.total_duration_seconds,
        duration_delta_seconds=0.0,
        metadata_validation_status=PackagingValidationStatus.PASSED,
        cover_art_status=PackagingValidationStatus.PASSED if sidecar.cover_art_embedded else PackagingValidationStatus.BLOCKED,
        backend_name=sidecar.backend_name,
        backend_version=sidecar.backend_version,
        encoder_name=sidecar.encoder_name,
        encoder_version=sidecar.encoder_version,
        output_artifact_relative_path=sidecar.output_artifact_relative_path,
        output_file_size=sidecar.file_size,
        warnings=sidecar.warnings,
        errors=sidecar.errors,
        package_input_hash=sidecar.package_input_hash,
        sidecar_relative_path=PACKAGE_SIDECAR_FILENAME,
    )
    return PackagingResult(
        book_id=sidecar.book_id,
        package_id=sidecar.audiobook_package_id,
        package_input_hash=sidecar.package_input_hash,
        output_artifact_path=output_path,
        output_artifact_relative_path=sidecar.output_artifact_relative_path,
        sidecar_path=output_path.with_name(PACKAGE_SIDECAR_FILENAME),
        report_path=report_path,
        container_format=sidecar.output_container,
        audio_codec=sidecar.audio_codec,
        audio_bitrate_kbps=sidecar.audio_bitrate_kbps,
        sample_rate_hz=sidecar.sample_rate_hz,
        channel_count=sidecar.channel_count,
        duration_seconds=sidecar.total_duration_seconds,
        chapter_count=sidecar.chapter_count,
        chapter_probe_data=backend_result.chapter_probe_data,
        metadata_probe_data=backend_result.metadata_probe_data,
        cover_art_probe_state=backend_result.cover_art_probe_state,
        backend_name=sidecar.backend_name,
        backend_version=sidecar.backend_version,
        encoder_name=sidecar.encoder_name,
        encoder_version=sidecar.encoder_version,
        file_size=sidecar.file_size,
        warnings=sidecar.warnings,
        errors=sidecar.errors,
        status=PackagingCompletionStatus.COMPLETE if sidecar.validation_result == PackagingValidationStatus.PASSED else PackagingCompletionStatus.COMPLETE_WITH_WARNINGS,
        cache_hit=True,
        newly_created=False,
        backend_result=backend_result,
        report=report,
        sidecar=sidecar,
        package_metadata_hash=sidecar.canonical_book_metadata_hash,
        output_artifact_content_hash=sidecar.output_artifact_content_hash,
    )


def package_audiobook(
    chapters: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
    *,
    metadata: BookMetadata | Mapping[str, Any],
    config: PackagingConfig,
    backend: PackagingBackend | None = None,
    cover_art: CoverArtInput | Mapping[str, Any] | None = None,
) -> PackagingResult:
    backend = backend or FakePackagingBackend()
    if not backend.is_available():
        book_id = "unknown-book"
        package_id = build_audiobook_package_id(book_id=book_id, packaging_contract_version=config.packaging_contract_version, container_format=config.container_format)
        output_path = canonical_package_path(package_root=config.package_root, book_id=book_id, package_id=package_id, container_format=config.container_format)
        failure = PackagingFailure(PackagingFailureType.PACKAGING_BACKEND_UNAVAILABLE, f"packaging backend unavailable: {backend.backend_name}")
        return _build_failure_result(
            book_id=book_id,
            package_id=package_id,
            package_input_hash="",
            output_artifact_path=output_path,
            output_artifact_relative_path=canonical_relative_package_path(book_id=book_id, package_id=package_id, container_format=config.container_format),
            sidecar_path=output_path.with_name(PACKAGE_SIDECAR_FILENAME),
            report_path=output_path.with_name(PACKAGE_REPORT_FILENAME),
            container_format=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
            encoder_name=backend.encoder_name,
            encoder_version=backend.encoder_version,
            warnings=(),
            errors=(failure.message,),
            failure=failure,
            status=PackagingCompletionStatus.BLOCKED,
        )
    normalized_metadata = normalize_book_metadata(_coerce_book_metadata(metadata))
    normalized_cover_art, cover_warnings, cover_failure = validate_cover_art_input(_coerce_cover_art(cover_art), config)
    mastered_inputs = tuple(_coerce_mastered_chapter_input(chapter) for chapter in chapters)
    validated_chapters, chapter_warnings, chapter_failures = validate_mastered_chapter_inputs(mastered_inputs, config)

    book_id = validated_chapters[0].book_id if validated_chapters else (mastered_inputs[0].book_id if mastered_inputs else "unknown-book")
    if chapter_failures:
        failure = chapter_failures[0]
        package_id = build_audiobook_package_id(book_id=book_id, packaging_contract_version=config.packaging_contract_version, container_format=config.container_format)
        output_path = canonical_package_path(package_root=config.package_root, book_id=book_id, package_id=package_id, container_format=config.container_format)
        return _build_failure_result(
            book_id=book_id,
            package_id=package_id,
            package_input_hash="",
            output_artifact_path=output_path,
            output_artifact_relative_path=canonical_relative_package_path(book_id=book_id, package_id=package_id, container_format=config.container_format),
            sidecar_path=output_path.with_name(PACKAGE_SIDECAR_FILENAME),
            report_path=output_path.with_name(PACKAGE_REPORT_FILENAME),
            container_format=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
            encoder_name=backend.encoder_name,
            encoder_version=backend.encoder_version,
            warnings=chapter_warnings + cover_warnings,
            errors=(failure.message,),
            failure=failure,
            status=PackagingCompletionStatus.BLOCKED,
        )
    if cover_failure is not None:
        package_id = build_audiobook_package_id(book_id=book_id, packaging_contract_version=config.packaging_contract_version, container_format=config.container_format)
        output_path = canonical_package_path(package_root=config.package_root, book_id=book_id, package_id=package_id, container_format=config.container_format)
        return _build_failure_result(
            book_id=book_id,
            package_id=package_id,
            package_input_hash="",
            output_artifact_path=output_path,
            output_artifact_relative_path=canonical_relative_package_path(book_id=book_id, package_id=package_id, container_format=config.container_format),
            sidecar_path=output_path.with_name(PACKAGE_SIDECAR_FILENAME),
            report_path=output_path.with_name(PACKAGE_REPORT_FILENAME),
            container_format=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
            encoder_name=backend.encoder_name,
            encoder_version=backend.encoder_version,
            warnings=tuple(chapter_warnings) + cover_warnings,
            errors=(cover_failure.message,),
            failure=cover_failure,
            status=PackagingCompletionStatus.BLOCKED,
        )

    ordered_chapters = tuple(sorted(validated_chapters, key=lambda item: (item.chapter_order, item.chapter_id, item.mastered_chapter_id)))
    timeline = tuple(
        build_chapter_timeline(
            [
                {
                    "book_id": chapter.book_id,
                    "chapter_id": chapter.chapter_id,
                    "chapter_order": chapter.chapter_order,
                    "chapter_title": chapter.chapter_title,
                    "mastered_chapter_id": chapter.mastered_chapter_id,
                    "duration_seconds": chapter.duration_seconds,
                }
                for chapter in ordered_chapters
            ],
            timebase=config.chapter_timebase,
            rounding=config.chapter_rounding,
        )
    )

    normalized_metadata_hash = build_book_metadata_hash(normalized_metadata)
    package_id = build_audiobook_package_id(book_id=book_id, packaging_contract_version=config.packaging_contract_version, container_format=config.container_format)
    output_path = canonical_package_path(package_root=config.package_root, book_id=book_id, package_id=package_id, container_format=config.container_format)
    temp_output_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if not config.package_root.is_absolute() or any(part == ".." for part in config.package_root.parts):
        failure = PackagingFailure(PackagingFailureType.UNSAFE_OUTPUT_PATH, f"unsafe package root: {config.package_root}")
        return _build_failure_result(
            book_id=book_id,
            package_id=package_id,
            package_input_hash="",
            output_artifact_path=output_path,
            output_artifact_relative_path=canonical_relative_package_path(book_id=book_id, package_id=package_id, container_format=config.container_format),
            sidecar_path=output_path.with_name(PACKAGE_SIDECAR_FILENAME),
            report_path=output_path.with_name(PACKAGE_REPORT_FILENAME),
            container_format=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
            encoder_name=backend.encoder_name,
            encoder_version=backend.encoder_version,
            warnings=tuple(chapter_warnings) + cover_warnings,
            errors=(failure.message,),
            failure=failure,
            status=PackagingCompletionStatus.BLOCKED,
        )
    sidecar_path = output_path.with_name(PACKAGE_SIDECAR_FILENAME)
    report_path = output_path.with_name(PACKAGE_REPORT_FILENAME)
    output_relative_path = canonical_relative_package_path(book_id=book_id, package_id=package_id, container_format=config.container_format)

    request = PackagingRequest(
        book_id=book_id,
        package_id=package_id,
        package_input_hash="",
        output_path=output_path,
        temp_output_path=temp_output_path,
        output_artifact_relative_path=output_relative_path,
        normalized_metadata=normalized_metadata,
        normalized_metadata_hash=normalized_metadata_hash,
        chapter_inputs=ordered_chapters,
        chapter_timeline=timeline,
        cover_art=normalized_cover_art,
        config=config,
    )

    package_input_hash = build_package_input_hash(
        request,
        backend_name=backend.backend_name,
        backend_version=backend.backend_version,
        encoder_name=backend.encoder_name,
        encoder_version=backend.encoder_version,
        cover_art_hash=None if normalized_cover_art is None else normalized_cover_art.source_content_hash,
    )
    request = replace(request, package_input_hash=package_input_hash)

    cache_hit = _maybe_cache_hit(
        output_path=output_path,
        sidecar_path=sidecar_path,
        report_path=report_path,
        backend=backend,
        config=config,
        expected_package_id=package_id,
        expected_package_input_hash=package_input_hash,
        expected_book_id=book_id,
        expected_output_relative_path=output_relative_path,
        expected_metadata_hash=normalized_metadata_hash,
        expected_cover_art_hash=None if normalized_cover_art is None else normalized_cover_art.source_content_hash,
        expected_cover_art_embedded=bool(normalized_cover_art and normalized_cover_art.expected_embedded),
        expected_chapter_timeline=timeline,
    )
    if cache_hit is not None:
        return cache_hit

    backup_output = _backup_if_exists(output_path)
    backup_sidecar = _backup_if_exists(sidecar_path)
    backup_report = _backup_if_exists(report_path)
    try:
        backend_result = backend.package(request)
        probe = backend.probe(backend_result.output_path)
        validation_failure = validate_backend_probe(
            probe,
            config=config,
            expected_chapter_count=len(ordered_chapters),
            expected_duration_seconds=sum(chapter.duration_seconds for chapter in ordered_chapters),
            expected_cover_art_enabled=bool(normalized_cover_art and normalized_cover_art.expected_embedded),
        )
        if validation_failure is not None:
            raise validation_failure
        if _hash_path(backend_result.output_path) != backend_result.audio_content_hash:
            raise PackagingFailure(
                PackagingFailureType.OUTPUT_VALIDATION_FAILURE,
                "backend output hash mismatch",
                backend_diagnostic_excerpt="output hash mismatch",
            )
        if backend_result.output_path != output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backend_result.output_path, output_path)
        else:
            output_path = backend_result.output_path
        sidecar = PackageSidecar(
            audiobook_package_id=package_id,
            book_id=book_id,
            packaging_contract_version=config.packaging_contract_version,
            packager_version=config.packager_version,
            backend_name=backend_result.backend_name,
            backend_version=backend_result.backend_version,
            encoder_name=backend_result.encoder_name,
            encoder_version=backend_result.encoder_version,
            package_input_hash=package_input_hash,
            output_artifact_relative_path=output_relative_path,
            output_container=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            total_duration_seconds=backend_result.duration_seconds,
            chapter_count=len(ordered_chapters),
            ordered_chapter_ids=tuple(chapter.chapter_id for chapter in ordered_chapters),
            ordered_mastered_chapter_ids=tuple(chapter.mastered_chapter_id for chapter in ordered_chapters),
            ordered_mastered_audio_content_hashes=tuple(chapter.mastered_audio_content_hash for chapter in ordered_chapters),
            chapter_timeline=timeline,
            canonical_book_metadata_hash=normalized_metadata_hash,
            cover_art_hash=None if normalized_cover_art is None else normalized_cover_art.source_content_hash,
            cover_art_embedded=bool(normalized_cover_art and normalized_cover_art.expected_embedded),
            output_artifact_content_hash=_hash_path(output_path),
            file_size=output_path.stat().st_size,
            validation_result=PackagingValidationStatus.PASSED if not backend_result.warnings else PackagingValidationStatus.PASSED_WITH_WARNINGS,
            warnings=tuple(chapter_warnings) + cover_warnings + backend_result.warnings,
            errors=backend_result.errors,
        )
        save_package_sidecar(sidecar_path, sidecar)
        report = PackagingReport(
            book_id=book_id,
            package_id=package_id,
            completion_status=PackagingCompletionStatus.COMPLETE if sidecar.validation_result == PackagingValidationStatus.PASSED else PackagingCompletionStatus.COMPLETE_WITH_WARNINGS,
            package_cache_hit=False,
            package_newly_created=True,
            chapters_expected=len(ordered_chapters),
            chapters_packaged=len(ordered_chapters),
            optional_chapters_omitted=tuple(chapter.chapter_id for chapter in mastered_inputs if not chapter.required and chapter.chapter_id not in {item.chapter_id for item in ordered_chapters}),
            blocked_chapters=tuple(),
            expected_duration_seconds=sum(chapter.duration_seconds for chapter in ordered_chapters),
            actual_duration_seconds=backend_result.duration_seconds,
            duration_delta_seconds=backend_result.duration_seconds - sum(chapter.duration_seconds for chapter in ordered_chapters),
            metadata_validation_status=PackagingValidationStatus.PASSED,
            cover_art_status=PackagingValidationStatus.PASSED if normalized_cover_art and normalized_cover_art.expected_embedded else PackagingValidationStatus.BLOCKED,
            backend_name=backend_result.backend_name,
            backend_version=backend_result.backend_version,
            encoder_name=backend_result.encoder_name,
            encoder_version=backend_result.encoder_version,
            output_artifact_relative_path=output_relative_path,
            output_file_size=output_path.stat().st_size,
            warnings=sidecar.warnings,
            errors=sidecar.errors,
            package_input_hash=package_input_hash,
            sidecar_relative_path=PACKAGE_SIDECAR_FILENAME,
        )
        _atomic_write_json(report_path, report)
        return PackagingResult(
            book_id=book_id,
            package_id=package_id,
            package_input_hash=package_input_hash,
            output_artifact_path=output_path,
            output_artifact_relative_path=output_relative_path,
            sidecar_path=sidecar_path,
            report_path=report_path,
            container_format=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            duration_seconds=backend_result.duration_seconds,
            chapter_count=len(ordered_chapters),
            chapter_probe_data=backend_result.chapter_probe_data,
            metadata_probe_data=backend_result.metadata_probe_data,
            cover_art_probe_state=backend_result.cover_art_probe_state,
            backend_name=backend_result.backend_name,
            backend_version=backend_result.backend_version,
            encoder_name=backend_result.encoder_name,
            encoder_version=backend_result.encoder_version,
            file_size=output_path.stat().st_size,
            warnings=sidecar.warnings,
            errors=sidecar.errors,
            status=report.completion_status,
            cache_hit=False,
            newly_created=True,
            backend_result=backend_result,
            report=report,
            sidecar=sidecar,
            package_metadata_hash=normalized_metadata_hash,
            output_artifact_content_hash=sidecar.output_artifact_content_hash,
        )
    except PackagingFailure as failure:
        _restore_backup(output_path, backup_output)
        _restore_backup(sidecar_path, backup_sidecar)
        _restore_backup(report_path, backup_report)
        if backup_output is not None:
            _cleanup_backup(backup_output)
        if backup_sidecar is not None:
            _cleanup_backup(backup_sidecar)
        if backup_report is not None:
            _cleanup_backup(backup_report)
        return _build_failure_result(
            book_id=book_id,
            package_id=package_id,
            package_input_hash=package_input_hash,
            output_artifact_path=output_path,
            output_artifact_relative_path=output_relative_path,
            sidecar_path=sidecar_path,
            report_path=report_path,
            container_format=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
            encoder_name=backend.encoder_name,
            encoder_version=backend.encoder_version,
            warnings=tuple(chapter_warnings) + cover_warnings,
            errors=(failure.message,),
            failure=failure,
            status=PackagingCompletionStatus.FAILED if not failure.package_blocking else PackagingCompletionStatus.BLOCKED,
        )
    except Exception as exc:  # noqa: BLE001
        _restore_backup(output_path, backup_output)
        _restore_backup(sidecar_path, backup_sidecar)
        _restore_backup(report_path, backup_report)
        if backup_output is not None:
            _cleanup_backup(backup_output)
        if backup_sidecar is not None:
            _cleanup_backup(backup_sidecar)
        if backup_report is not None:
            _cleanup_backup(backup_report)
        failure = PackagingFailure(
            PackagingFailureType.ENCODING_FAILURE,
            str(exc),
            backend_diagnostic_excerpt=str(exc),
        )
        return _build_failure_result(
            book_id=book_id,
            package_id=package_id,
            package_input_hash=package_input_hash,
            output_artifact_path=output_path,
            output_artifact_relative_path=output_relative_path,
            sidecar_path=sidecar_path,
            report_path=report_path,
            container_format=config.container_format,
            audio_codec=config.audio_codec,
            audio_bitrate_kbps=config.audio_bitrate_kbps,
            sample_rate_hz=config.sample_rate_hz,
            channel_count=config.channel_count,
            backend_name=backend.backend_name,
            backend_version=backend.backend_version,
            encoder_name=backend.encoder_name,
            encoder_version=backend.encoder_version,
            warnings=tuple(chapter_warnings) + cover_warnings,
            errors=(str(exc),),
            failure=failure,
            status=PackagingCompletionStatus.FAILED,
        )


def _maybe_cache_hit(
    *,
    output_path: Path,
    sidecar_path: Path,
    report_path: Path,
    backend: PackagingBackend,
    config: PackagingConfig,
    expected_package_id: str,
    expected_package_input_hash: str,
    expected_book_id: str,
    expected_output_relative_path: str,
    expected_metadata_hash: str,
    expected_cover_art_hash: str | None,
    expected_cover_art_embedded: bool,
    expected_chapter_timeline: tuple[ChapterTimelineEntry, ...],
) -> PackagingResult | None:
    if not output_path.exists() or not sidecar_path.exists():
        return None
    try:
        sidecar = load_package_sidecar(sidecar_path)
    except Exception:
        return None
    if not package_sidecar_matches(
        sidecar,
        expected_package_id=expected_package_id,
        expected_package_input_hash=expected_package_input_hash,
        expected_book_id=expected_book_id,
        expected_packaging_contract_version=config.packaging_contract_version,
        expected_packager_version=config.packager_version,
        expected_backend_name=backend.backend_name,
        expected_backend_version=backend.backend_version,
        expected_encoder_name=backend.encoder_name,
        expected_encoder_version=backend.encoder_version,
        expected_output_artifact_relative_path=expected_output_relative_path,
        expected_container=config.container_format,
        expected_audio_codec=config.audio_codec,
        expected_audio_bitrate_kbps=config.audio_bitrate_kbps,
        expected_sample_rate_hz=config.sample_rate_hz,
        expected_channel_count=config.channel_count,
        expected_chapter_count=len(expected_chapter_timeline),
        expected_chapter_timeline=expected_chapter_timeline,
        expected_canonical_book_metadata_hash=expected_metadata_hash,
        expected_cover_art_hash=expected_cover_art_hash,
        expected_cover_art_embedded=expected_cover_art_embedded,
    ):
        return None
    if _hash_path(output_path) != sidecar.output_artifact_content_hash:
        return None
    probe = backend.probe(output_path)
    validation_failure = validate_backend_probe(
        probe,
        config=config,
        expected_chapter_count=sidecar.chapter_count,
        expected_duration_seconds=sidecar.total_duration_seconds,
        expected_cover_art_enabled=sidecar.cover_art_embedded,
    )
    if validation_failure is not None:
        return None
    backend_result = PackagingBackendResult(
        output_path=output_path,
        output_artifact_relative_path=sidecar.output_artifact_relative_path,
        output_container=sidecar.output_container,
        audio_codec=sidecar.audio_codec,
        audio_bitrate_kbps=sidecar.audio_bitrate_kbps,
        sample_rate_hz=sidecar.sample_rate_hz,
        channel_count=sidecar.channel_count,
        duration_seconds=sidecar.total_duration_seconds,
        chapter_count=sidecar.chapter_count,
        chapter_probe_data=tuple(canonicalize(item) for item in probe.get("chapter_probe_data", [])),
        metadata_probe_data=dict(probe.get("metadata_probe_data", {})),
        cover_art_probe_state=probe.get("cover_art_probe_state"),
        backend_name=sidecar.backend_name,
        backend_version=sidecar.backend_version,
        encoder_name=sidecar.encoder_name,
        encoder_version=sidecar.encoder_version,
        file_size=sidecar.file_size,
        audio_content_hash=sidecar.output_artifact_content_hash,
        validation_result=sidecar.validation_result,
        warnings=sidecar.warnings,
        errors=sidecar.errors,
        probe_data=probe,
    )
    return _cache_hit_result(sidecar=sidecar, output_path=output_path, report_path=report_path, backend_result=backend_result)
