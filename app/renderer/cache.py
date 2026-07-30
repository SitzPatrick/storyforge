from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.voice_planner import canonical_json_dumps

from .audio_validation import validate_rendered_audio


@dataclass(frozen=True)
class RenderCacheEntry:
    render_unit_id: str
    canonical_segment_id: str | None
    synthesis_input_hash: str
    renderer_contract_version: int
    provider: str | None
    provider_voice_id: str | None
    provider_adapter_version: str
    model_version: str | None
    output_format: str
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    deterministic_seed: int | None
    manifest_content_hash: str | None
    cache_key: str
    artifact_relative_path: str
    validation_result: str
    attempt_outcome: str | None
    warnings: list[str]
    errors: list[str]
    audio_content_hash: str
    frame_count: int | None = None
    duration_seconds: float | None = None
    sidecar_path: Path | None = None


_REQUIRED_FIELDS = {
    "render_unit_id",
    "synthesis_input_hash",
    "renderer_contract_version",
    "provider",
    "provider_voice_id",
    "provider_adapter_version",
    "model_version",
    "output_format",
    "sample_rate_hz",
    "channel_count",
    "sample_width_bytes",
    "cache_key",
    "artifact_relative_path",
    "validation_result",
    "frame_count",
    "duration_seconds",
    "audio_content_hash",
}


def build_render_cache_key(payload: Mapping[str, Any]) -> str:
    normalized = {
        "render_unit_id": str(payload["render_unit_id"]),
        "synthesis_input_hash": str(payload["synthesis_input_hash"]),
        "renderer_contract_version": int(payload["renderer_contract_version"]),
        "provider": str(payload["provider"]),
        "provider_voice_id": str(payload["provider_voice_id"]),
        "provider_adapter_version": str(payload["provider_adapter_version"]),
        "model_version": payload.get("model_version"),
        "output_format": str(payload["output_format"]),
        "sample_rate_hz": int(payload["sample_rate_hz"]),
        "channel_count": int(payload["channel_count"]),
        "sample_width_bytes": int(payload["sample_width_bytes"]),
        "deterministic_seed": payload.get("deterministic_seed"),
    }
    return hashlib.sha256(canonical_json_dumps(normalized).encode("utf-8")).hexdigest()


def load_render_sidecar(path: Path) -> RenderCacheEntry | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupt render sidecar: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"render sidecar must be a JSON object: {path}")
    missing = sorted(_REQUIRED_FIELDS - set(payload))
    if missing:
        raise ValueError(f"render sidecar missing required fields: {', '.join(missing)}")
    return RenderCacheEntry(
        render_unit_id=str(payload["render_unit_id"]),
        canonical_segment_id=payload.get("canonical_segment_id"),
        synthesis_input_hash=str(payload["synthesis_input_hash"]),
        renderer_contract_version=int(payload["renderer_contract_version"]),
        provider=payload.get("provider"),
        provider_voice_id=payload.get("provider_voice_id"),
        provider_adapter_version=str(payload["provider_adapter_version"]),
        model_version=payload.get("model_version"),
        output_format=str(payload["output_format"]),
        sample_rate_hz=int(payload["sample_rate_hz"]),
        channel_count=int(payload["channel_count"]),
        sample_width_bytes=int(payload["sample_width_bytes"]),
        deterministic_seed=payload.get("deterministic_seed"),
        manifest_content_hash=payload.get("manifest_content_hash"),
        cache_key=str(payload["cache_key"]),
        artifact_relative_path=str(payload["artifact_relative_path"]),
        validation_result=str(payload["validation_result"]),
        attempt_outcome=payload.get("attempt_outcome"),
        warnings=[str(item) for item in payload.get("warnings", []) or []],
        errors=[str(item) for item in payload.get("errors", []) or []],
        audio_content_hash=str(payload["audio_content_hash"]),
        frame_count=payload.get("frame_count"),
        duration_seconds=payload.get("duration_seconds"),
        sidecar_path=path,
    )


def cache_entry_matches(
    entry: RenderCacheEntry,
    *,
    render_unit_id: str,
    synthesis_input_hash: str,
    renderer_contract_version: int,
    provider: str,
    provider_voice_id: str,
    provider_adapter_version: str,
    model_version: str | None,
    cache_key: str,
    output_format: str,
    sample_rate_hz: int,
    channel_count: int,
    sample_width_bytes: int,
    artifact_path: Path,
    artifact_relative_path: str,
) -> bool:
    if entry.render_unit_id != render_unit_id:
        return False
    if entry.synthesis_input_hash != synthesis_input_hash:
        return False
    if entry.renderer_contract_version != renderer_contract_version:
        return False
    if entry.provider != provider:
        return False
    if entry.provider_voice_id != provider_voice_id:
        return False
    if entry.provider_adapter_version != provider_adapter_version:
        return False
    if entry.model_version != model_version:
        return False
    if entry.cache_key != cache_key:
        return False
    if entry.output_format != output_format:
        return False
    if entry.sample_rate_hz != sample_rate_hz:
        return False
    if entry.channel_count != channel_count:
        return False
    if entry.sample_width_bytes != sample_width_bytes:
        return False
    if Path(entry.artifact_relative_path).as_posix() != Path(artifact_relative_path).as_posix():
        return False
    if not artifact_path.exists() or not artifact_path.is_file():
        return False
    try:
        validation = validate_rendered_audio(
            artifact_path,
            expected_sample_rate=sample_rate_hz,
            expected_channels=channel_count,
            expected_sample_width=sample_width_bytes,
            maximum_duration_seconds=10_000.0,
        )
    except Exception:
        return False
    if entry.frame_count is not None and entry.frame_count != validation.frame_count:
        return False
    if entry.duration_seconds is not None and abs(float(entry.duration_seconds) - validation.duration_seconds) > 1e-6:
        return False
    if entry.validation_result != "passed":
        return False
    return validation.audio_content_hash == entry.audio_content_hash
