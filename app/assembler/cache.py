from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from app.voice_planner.models import dataclass_to_dict
from app.voice_planner.schema import canonical_json_dumps

from .models import ChapterSidecar


CHAPTER_SIDECAR_FILENAME = "chapter_sidecar.json"


class ChapterSidecarError(RuntimeError):
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


def build_chapter_assembly_id(*, book_id: str, chapter_id: str, chapter_order: int, assembly_contract_version: int) -> str:
    payload = {
        "assembly_contract_version": assembly_contract_version,
        "book_id": book_id,
        "chapter_id": chapter_id,
        "chapter_order": chapter_order,
    }
    digest = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    return f"chapter-{digest[:24]}"


def build_chapter_input_hash(*, chapter_assembly_id: str, chapter_id: str, chapter_order: int, book_id: str, assembly_contract_version: int, ordered_render_unit_ids: list[str] | tuple[str, ...], ordered_segment_synthesis_input_hashes: list[str] | tuple[str, ...], ordered_segment_audio_content_hashes: list[str] | tuple[str, ...], ordered_segment_cache_keys: list[str] | tuple[str, ...], ordered_segment_artifact_relative_paths: list[str] | tuple[str, ...], spacing: Mapping[str, Any], output_format: str, sample_rate_hz: int, channel_count: int, sample_width_bytes: int, source_section_id: str | None = None, fade_config: Mapping[str, Any] | None = None, trim_config: Mapping[str, Any] | None = None) -> str:
    payload = {
        "assembly_contract_version": assembly_contract_version,
        "book_id": book_id,
        "chapter_assembly_id": chapter_assembly_id,
        "chapter_group_identity": {
            "chapter_id": chapter_id,
            "chapter_order": chapter_order,
            "source_section_id": source_section_id,
        },
        "ordered_render_unit_ids": list(ordered_render_unit_ids),
        "ordered_segment_synthesis_input_hashes": list(ordered_segment_synthesis_input_hashes),
        "ordered_segment_audio_content_hashes": list(ordered_segment_audio_content_hashes),
        "ordered_segment_cache_keys": list(ordered_segment_cache_keys),
        "ordered_segment_artifact_relative_paths": list(ordered_segment_artifact_relative_paths),
        "spacing": _stringify(dict(spacing)),
        "output_format": output_format,
        "sample_rate_hz": sample_rate_hz,
        "channel_count": channel_count,
        "sample_width_bytes": sample_width_bytes,
    }
    if fade_config is not None:
        payload["fade_config"] = _stringify(dict(fade_config))
    if trim_config is not None:
        payload["trim_config"] = _stringify(dict(trim_config))
    digest = hashlib.sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()
    return digest


