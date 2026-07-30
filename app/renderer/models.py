from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Sequence


class RenderFailureType(str, Enum):
    MANIFEST_BLOCKED = "manifest_blocked"
    UNSUPPORTED_PROVIDER = "unsupported_provider"
    UNSUPPORTED_VOICE = "unsupported_voice"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_RENDER_CONTROL = "invalid_renderer_control"
    SYNTHESIS_FAILURE = "synthesis_failure"
    TIMEOUT = "timeout"
    INVALID_GENERATED_AUDIO = "invalid_generated_audio"
    OUTPUT_WRITE_FAILURE = "output_write_failure"
    CACHE_CORRUPTION = "cache_corruption"
    SIDECAR_CORRUPTION = "sidecar_corruption"
    PATH_VALIDATION_FAILURE = "path_validation_failure"
    CANCELLED_OR_INTERRUPTED = "cancelled_or_interrupted"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass(frozen=True)
class RenderFailure(Exception):
    failure_type: RenderFailureType
    message: str
    retryable: bool
    provider: str | None = None
    provider_adapter_version: str | None = None
    model_version: str | None = None
    render_unit_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class RenderContext:
    render_root: Path
    report_path: Path
    output_format: str = "wav"
    sample_rate_hz: int = 24000
    channel_count: int = 1
    sample_width_bytes: int = 2
    maximum_duration_seconds: float = 600.0
    renderer_contract_version: int = 1
    allow_ready_with_warnings: bool = False
    max_attempts: int = 3
    retry_delay_seconds: float = 0.0
    retryable_failure_types: tuple[RenderFailureType, ...] = (
        RenderFailureType.PROVIDER_UNAVAILABLE,
        RenderFailureType.MODEL_UNAVAILABLE,
        RenderFailureType.SYNTHESIS_FAILURE,
        RenderFailureType.TIMEOUT,
    )
    deterministic_seed: int | None = None


RenderSettings = RenderContext


@dataclass(frozen=True)
class RenderUnitResult:
    render_unit_id: str
    canonical_segment_id: str
    provider: str | None
    provider_voice_id: str | None
    status: str
    cache_hit: bool = False
    render_attempts: int = 0
    output_path: str | None = None
    sidecar_path: str | None = None
    bytes_written: int = 0
    duration_seconds: float = 0.0
    audio_content_hash: str | None = None
    cache_key: str | None = None
    validation_result: str | None = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    failure_type: str | None = None
    failure_message: str | None = None
    skipped_reason: str | None = None


@dataclass(frozen=True)
class RenderReport:
    manifest_content_hash: str
    renderer_version: str
    renderer_contract_version: int
    provider_adapters_used: list[str]
    total_render_units: int
    attempted_units: int
    successfully_rendered_units: int
    cache_hits: int
    cache_misses: int
    skipped_units: int
    blocked_units: int
    failed_units: int
    retryable_failures: int
    permanent_failures: int
    audio_duration_seconds: float
    bytes_written: int
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    completion_status: str = "partial"
    unit_results: list[RenderUnitResult] = field(default_factory=list)

    @property
    def rendered_units(self) -> int:
        return self.successfully_rendered_units


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_id: str
    adapter_version: str
    model_version: str | None
    supported_output_formats: Sequence[str]
    supported_sample_rates: Sequence[int]
    supported_channel_counts: Sequence[int]
    supports_seed: bool
    deterministic: bool


@dataclass(frozen=True)
class ProviderRenderRequest:
    render_unit_id: str
    canonical_segment_id: str
    synthesis_input_hash: str
    synthesis_text: str
    provider: str
    provider_voice_id: str
    language: str | None
    controls: dict[str, Any]
    output_format: str
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    pronunciation_notes: str | None = None
    performance_notes: str | None = None
    pace_intent: str | None = None
    pause_intent: str | None = None
    emphasis_intent: str | None = None
    deterministic_seed: int | None = None
    manifest_content_hash: str | None = None
    renderer_contract_version: int = 1


@dataclass(frozen=True)
class ProviderRenderResult:
    provider: str
    provider_adapter_version: str
    model_version: str | None
    output_path: Path
    claimed_deterministic: bool
    warnings: list[str] = field(default_factory=list)
    bytes_written: int = 0
    duration_seconds: float = 0.0


class ProviderRenderSession:
    def render(self, request: ProviderRenderRequest, output_path: Path) -> ProviderRenderResult:  # pragma: no cover - protocol-like base
        raise NotImplementedError


class TTSProviderAdapter:
    provider_id: str
    adapter_version: str
    model_version: str | None

    @property
    def capabilities(self) -> ProviderCapabilities:  # pragma: no cover - protocol-like base
        raise NotImplementedError

    def open_session(self) -> ProviderRenderSession:  # pragma: no cover - protocol-like base
        raise NotImplementedError

    def validate_voice(self, voice_id: str) -> None:  # pragma: no cover - protocol-like base
        raise NotImplementedError
