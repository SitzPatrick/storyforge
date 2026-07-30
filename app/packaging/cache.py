from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .metadata import normalize_book_metadata
from .models import BookMetadata, ChapterTimelineEntry, PackageSidecar, PackagingConfig, PackagingRequest, PackagingValidationStatus
from .serialization import canonical_bytes, canonical_json, canonicalize

PACKAGE_SIDECAR_FILENAME = "package_sidecar.json"
PACKAGE_REPORT_FILENAME = "packaging_report.json"


class PackageSidecarError(RuntimeError):
    pass


def build_audiobook_package_id(*, book_id: str, packaging_contract_version: int, container_format: str) -> str:
    payload = {
        "book_id": book_id,
        "packaging_contract_version": packaging_contract_version,
        "container_format": container_format,
    }
    return f"package-{hashlib.sha256(canonical_bytes(payload)).hexdigest()[:24]}"


def build_package_input_hash(
    request: PackagingRequest,
    *,
    backend_name: str,
    backend_version: str,
    encoder_name: str,
    encoder_version: str | None,
    cover_art_hash: str | None,
) -> str:
    payload = {
        "book_id": request.book_id,
        "package_id": request.package_id,
        "packaging_contract_version": request.config.packaging_contract_version,
        "packager_version": request.config.packager_version,
        "backend_name": backend_name,
        "backend_version": backend_version,
        "encoder_name": encoder_name,
        "encoder_version": encoder_version,
        "container_format": request.config.container_format,
        "audio_codec": request.config.audio_codec,
        "audio_bitrate_kbps": request.config.audio_bitrate_kbps,
        "sample_rate_hz": request.config.sample_rate_hz,
        "channel_count": request.config.channel_count,
        "encoder_profile": request.config.encoder_profile,
        "encoder_flags": request.config.encoder_flags,
        "chapter_timebase": request.config.chapter_timebase,
        "chapter_rounding": request.config.chapter_rounding,
        "metadata_mapping_version": request.config.metadata_mapping_version,
        "metadata_mapping_name": request.config.metadata_mapping_name,
        "allowed_mastering_statuses": request.config.allowed_mastering_statuses,
        "ordered_chapter_ids": [chapter.chapter_id for chapter in request.chapter_inputs],
        "ordered_mastered_chapter_ids": [chapter.mastered_chapter_id for chapter in request.chapter_inputs],
        "ordered_mastered_audio_content_hashes": [chapter.mastered_audio_content_hash for chapter in request.chapter_inputs],
        "ordered_mastered_source_durations": [chapter.duration_seconds for chapter in request.chapter_inputs],
        "ordered_chapter_titles": [chapter.chapter_title for chapter in request.chapter_inputs],
        "chapter_timeline": [canonicalize(entry) for entry in request.chapter_timeline],
        "normalized_book_metadata": canonicalize(request.normalized_metadata),
        "normalized_book_metadata_hash": request.normalized_metadata_hash,
        "cover_art_hash": cover_art_hash,
        "cover_art_settings": None if request.cover_art is None else canonicalize(request.cover_art.conversion_settings),
        "fast_start": request.config.fast_start,
        "embed_cover_art": request.config.embed_cover_art,
    }
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def load_package_sidecar(path: Path) -> PackageSidecar:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PackageSidecarError(f"unable to parse package sidecar: {path}") from exc
    missing = [field for field in _required_fields() if field not in payload]
    if missing:
        raise PackageSidecarError(f"package sidecar missing required fields: {', '.join(sorted(missing))}")
    return PackageSidecar(
        audiobook_package_id=str(payload["audiobook_package_id"]),
        book_id=str(payload["book_id"]),
        packaging_contract_version=int(payload["packaging_contract_version"]),
        packager_version=str(payload["packager_version"]),
        backend_name=str(payload["backend_name"]),
        backend_version=str(payload["backend_version"]),
        encoder_name=str(payload["encoder_name"]),
        encoder_version=payload.get("encoder_version"),
        package_input_hash=str(payload["package_input_hash"]),
        output_artifact_relative_path=str(payload["output_artifact_relative_path"]),
        output_container=str(payload["output_container"]),
        audio_codec=str(payload["audio_codec"]),
        audio_bitrate_kbps=int(payload["audio_bitrate_kbps"]),
        sample_rate_hz=int(payload["sample_rate_hz"]),
        channel_count=int(payload["channel_count"]),
        total_duration_seconds=float(payload["total_duration_seconds"]),
        chapter_count=int(payload["chapter_count"]),
        ordered_chapter_ids=tuple(str(item) for item in payload.get("ordered_chapter_ids", [])),
        ordered_mastered_chapter_ids=tuple(str(item) for item in payload.get("ordered_mastered_chapter_ids", [])),
        ordered_mastered_audio_content_hashes=tuple(str(item) for item in payload.get("ordered_mastered_audio_content_hashes", [])),
        chapter_timeline=tuple(_load_timeline_entry(item) for item in payload.get("chapter_timeline", [])),
        canonical_book_metadata_hash=str(payload["canonical_book_metadata_hash"]),
        cover_art_hash=payload.get("cover_art_hash"),
        cover_art_embedded=bool(payload["cover_art_embedded"]),
        output_artifact_content_hash=str(payload["output_artifact_content_hash"]),
        file_size=int(payload["file_size"]),
        validation_result=str(payload["validation_result"]),
        warnings=tuple(str(item) for item in payload.get("warnings", [])),
        errors=tuple(str(item) for item in payload.get("errors", [])),
    )


