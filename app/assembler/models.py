from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class AssemblyFailureType(str, Enum):
    MANIFEST_BLOCKED = "manifest_blocked"
    CHAPTER_MAPPING_MISSING = "chapter_mapping_missing"
    DUPLICATE_CHAPTER_ID = "duplicate_chapter_id"
    DUPLICATE_RENDER_UNIT_MEMBERSHIP = "duplicate_render_unit_membership"
    SEGMENT_MISSING = "segment_missing"
    SEGMENT_SIDECAR_MISSING = "segment_sidecar_missing"
    SEGMENT_SIDECAR_CORRUPT = "segment_sidecar_corrupt"
    SEGMENT_HASH_MISMATCH = "segment_hash_mismatch"
    INVALID_SEGMENT_AUDIO = "invalid_segment_audio"
    INCOMPATIBLE_SEGMENT_FORMAT = "incompatible_segment_format"
    UNSAFE_OUTPUT_PATH = "unsafe_output_path"
    OUTPUT_WRITE_FAILURE = "output_write_failure"
    SIDECAR_WRITE_FAILURE = "sidecar_write_failure"
    CHAPTER_VALIDATION_FAILURE = "chapter_validation_failure"
    CACHE_CORRUPTION = "cache_corruption"
    INTERRUPTED_ASSEMBLY = "interrupted_assembly"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass(frozen=True)
class AssemblyFailure:
    failure_type: AssemblyFailureType
    message: str
    chapter_id: str | None = None
    render_unit_id: str | None = None
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChapterSpacingConfig:
    chapter_start_ms: int = 0
    chapter_end_ms: int = 0
    narration_to_narration_ms: int = 0
    narration_to_dialogue_ms: int = 0
    dialogue_to_narration_ms: int = 0
    dialogue_to_dialogue_ms: int = 0
    scene_boundary_ms: int = 0
    default_between_segments_ms: int = 0


@dataclass(frozen=True)
class ChapterAssemblyConfig:
    assembly_root: Path
    segment_root: Path
    assembly_contract_version: int = 1
    assembler_version: str = "milestone-12"
    output_format: str = "wav"
    sample_rate_hz: int = 24000
    channel_count: int = 1
    sample_width_bytes: int = 2
    fallback_chapter_mode: str = "reject"
    empty_chapter_policy: str = "reject"
    missing_segment_policy: str = "block"
    spacing: ChapterSpacingConfig = field(default_factory=ChapterSpacingConfig)
    chapter_path_prefix: str = "chapters"
    report_filename: str = "chapter_assembly_report.json"
    silence_rounding: str = "round"


@dataclass(frozen=True)
class ChapterGroup:
    chapter_id: str
    chapter_order: int
    chapter_title: str | None = None
    source_section_id: str | None = None
    render_unit_ids: tuple[str, ...] = ()
    scene_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChapterSegmentStatus:
    render_unit_id: str
    status: str
    artifact_path: str | None = None
    sidecar_path: str | None = None
    warnings: tuple[str, ...] = ()
    failure: AssemblyFailure | None = None
    audio_content_hash: str | None = None
    synthesis_input_hash: str | None = None
    cache_key: str | None = None
    frame_count: int | None = None
    duration_seconds: float | None = None


@dataclass(frozen=True)
class ChapterAssemblyResult:
    chapter_assembly_id: str
    chapter_id: str
    chapter_order: int
    chapter_title: str | None
    source_section_id: str | None
    output_artifact_relative_path: str
    output_artifact_path: str
    sidecar_path: str
    chapter_input_hash: str
    render_unit_ids: tuple[str, ...]
    status: str
    cache_hit: bool
    speech_frame_count: int
    silence_frame_count: int
    frame_count: int
    duration_seconds: float
    bytes_written: int
    audio_content_hash: str | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    missing_unit_ids: tuple[str, ...] = ()
    invalid_unit_ids: tuple[str, ...] = ()
    blocked_unit_ids: tuple[str, ...] = ()
    omitted_unit_ids: tuple[str, ...] = ()
    segment_statuses: tuple[ChapterSegmentStatus, ...] = ()


@dataclass(frozen=True)
class ChapterAssemblyReport:
    book_id: str
    manifest_content_hash: str
    assembler_version: str
    assembly_contract_version: int
    total_chapters: int
    completed_chapters: int
    cache_hit_chapters: int
    newly_assembled_chapters: int
    blocked_chapters: int
    partial_chapters: int
    failed_chapters: int
    total_source_render_units: int
    assembled_render_units: int
    blocked_units: int
    omitted_units: int
    missing_artifacts: int
    invalid_artifacts: int
    total_speech_duration_seconds: float
    total_inserted_silence_duration_seconds: float
    total_chapter_duration_seconds: float
    bytes_written: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    completion_status: str
    chapter_results: tuple[ChapterAssemblyResult, ...] = ()


@dataclass(frozen=True)
class ChapterSidecar:
    chapter_assembly_id: str
    chapter_id: str
    chapter_order: int
    chapter_title: str | None
    source_section_id: str | None
    book_id: str
    manifest_content_hash: str
    assembly_contract_version: int
    assembler_version: str
    chapter_input_hash: str
    ordered_render_unit_ids: tuple[str, ...]
    ordered_segment_synthesis_input_hashes: tuple[str, ...]
    ordered_segment_audio_content_hashes: tuple[str, ...]
    ordered_segment_cache_keys: tuple[str, ...]
    ordered_segment_artifact_relative_paths: tuple[str, ...]
    output_artifact_relative_path: str
    output_format: str
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    frame_count: int
    speech_frame_count: int
    silence_frame_count: int
    duration_seconds: float
    audio_content_hash: str
    validation_result: str
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    blocked_unit_ids: tuple[str, ...] = ()
    omitted_unit_ids: tuple[str, ...] = ()
    missing_unit_ids: tuple[str, ...] = ()
    invalid_unit_ids: tuple[str, ...] = ()
    chapter_source: Mapping[str, Any] | None = None