def load_chapter_sidecar(path: Path) -> ChapterSidecar:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ChapterSidecarError(f"unable to parse chapter sidecar: {path}") from exc
    missing = [field for field in _required_fields() if field not in payload]
    if missing:
        raise ChapterSidecarError(f"chapter sidecar missing required fields: {', '.join(sorted(missing))}")
    ordered = tuple(str(item) for item in payload.get("ordered_render_unit_ids", []))
    ordered_synthesis = tuple(str(item) for item in payload.get("ordered_segment_synthesis_input_hashes", []))
    ordered_audio = tuple(str(item) for item in payload.get("ordered_segment_audio_content_hashes", []))
    ordered_cache_keys = tuple(str(item) for item in payload.get("ordered_segment_cache_keys", []))
    ordered_paths = tuple(str(item) for item in payload.get("ordered_segment_artifact_relative_paths", []))
    warnings = tuple(str(item) for item in payload.get("warnings", []))
    errors = tuple(str(item) for item in payload.get("errors", []))
    blocked = tuple(str(item) for item in payload.get("blocked_unit_ids", []))
    omitted = tuple(str(item) for item in payload.get("omitted_unit_ids", []))
    missing_unit_ids = tuple(str(item) for item in payload.get("missing_unit_ids", []))
    invalid_unit_ids = tuple(str(item) for item in payload.get("invalid_unit_ids", []))
    return ChapterSidecar(
        chapter_assembly_id=str(payload["chapter_assembly_id"]),
        chapter_id=str(payload["chapter_id"]),
        chapter_order=int(payload["chapter_order"]),
        chapter_title=payload.get("chapter_title"),
        source_section_id=payload.get("source_section_id"),
        book_id=str(payload["book_id"]),
        manifest_content_hash=str(payload["manifest_content_hash"]),
        assembly_contract_version=int(payload["assembly_contract_version"]),
        assembler_version=str(payload["assembler_version"]),
        chapter_input_hash=str(payload["chapter_input_hash"]),
        ordered_render_unit_ids=ordered,
        ordered_segment_synthesis_input_hashes=ordered_synthesis,
        ordered_segment_audio_content_hashes=ordered_audio,
        ordered_segment_cache_keys=ordered_cache_keys,
        ordered_segment_artifact_relative_paths=ordered_paths,
        output_artifact_relative_path=str(payload["output_artifact_relative_path"]),
        output_format=str(payload["output_format"]),
        sample_rate_hz=int(payload["sample_rate_hz"]),
        channel_count=int(payload["channel_count"]),
        sample_width_bytes=int(payload["sample_width_bytes"]),
        frame_count=int(payload["frame_count"]),
        speech_frame_count=int(payload["speech_frame_count"]),
        silence_frame_count=int(payload["silence_frame_count"]),
        duration_seconds=float(payload["duration_seconds"]),
        audio_content_hash=str(payload["audio_content_hash"]),
        validation_result=str(payload["validation_result"]),
        warnings=warnings,
        errors=errors,
        blocked_unit_ids=blocked,
        omitted_unit_ids=omitted,
        missing_unit_ids=missing_unit_ids,
        invalid_unit_ids=invalid_unit_ids,
        chapter_source=payload.get("chapter_source"),
    )


def save_chapter_sidecar_payload(sidecar_path: Path, payload: Mapping[str, Any]) -> None:
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(canonical_json_dumps(_stringify(dict(payload))) + "\n", encoding="utf-8")


def chapter_sidecar_payload(sidecar: ChapterSidecar) -> dict[str, Any]:
    return dataclass_to_dict(sidecar)


def chapter_cache_entry_matches(
    sidecar: ChapterSidecar,
    *,
    expected_chapter_assembly_id: str,
    expected_chapter_input_hash: str,
    expected_output_artifact_relative_path: str,
    expected_render_unit_ids: tuple[str, ...],
    expected_output_format: str,
    expected_sample_rate_hz: int,
    expected_channel_count: int,
    expected_sample_width_bytes: int,
    expected_assembly_contract_version: int,
) -> bool:
    return (
        sidecar.validation_result == "passed"
        and sidecar.chapter_assembly_id == expected_chapter_assembly_id
        and sidecar.chapter_input_hash == expected_chapter_input_hash
        and sidecar.output_artifact_relative_path == expected_output_artifact_relative_path
        and sidecar.ordered_render_unit_ids == expected_render_unit_ids
        and sidecar.output_format == expected_output_format
        and sidecar.sample_rate_hz == expected_sample_rate_hz
        and sidecar.channel_count == expected_channel_count
        and sidecar.sample_width_bytes == expected_sample_width_bytes
        and sidecar.assembly_contract_version == expected_assembly_contract_version
    )


def _required_fields() -> tuple[str, ...]:
    return (
        "chapter_assembly_id",
        "chapter_id",
        "chapter_order",
        "book_id",
        "manifest_content_hash",
        "assembly_contract_version",
        "assembler_version",
        "chapter_input_hash",
        "ordered_render_unit_ids",
        "ordered_segment_synthesis_input_hashes",
        "ordered_segment_audio_content_hashes",
        "ordered_segment_cache_keys",
        "ordered_segment_artifact_relative_paths",
        "output_artifact_relative_path",
        "output_format",
        "sample_rate_hz",
        "channel_count",
        "sample_width_bytes",
        "frame_count",
        "speech_frame_count",
        "silence_frame_count",
        "duration_seconds",
        "audio_content_hash",
        "validation_result",
    )
