from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class MasteringFailureType(str, Enum):
    SOURCE_CHAPTER_MISSING = "source_chapter_missing"
    SOURCE_SIDECAR_MISSING = "source_sidecar_missing"
    SOURCE_SIDECAR_CORRUPT = "source_sidecar_corrupt"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    INVALID_SOURCE_AUDIO = "invalid_source_audio"
    UNSUPPORTED_SOURCE_FORMAT = "unsupported_source_format"
    MASTERING_BACKEND_UNAVAILABLE = "mastering_backend_unavailable"
    MASTERING_BACKEND_VERSION_UNAVAILABLE = "mastering_backend_version_unavailable"
    MEASUREMENT_FAILURE = "measurement_failure"
    GAIN_CALCULATION_FAILURE = "gain_calculation_failure"
    GAIN_EXCEEDS_CONFIGURED_LIMIT = "gain_exceeds_configured_limit"
    LIMITER_FAILURE = "limiter_failure"
    SILENCE_ANALYSIS_FAILURE = "silence_analysis_failure"
    PROCESSING_FAILURE = "processing_failure"
    INVALID_MASTERED_AUDIO = "invalid_mastered_audio"
    QUALITY_TARGET_FAILURE = "quality_target_failure"
    UNSAFE_OUTPUT_PATH = "unsafe_output_path"
    OUTPUT_WRITE_FAILURE = "output_write_failure"
    SIDECAR_WRITE_FAILURE = "sidecar_write_failure"
    CACHE_CORRUPTION = "cache_corruption"
    INTERRUPTED_MASTERING = "interrupted_mastering"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass(frozen=True)
class MasteringFailure:
    failure_type: MasteringFailureType
    message: str
    chapter_id: str | None = None
    mastered_chapter_id: str | None = None
    retryable: bool = False
    chapter_blocking: bool = True
    full_run_blocking: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MasteringConfig:
    mastering_root: Path = Path("mastered")
    source_root: Path = Path("assembly")
    mastering_contract_version: int = 1
    processor_version: str = "milestone-13"
    backend_name: str = "python-rms"
    backend_version: str = "1"
    target_integrated_loudness_dbfs: float = -20.0
    max_gain_increase_db: float = 24.0
    max_gain_reduction_db: float = 24.0
    max_sample_peak_dbfs: float = -1.0
    trim_leading_silence_enabled: bool = True
    trim_trailing_silence_enabled: bool = True
    leading_silence_target_ms: int = 5
    trailing_silence_target_ms: int = 5
    silence_detection_threshold_dbfs: float = -60.0
    minimum_silence_duration_ms: int = 10
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    limiter_enabled: bool = False
    limiter_ceiling_dbfs: float = -1.0
    output_format: str = "wav"
    sample_rate_hz: int = 24000
    channel_count: int = 1
    sample_width_bytes: int = 2
    assembler_compatibility_version: int = 1
    source_assembler_version: str = "milestone-12"
    silence_trim_tolerance_frames: int = 1
    loudness_tolerance_db: float = 0.75
    peak_tolerance_db: float = 0.1
    max_duration_change_ratio: float = 0.2


@dataclass(frozen=True)
class AudioMeasurements:
    path: Path
    file_size: int
    frame_count: int
    duration_seconds: float
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    sample_peak: float
    sample_peak_dbfs: float
    integrated_loudness_dbfs: float
    true_peak: float | None
    leading_silence_frames: int
    trailing_silence_frames: int
    audio_content_hash: str


@dataclass(frozen=True)
class MasteringSidecar:
    mastered_chapter_id: str
    chapter_id: str
    chapter_order: int
    chapter_title: str | None
    book_id: str
    source_chapter_assembly_id: str
    source_chapter_input_hash: str
    source_chapter_audio_content_hash: str
    mastering_contract_version: int
    mastering_processor_version: str
    processing_backend: str
    processing_backend_version: str
    mastering_input_hash: str
    output_artifact_relative_path: str
    output_format: str
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    input_frame_count: int
    output_frame_count: int
    input_duration_seconds: float
    output_duration_seconds: float
    input_integrated_loudness_dbfs: float
    output_integrated_loudness_dbfs: float
    input_sample_peak_dbfs: float
    output_sample_peak_dbfs: float
    true_peak_dbfs: float | None
    requested_gain_db: float
    applied_gain_db: float
    gain_constrained: bool
    limiter_activated: bool
    limiter_amount_db: float | None
    original_leading_silence_frames: int
    original_trailing_silence_frames: int
    trimmed_leading_silence_frames: int
    trimmed_trailing_silence_frames: int
    final_leading_silence_frames: int
    final_trailing_silence_frames: int
    fade_in_frames: int
    fade_out_frames: int
    mastered_audio_content_hash: str
    validation_result: str
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    source_chapter_output_relative_path: str | None = None
    source_chapter_source: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class MasteringResult:
    chapter_id: str
    chapter_order: int
    chapter_title: str | None
    book_id: str
    source_chapter_assembly_id: str
    mastered_chapter_id: str
    mastering_input_hash: str
    output_artifact_relative_path: str
    output_artifact_path: str
    sidecar_path: str
    status: str
    cache_hit: bool
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    failure: MasteringFailure | None = None
    bytes_written: int = 0
    input_frame_count: int = 0
    output_frame_count: int = 0
    input_duration_seconds: float = 0.0
    output_duration_seconds: float = 0.0
    input_integrated_loudness_dbfs: float = 0.0
    output_integrated_loudness_dbfs: float = 0.0
    input_sample_peak_dbfs: float = 0.0
    output_sample_peak_dbfs: float = 0.0
    requested_gain_db: float = 0.0
    applied_gain_db: float = 0.0
    gain_constrained: bool = False
    limiter_activated: bool = False
    limiter_amount_db: float | None = None
    original_leading_silence_frames: int = 0
    original_trailing_silence_frames: int = 0
    trimmed_leading_silence_frames: int = 0
    trimmed_trailing_silence_frames: int = 0
    final_leading_silence_frames: int = 0
    final_trailing_silence_frames: int = 0
    fade_in_frames: int = 0
    fade_out_frames: int = 0
    mastered_audio_content_hash: str | None = None


@dataclass(frozen=True)
class MasteringReport:
    book_id: str
    mastering_contract_version: int
    processor_version: str
    backend_name: str
    backend_version: str
    total_chapters: int
    mastered_chapters: int
    cache_hit_chapters: int
    newly_processed_chapters: int
    blocked_chapters: int
    warning_chapters: int
    failed_chapters: int
    total_input_duration_seconds: float
    total_output_duration_seconds: float
    total_frames_trimmed: int
    aggregate_loudness_statistics: dict[str, Any]
    aggregate_peak_statistics: dict[str, Any]
    chapters_constrained_by_peak_headroom: int
    chapters_using_limiting: int
    bytes_written: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    completion_status: str
    chapter_results: tuple[MasteringResult, ...] = ()
