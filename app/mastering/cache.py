from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from app.voice_planner.models import dataclass_to_dict
from app.voice_planner.schema import canonical_json_dumps

from .models import MasteringSidecar


MASTERING_SIDECAR_FILENAME = "mastering_sidecar.json"


class MasteringSidecarError(RuntimeError):
    pass


def _stringify(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_stringify(item) for item in value]
    if isinstance(value, list):
        return [_stringify(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _stringify(item) for key, item in value.items()}
    return value


def build_mastered_chapter_id(*, book_id: str, chapter_id: str, chapter_assembly_id: str, mastering_contract_version: int) -> str:
    payload = {
        "book_id": book_id,
        "chapter_id": chapter_id,
        "chapter_assembly_id": chapter_assembly_id,
        "mastering_contract_version": mastering_contract_version,
    }
    digest = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"mastered-{digest[:24]}"


def build_mastering_input_hash(
    *,
    mastering_contract_version: int,
    processor_version: str,
    backend_name: str,
    backend_version: str,
    book_id: str,
    chapter_id: str,
    chapter_order: int,
    chapter_assembly_id: str,
    source_chapter_input_hash: str,
    source_chapter_audio_content_hash: str,
    target_integrated_loudness_dbfs: float,
    max_gain_increase_db: float,
    max_gain_reduction_db: float,
    max_sample_peak_dbfs: float,
    trim_leading_silence_enabled: bool,
    trim_trailing_silence_enabled: bool,
    leading_silence_target_ms: int,
    trailing_silence_target_ms: int,
    silence_detection_threshold_dbfs: float,
    minimum_silence_duration_ms: int,
    fade_in_ms: int,
    fade_out_ms: int,
    limiter_enabled: bool,
    limiter_ceiling_dbfs: float,
    output_format: str,
    sample_rate_hz: int,
    channel_count: int,
    sample_width_bytes: int,
    source_chapter_assembler_version: str,
    source_chapter_audio_format: str,
    source_chapter_sample_rate_hz: int,
    source_chapter_channel_count: int,
    source_chapter_sample_width_bytes: int,
    source_chapter_output_relative_path: str | None = None,
    source_chapter_title: str | None = None,
) -> str:
    payload = {
        "mastering_contract_version": mastering_contract_version,
        "processor_version": processor_version,
        "backend": {"name": backend_name, "version": backend_version},
        "book_id": book_id,
        "chapter_group": {"chapter_id": chapter_id, "chapter_order": chapter_order, "chapter_assembly_id": chapter_assembly_id},
        "source_chapter_input_hash": source_chapter_input_hash,
        "source_chapter_audio_content_hash": source_chapter_audio_content_hash,
        "target_integrated_loudness_dbfs": target_integrated_loudness_dbfs,
        "gain_limits": {"max_gain_increase_db": max_gain_increase_db, "max_gain_reduction_db": max_gain_reduction_db},
        "peak_limits": {"max_sample_peak_dbfs": max_sample_peak_dbfs, "limiter_enabled": limiter_enabled, "limiter_ceiling_dbfs": limiter_ceiling_dbfs},
        "silence_trim": {
            "trim_leading_silence_enabled": trim_leading_silence_enabled,
            "trim_trailing_silence_enabled": trim_trailing_silence_enabled,
            "leading_silence_target_ms": leading_silence_target_ms,
            "trailing_silence_target_ms": trailing_silence_target_ms,
            "silence_detection_threshold_dbfs": silence_detection_threshold_dbfs,
            "minimum_silence_duration_ms": minimum_silence_duration_ms,
        },
        "fade": {"fade_in_ms": fade_in_ms, "fade_out_ms": fade_out_ms},
        "output_format": output_format,
        "sample_rate_hz": sample_rate_hz,
        "channel_count": channel_count,
        "sample_width_bytes": sample_width_bytes,
        "source_chapter": {
            "assembler_version": source_chapter_assembler_version,
            "audio_format": source_chapter_audio_format,
            "sample_rate_hz": source_chapter_sample_rate_hz,
            "channel_count": source_chapter_channel_count,
            "sample_width_bytes": source_chapter_sample_width_bytes,
        },
    }
    digest = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    return digest


def load_mastering_sidecar(path: Path) -> MasteringSidecar:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise MasteringSidecarError(f"unable to parse mastering sidecar: {path}") from exc
    required = _required_fields()
    missing = [field for field in required if field not in payload]
    if missing:
        raise MasteringSidecarError(f"mastering sidecar missing required fields: {', '.join(sorted(missing))}")
    return MasteringSidecar(
        mastered_chapter_id=str(payload["mastered_chapter_id"]),
        chapter_id=str(payload["chapter_id"]),
        chapter_order=int(payload["chapter_order"]),
        chapter_title=payload.get("chapter_title"),
        book_id=str(payload["book_id"]),
        source_chapter_assembly_id=str(payload["source_chapter_assembly_id"]),
        source_chapter_input_hash=str(payload["source_chapter_input_hash"]),
        source_chapter_audio_content_hash=str(payload["source_chapter_audio_content_hash"]),
        mastering_contract_version=int(payload["mastering_contract_version"]),
        mastering_processor_version=str(payload["mastering_processor_version"]),
        processing_backend=str(payload["processing_backend"]),
        processing_backend_version=str(payload["processing_backend_version"]),
        mastering_input_hash=str(payload["mastering_input_hash"]),
        output_artifact_relative_path=str(payload["output_artifact_relative_path"]),
        output_format=str(payload["output_format"]),
        sample_rate_hz=int(payload["sample_rate_hz"]),
        channel_count=int(payload["channel_count"]),
        sample_width_bytes=int(payload["sample_width_bytes"]),
        input_frame_count=int(payload["input_frame_count"]),
        output_frame_count=int(payload["output_frame_count"]),
        input_duration_seconds=float(payload["input_duration_seconds"]),
        output_duration_seconds=float(payload["output_duration_seconds"]),
        input_integrated_loudness_dbfs=float(payload["input_integrated_loudness_dbfs"]),
        output_integrated_loudness_dbfs=float(payload["output_integrated_loudness_dbfs"]),
        input_sample_peak_dbfs=float(payload["input_sample_peak_dbfs"]),
        output_sample_peak_dbfs=float(payload["output_sample_peak_dbfs"]),
        true_peak_dbfs=payload.get("true_peak_dbfs"),
        requested_gain_db=float(payload["requested_gain_db"]),
        applied_gain_db=float(payload["applied_gain_db"]),
        gain_constrained=bool(payload["gain_constrained"]),
        limiter_activated=bool(payload["limiter_activated"]),
        limiter_amount_db=payload.get("limiter_amount_db"),
        original_leading_silence_frames=int(payload["original_leading_silence_frames"]),
        original_trailing_silence_frames=int(payload["original_trailing_silence_frames"]),
        trimmed_leading_silence_frames=int(payload["trimmed_leading_silence_frames"]),
        trimmed_trailing_silence_frames=int(payload["trimmed_trailing_silence_frames"]),
        final_leading_silence_frames=int(payload["final_leading_silence_frames"]),
        final_trailing_silence_frames=int(payload["final_trailing_silence_frames"]),
        fade_in_frames=int(payload["fade_in_frames"]),
        fade_out_frames=int(payload["fade_out_frames"]),
        mastered_audio_content_hash=str(payload["mastered_audio_content_hash"]),
        validation_result=str(payload["validation_result"]),
        warnings=tuple(str(item) for item in payload.get("warnings", [])),
        errors=tuple(str(item) for item in payload.get("errors", [])),
        source_chapter_output_relative_path=payload.get("source_chapter_output_relative_path"),
        source_chapter_source=payload.get("source_chapter_source"),
    )


def save_mastering_sidecar(path: Path, payload: Mapping[str, Any] | MasteringSidecar) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, MasteringSidecar):
        data = dataclass_to_dict(payload)
    else:
        data = dict(payload)
    path.write_text(canonical_json_dumps(_stringify(data)) + "\n", encoding="utf-8")


