from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import VoiceCapability
from .schema import SchemaValidationError, canonical_json_dumps, validate_voice_registry

_SELECTABLE_AVAILABILITY = {"available", "enabled"}


def load_voice_registry(path: str | Path) -> dict[str, Any]:
    registry_path = Path(path)
    raw = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SchemaValidationError("voice registry must be a JSON object")
    errors = validate_voice_registry(raw)
    if errors:
        raise SchemaValidationError("; ".join(errors))
    voices = [_coerce_voice_capability(entry) for entry in sorted(raw["voices"], key=_voice_sort_key)]
    return {
        "schema_version": int(raw["schema_version"]),
        "registry_version": raw["registry_version"],
        "voices": voices,
    }


def dump_voice_registry(registry: dict[str, Any]) -> str:
    return canonical_json_dumps(registry)


def write_voice_registry(path: str | Path, registry: dict[str, Any]) -> None:
    registry_path = Path(path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(dump_voice_registry(registry) + "\n", encoding="utf-8")


def voice_registry_key(voice: VoiceCapability | dict[str, Any]) -> tuple[str, str]:
    if isinstance(voice, VoiceCapability):
        return voice.provider, voice.provider_voice_id
    return str(voice["provider"]), str(voice["provider_voice_id"])


def is_voice_selectable(voice: VoiceCapability | dict[str, Any]) -> bool:
    availability = voice.availability if isinstance(voice, VoiceCapability) else voice.get("availability")
    return isinstance(availability, str) and availability.lower() in _SELECTABLE_AVAILABILITY


def selectable_voices(registry: dict[str, Any]) -> list[VoiceCapability]:
    return [voice for voice in registry.get("voices", []) if is_voice_selectable(voice)]


def _coerce_voice_capability(entry: dict[str, Any]) -> VoiceCapability:
    return VoiceCapability(
        schema_version=int(entry["schema_version"]),
        voice_id=str(entry["voice_id"]),
        provider=str(entry["provider"]),
        provider_voice_id=str(entry["provider_voice_id"]),
        display_name=str(entry["display_name"]),
        gender_presentation=_optional_str(entry.get("gender_presentation")),
        age_presentation=_optional_str(entry.get("age_presentation")),
        archetype_tags=_sorted_unique_strings(entry.get("archetype_tags", [])),
        style_tags=_sorted_unique_strings(entry.get("style_tags", [])),
        similarity_cluster=_optional_str(entry.get("similarity_cluster")),
        quality_score=float(entry["quality_score"]),
        latency_estimate_ms=_optional_int(entry.get("latency_estimate_ms")),
        supported_languages=_sorted_unique_strings(entry.get("supported_languages", [])),
        sample_rate_hz=_optional_int(entry.get("sample_rate_hz")),
        supported_controls=_sorted_unique_strings(entry.get("supported_controls", [])),
        licensing_information=_optional_str(entry.get("licensing_information")),
        availability=str(entry["availability"]),
        base_priority=int(entry["base_priority"]),
        notes=_optional_str(entry.get("notes")),
    )


def _voice_sort_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (str(entry.get("provider", "")), str(entry.get("provider_voice_id", "")), str(entry.get("voice_id", "")))


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _sorted_unique_strings(values: Any) -> list[str]:
    if values is None:
        return []
    unique = {str(value) for value in values}
    return sorted(unique)
