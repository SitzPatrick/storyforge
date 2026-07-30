from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class PackagingFailureType(str, Enum):
    SOURCE_CHAPTER_MISSING = "source_chapter_missing"
    SOURCE_SIDECAR_MISSING = "source_sidecar_missing"
    SOURCE_SIDECAR_CORRUPT = "source_sidecar_corrupt"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    INVALID_MASTERED_AUDIO = "invalid_mastered_audio"
    INCOMPATIBLE_CHAPTER_FORMAT = "incompatible_chapter_format"
    DUPLICATE_CHAPTER_ORDER = "duplicate_chapter_order"
    AMBIGUOUS_CHAPTER_ORDER = "ambiguous_chapter_order"
    INVALID_CHAPTER_TIMING = "invalid_chapter_timing"
    MISSING_REQUIRED_METADATA = "missing_required_metadata"
    INVALID_METADATA = "invalid_metadata"
    COVER_ART_MISSING = "cover_art_missing"
    COVER_ART_INVALID = "cover_art_invalid"
    PACKAGING_BACKEND_UNAVAILABLE = "packaging_backend_unavailable"
    BACKEND_VERSION_UNAVAILABLE = "backend_version_unavailable"
    ENCODING_FAILURE = "encoding_failure"
    INVALID_OUTPUT_CONTAINER = "invalid_output_container"
    CHAPTER_METADATA_FAILURE = "chapter_metadata_failure"
    OUTPUT_VALIDATION_FAILURE = "output_validation_failure"
    UNSAFE_OUTPUT_PATH = "unsafe_output_path"
    OUTPUT_WRITE_FAILURE = "output_write_failure"
    SIDECAR_WRITE_FAILURE = "sidecar_write_failure"
    CACHE_CORRUPTION = "cache_corruption"
    INTERRUPTED_PACKAGING = "interrupted_packaging"
    UNKNOWN_FAILURE = "unknown_failure"


class PackagingCompletionStatus(str, Enum):
    COMPLETE = "complete"
    COMPLETE_WITH_WARNINGS = "complete-with-warnings"
    BLOCKED = "blocked"
    FAILED = "failed"


class PackagingValidationStatus(str, Enum):
    PASSED = "passed"
    PASSED_WITH_WARNINGS = "passed-with-warnings"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class PackagingFailure(RuntimeError):
    failure_type: PackagingFailureType
    message: str
    retryable: bool = False
    package_blocking: bool = True
    chapter_id: str | None = None
    backend_diagnostic_excerpt: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class BookMetadata:
    title: str | None = None
    subtitle: str | None = None
    author: str | None = None
    narrator: str | None = None
    series: str | None = None
    series_position: str | int | None = None
    publisher: str | None = None
    publication_year: str | int | None = None
    description: str | None = None
    language: str | None = None
    copyright: str | None = None
    genre: str | None = None
    identifier: str | None = None
    comment: str | None = None


@dataclass(frozen=True)
class CoverArtInput:
    enabled: bool = False
    source_relative_path: str | None = None
    resolved_source_path: Path | None = None
    source_content_hash: str | None = None
    source_format: str | None = None
    width: int | None = None
    height: int | None = None
    conversion_settings: Mapping[str, Any] = field(default_factory=dict)
    expected_embedded: bool = False


@dataclass(frozen=True)
class PackagingConfig:
    package_root: Path = Path("packages")
    mastered_root: Path = Path("mastered")
    packaging_contract_version: int = 1
    packager_version: str = "milestone-14"
    backend_name: str = "fake"
    backend_version: str = "unknown"
    expected_backend_version: str | None = None
    container_format: str = "m4b"
    audio_codec: str = "aac"
    audio_bitrate_kbps: int = 96
    sample_rate_hz: int = 24000
    channel_count: int = 1
    encoder_profile: str = "aac_low"
    encoder_flags: tuple[str, ...] = ("-movflags", "+faststart")
    chapter_timebase: int = 1_000_000
    chapter_rounding: str = "round"
    metadata_mapping_version: int = 1
    metadata_mapping_name: str = "audiobook"
    embed_cover_art: bool = False
    cover_art_enabled: bool = False
    cover_art_max_width: int = 2000
    cover_art_max_height: int = 2000
    cover_art_format: str = "jpeg"
    cover_art_quality: int = 85
    fast_start: bool = True
    allowed_mastering_statuses: tuple[str, ...] = ("passed", "passed-with-warnings")
    retry_max_attempts: int = 0
    retry_delay_seconds: float = 0.0
    retry_backoff_seconds: float = 0.0

    @property
    def audio_bitrate(self) -> str:
        return f"{self.audio_bitrate_kbps}k"