def mastering_sidecar_payload(sidecar: MasteringSidecar) -> dict[str, Any]:
    return dataclass_to_dict(sidecar)


def mastering_cache_entry_matches(
    sidecar: MasteringSidecar,
    *,
    expected_mastered_chapter_id: str,
    expected_mastering_input_hash: str,
    expected_output_artifact_relative_path: str,
    expected_chapter_id: str,
    expected_chapter_assembly_id: str,
    expected_source_chapter_audio_content_hash: str,
    expected_output_format: str,
    expected_sample_rate_hz: int,
    expected_channel_count: int,
    expected_sample_width_bytes: int,
    expected_mastering_contract_version: int,
    expected_mastering_processor_version: str,
    expected_backend_name: str,
    expected_backend_version: str,
) -> bool:
    return (
        sidecar.validation_result == "passed"
        and sidecar.mastered_chapter_id == expected_mastered_chapter_id
        and sidecar.mastering_input_hash == expected_mastering_input_hash
        and sidecar.output_artifact_relative_path == expected_output_artifact_relative_path
        and sidecar.chapter_id == expected_chapter_id
        and sidecar.source_chapter_assembly_id == expected_chapter_assembly_id
        and sidecar.source_chapter_audio_content_hash == expected_source_chapter_audio_content_hash
        and sidecar.output_format == expected_output_format
        and sidecar.sample_rate_hz == expected_sample_rate_hz
        and sidecar.channel_count == expected_channel_count
        and sidecar.sample_width_bytes == expected_sample_width_bytes
        and sidecar.mastering_contract_version == expected_mastering_contract_version
        and sidecar.mastering_processor_version == expected_mastering_processor_version
        and sidecar.processing_backend == expected_backend_name
        and sidecar.processing_backend_version == expected_backend_version
    )


def _required_fields() -> tuple[str, ...]:
    return (
        "mastered_chapter_id",
        "chapter_id",
        "chapter_order",
        "book_id",
        "source_chapter_assembly_id",
        "source_chapter_input_hash",
        "source_chapter_audio_content_hash",
        "mastering_contract_version",
        "mastering_processor_version",
        "processing_backend",
        "processing_backend_version",
        "mastering_input_hash",
        "output_artifact_relative_path",
        "output_format",
        "sample_rate_hz",
        "channel_count",
        "sample_width_bytes",
        "input_frame_count",
        "output_frame_count",
        "input_duration_seconds",
        "output_duration_seconds",
        "input_integrated_loudness_dbfs",
        "output_integrated_loudness_dbfs",
        "input_sample_peak_dbfs",
        "output_sample_peak_dbfs",
        "requested_gain_db",
        "applied_gain_db",
        "gain_constrained",
        "limiter_activated",
        "original_leading_silence_frames",
        "original_trailing_silence_frames",
        "trimmed_leading_silence_frames",
        "trimmed_trailing_silence_frames",
        "final_leading_silence_frames",
        "final_trailing_silence_frames",
        "fade_in_frames",
        "fade_out_frames",
        "mastered_audio_content_hash",
        "validation_result",
    )