def save_package_sidecar(path: Path, sidecar: PackageSidecar | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = canonicalize(sidecar)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def package_sidecar_matches(
    sidecar: PackageSidecar,
    *,
    expected_package_id: str,
    expected_package_input_hash: str,
    expected_book_id: str,
    expected_packaging_contract_version: int,
    expected_packager_version: str,
    expected_backend_name: str,
    expected_backend_version: str,
    expected_encoder_name: str,
    expected_encoder_version: str | None,
    expected_output_artifact_relative_path: str,
    expected_container: str,
    expected_audio_codec: str,
    expected_audio_bitrate_kbps: int,
    expected_sample_rate_hz: int,
    expected_channel_count: int,
    expected_chapter_count: int,
    expected_chapter_timeline: tuple[ChapterTimelineEntry, ...],
    expected_canonical_book_metadata_hash: str,
    expected_cover_art_hash: str | None,
    expected_cover_art_embedded: bool,
    expected_validation_result: str | PackagingValidationStatus | None = None,
) -> bool:
    if expected_validation_result is None:
        allowed_validation_texts = {PackagingValidationStatus.PASSED.value, PackagingValidationStatus.PASSED_WITH_WARNINGS.value}
    elif isinstance(expected_validation_result, PackagingValidationStatus):
        allowed_validation_texts = {expected_validation_result.value}
    else:
        allowed_validation_texts = {str(expected_validation_result)}
    return (
        str(sidecar.validation_result) in allowed_validation_texts
        and sidecar.audiobook_package_id == expected_package_id
        and sidecar.book_id == expected_book_id
        and sidecar.packaging_contract_version == expected_packaging_contract_version
        and sidecar.packager_version == expected_packager_version
        and sidecar.backend_name == expected_backend_name
        and sidecar.backend_version == expected_backend_version
        and sidecar.encoder_name == expected_encoder_name
        and sidecar.encoder_version == expected_encoder_version
        and sidecar.package_input_hash == expected_package_input_hash
        and sidecar.output_artifact_relative_path == expected_output_artifact_relative_path
        and sidecar.output_container == expected_container
        and sidecar.audio_codec == expected_audio_codec
        and sidecar.audio_bitrate_kbps == expected_audio_bitrate_kbps
        and sidecar.sample_rate_hz == expected_sample_rate_hz
        and sidecar.channel_count == expected_channel_count
        and sidecar.chapter_count == expected_chapter_count
        and sidecar.chapter_timeline == expected_chapter_timeline
        and sidecar.canonical_book_metadata_hash == expected_canonical_book_metadata_hash
        and sidecar.cover_art_hash == expected_cover_art_hash
        and sidecar.cover_art_embedded == expected_cover_art_embedded
    )


def canonical_package_path(*, package_root: Path, book_id: str, package_id: str, container_format: str) -> Path:
    return package_root / _safe_segment(book_id) / f"{_safe_segment(package_id)}.{container_format.lstrip('.')}"


def canonical_relative_package_path(*, book_id: str, package_id: str, container_format: str) -> str:
    return f"packages/{_safe_segment(book_id)}/{_safe_segment(package_id)}.{container_format.lstrip('.')}"


def _load_timeline_entry(payload: dict[str, Any]) -> ChapterTimelineEntry:
    return ChapterTimelineEntry(
        book_id=str(payload["book_id"]),
        chapter_id=str(payload["chapter_id"]),
        chapter_order=int(payload["chapter_order"]),
        chapter_title=payload.get("chapter_title"),
        mastered_chapter_id=str(payload["mastered_chapter_id"]),
        start_time=int(payload["start_time"]),
        end_time=int(payload["end_time"]),
        duration_ticks=int(payload["duration_ticks"]),
        timebase=int(payload["timebase"]),
        optional=bool(payload.get("optional", False)),
    )


def _safe_segment(value: str) -> str:
    cleaned = [char if (char.isalnum() or char in {"-", "_", "."}) else "-" for char in str(value)]
    text = "".join(cleaned).strip(".-_")
    return text or "unnamed"


def _required_fields() -> tuple[str, ...]:
    return (
        "audiobook_package_id",
        "book_id",
        "packaging_contract_version",
        "packager_version",
        "backend_name",
        "backend_version",
        "encoder_name",
        "encoder_version",
        "package_input_hash",
        "output_artifact_relative_path",
        "output_container",
        "audio_codec",
        "audio_bitrate_kbps",
        "sample_rate_hz",
        "channel_count",
        "total_duration_seconds",
        "chapter_count",
        "ordered_chapter_ids",
        "ordered_mastered_chapter_ids",
        "ordered_mastered_audio_content_hashes",
        "chapter_timeline",
        "canonical_book_metadata_hash",
        "cover_art_hash",
        "cover_art_embedded",
        "output_artifact_content_hash",
        "file_size",
        "validation_result",
    )