@dataclass(frozen=True)
class MasteredChapterInput:
    book_id: str
    chapter_id: str
    chapter_order: int
    chapter_title: str | None
    mastered_chapter_id: str
    source_chapter_assembly_id: str
    mastered_chapter_input_hash: str
    mastered_audio_content_hash: str
    output_artifact_relative_path: str
    mastered_audio_path: Path
    mastered_sidecar_path: Path
    duration_seconds: float
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    mastering_validation_result: PackagingValidationStatus | str
    required: bool = True
    source_chapter_output_relative_path: str | None = None
    source_chapter_path: Path | None = None


@dataclass(frozen=True)
class ChapterTimelineEntry:
    book_id: str
    chapter_id: str
    chapter_order: int
    chapter_title: str | None
    mastered_chapter_id: str
    start_time: int
    end_time: int
    duration_ticks: int
    timebase: int
    optional: bool = False


@dataclass(frozen=True)
class PackagingRequest:
    book_id: str
    package_id: str
    package_input_hash: str
    output_path: Path
    temp_output_path: Path
    output_artifact_relative_path: str
    normalized_metadata: BookMetadata
    normalized_metadata_hash: str
    chapter_inputs: tuple[MasteredChapterInput, ...]
    chapter_timeline: tuple[ChapterTimelineEntry, ...]
    cover_art: CoverArtInput | None
    config: PackagingConfig


@dataclass(frozen=True)
class PackagingBackendResult:
    output_path: Path
    output_artifact_relative_path: str
    output_container: str
    audio_codec: str
    audio_bitrate_kbps: int
    sample_rate_hz: int
    channel_count: int
    duration_seconds: float
    chapter_count: int
    chapter_probe_data: tuple[dict[str, Any], ...] = ()
    metadata_probe_data: dict[str, Any] = field(default_factory=dict)
    cover_art_probe_state: dict[str, Any] | None = None
    backend_name: str = "fake"
    backend_version: str = "unknown"
    encoder_name: str = "fake-encoder"
    encoder_version: str | None = None
    file_size: int = 0
    audio_content_hash: str | None = None
    validation_result: PackagingValidationStatus | str = PackagingValidationStatus.PASSED
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    probe_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackageSidecar:
    audiobook_package_id: str
    book_id: str
    packaging_contract_version: int
    packager_version: str
    backend_name: str
    backend_version: str
    encoder_name: str
    encoder_version: str | None
    package_input_hash: str
    output_artifact_relative_path: str
    output_container: str
    audio_codec: str
    audio_bitrate_kbps: int
    sample_rate_hz: int
    channel_count: int
    total_duration_seconds: float
    chapter_count: int
    ordered_chapter_ids: tuple[str, ...]
    ordered_mastered_chapter_ids: tuple[str, ...]
    ordered_mastered_audio_content_hashes: tuple[str, ...]
    chapter_timeline: tuple[ChapterTimelineEntry, ...]
    canonical_book_metadata_hash: str
    cover_art_hash: str | None
    cover_art_embedded: bool
    output_artifact_content_hash: str
    file_size: int
    validation_result: PackagingValidationStatus | str
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackagingReport:
    book_id: str
    package_id: str
    completion_status: PackagingCompletionStatus | str
    package_cache_hit: bool
    package_newly_created: bool
    chapters_expected: int
    chapters_packaged: int
    optional_chapters_omitted: tuple[str, ...]
    blocked_chapters: tuple[str, ...]
    expected_duration_seconds: float
    actual_duration_seconds: float
    duration_delta_seconds: float
    metadata_validation_status: PackagingValidationStatus | str
    cover_art_status: PackagingValidationStatus | str
    backend_name: str
    backend_version: str
    encoder_name: str
    encoder_version: str | None
    output_artifact_relative_path: str
    output_file_size: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    package_input_hash: str
    sidecar_relative_path: str


@dataclass(frozen=True)
class PackagingResult:
    book_id: str
    package_id: str
    package_input_hash: str
    output_artifact_path: Path
    output_artifact_relative_path: str
    sidecar_path: Path
    report_path: Path
    container_format: str
    audio_codec: str
    audio_bitrate_kbps: int
    sample_rate_hz: int
    channel_count: int
    duration_seconds: float
    chapter_count: int
    chapter_probe_data: tuple[dict[str, Any], ...]
    metadata_probe_data: dict[str, Any]
    cover_art_probe_state: dict[str, Any] | None
    backend_name: str
    backend_version: str
    encoder_name: str
    encoder_version: str | None
    file_size: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    status: PackagingCompletionStatus | str
    cache_hit: bool
    newly_created: bool
    failure: PackagingFailure | None = None
    backend_result: PackagingBackendResult | None = None
    report: PackagingReport | None = None
    sidecar: PackageSidecar | None = None
    package_metadata_hash: str | None = None
    output_artifact_content_hash: str | None = None
