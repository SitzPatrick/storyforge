from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from .models import dataclass_to_dict

SCHEMA_VERSIONS = {
    "voice_capability": 1,
    "voice_plan": 1,
    "assignment_report": 1,
    "series_bindings": 1,
    "character_profile": 1,
    "character_profile_bundle": 1,
}

_REQUIRED_VOICE_REGISTRY_KEYS = {
    "schema_version",
    "registry_version",
    "voices",
}

_REQUIRED_VOICE_CAPABILITY_KEYS = {
    "schema_version",
    "voice_id",
    "provider",
    "provider_voice_id",
    "display_name",
    "availability",
    "quality_score",
    "base_priority",
}

_REQUIRED_VOICE_PLAN_KEYS = {
    "schema_version",
    "planner_version",
    "book_id",
    "series_id",
    "source_analysis_hash",
    "source_analysis_path",
    "narrator",
    "characters",
    "conflicts",
    "scarcity_events",
    "warnings",
    "statistics",
}

_REQUIRED_ASSIGNMENT_REPORT_KEYS = {
    "schema_version",
    "book_id",
    "series_id",
    "plan_hash",
    "generated_at",
    "narrator_choice",
    "reused_bindings",
    "new_bindings",
    "manual_overrides",
    "locked_assignments",
    "deferred_characters",
    "unavailable_voices",
    "scarcity_events",
    "similarity_conflicts",
    "scene_conflicts",
    "fallback_tiers_used",
    "scoring_summaries",
    "validation_warnings",
    "final_statistics",
}

_REQUIRED_SERIES_BINDINGS_KEYS = {
    "schema_version",
    "series_id",
    "bindings",
    "narrator",
    "history",
    "updated_at",
}

_REQUIRED_CHARACTER_PROFILE_KEYS = {
    "schema_version",
    "canonical_character_id",
    "canonical_name",
    "speaking_frequency",
    "dialogue_count",
    "scene_count",
    "scene_relationships",
    "source_aliases",
    "unresolved_metadata",
    "source_provenance",
}

_REQUIRED_CHARACTER_PROFILE_BUNDLE_KEYS = {
    "schema_version",
    "book_id",
    "series_id",
    "source_analysis_path",
    "source_hashes",
    "profiles",
    "statistics",
}


class SchemaValidationError(ValueError):
    pass


def canonical_json_dumps(value: Any) -> str:
    return json.dumps(dataclass_to_dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def migrate_schema(data: Mapping[str, Any], target_schema: str) -> dict[str, Any]:
    current = data.get("schema_version")
    if current == SCHEMA_VERSIONS.get(target_schema):
        return dict(data)
    raise SchemaValidationError(f"Unsupported schema migration from {current!r} to {target_schema!r}")


def validate_voice_registry(data: Mapping[str, Any]) -> list[str]:
    errors = _validate_required_keys(data, _REQUIRED_VOICE_REGISTRY_KEYS, "voice registry")
    if not isinstance(data.get("schema_version"), int):
        errors.append("voice registry schema_version must be an integer")
    if not isinstance(data.get("registry_version"), str) or not data.get("registry_version"):
        errors.append("voice registry registry_version must be a non-empty string")
    voices = data.get("voices")
    if not isinstance(voices, Sequence) or isinstance(voices, (str, bytes)):
        errors.append("voice registry voices must be a sequence")
        return errors
    seen_pairs = set()
    seen_voice_ids = set()
    for idx, voice in enumerate(voices):
        if not isinstance(voice, Mapping):
            errors.append(f"voice registry entry {idx} must be a mapping")
            continue
        entry_errors = _validate_required_keys(voice, _REQUIRED_VOICE_CAPABILITY_KEYS, f"voice registry entry {idx}")
        errors.extend(entry_errors)
        voice_id = voice.get("voice_id")
        provider = voice.get("provider")
        provider_voice_id = voice.get("provider_voice_id")
        if not isinstance(voice_id, str) or not voice_id.strip():
            errors.append(f"voice registry entry {idx} voice_id must be a non-empty string")
        if not isinstance(provider, str) or not provider.strip():
            errors.append(f"voice registry entry {idx} provider must be a non-empty string")
        if not isinstance(provider_voice_id, str) or not provider_voice_id.strip():
            errors.append(f"voice registry entry {idx} provider_voice_id must be a non-empty string")
        if voice_id in seen_voice_ids:
            errors.append(f"duplicate voice registry voice_id: {voice_id}")
        seen_voice_ids.add(voice_id)
        pair = (provider, provider_voice_id)
        if pair in seen_pairs:
            errors.append(f"duplicate voice registry key: {provider}::{provider_voice_id}")
        seen_pairs.add(pair)

        display_name = voice.get("display_name")
        if not isinstance(display_name, str) or not display_name.strip():
            errors.append(f"voice registry entry {idx} display_name must be a non-empty string")
        if not isinstance(voice.get("quality_score"), (int, float)):
            errors.append(f"voice registry entry {idx} quality_score must be numeric")
        if not isinstance(voice.get("base_priority"), int):
            errors.append(f"voice registry entry {idx} base_priority must be an integer")
        if not isinstance(voice.get("availability"), str) or not voice.get("availability"):
            errors.append(f"voice registry entry {idx} availability must be a non-empty string")
        _validate_optional_string_field(voice, idx, "gender_presentation", errors)
        _validate_optional_string_field(voice, idx, "age_presentation", errors)
        _validate_optional_string_field(voice, idx, "similarity_cluster", errors)
        _validate_optional_string_field(voice, idx, "licensing_information", errors)
        _validate_optional_int_field(voice, idx, "latency_estimate_ms", errors)
        _validate_optional_int_field(voice, idx, "sample_rate_hz", errors)
        _validate_string_sequence_field(voice, idx, "archetype_tags", errors)
        _validate_string_sequence_field(voice, idx, "style_tags", errors)
        _validate_string_sequence_field(voice, idx, "supported_languages", errors)
        _validate_string_sequence_field(voice, idx, "supported_controls", errors)
    return errors


def validate_voice_plan(data: Mapping[str, Any]) -> list[str]:
    errors = _validate_required_keys(data, _REQUIRED_VOICE_PLAN_KEYS, "voice plan")
    narrator = data.get("narrator")
    if not isinstance(narrator, Mapping):
        errors.append("voice plan narrator must be a mapping")
    characters = data.get("characters")
    if not isinstance(characters, Sequence) or isinstance(characters, (str, bytes)):
        errors.append("voice plan characters must be a sequence")
    else:
        for idx, character in enumerate(characters):
            if not isinstance(character, Mapping):
                errors.append(f"voice plan character {idx} must be a mapping")
    _validate_sequence_of_mappings(data, "conflicts", errors)
    _validate_sequence_of_mappings(data, "scarcity_events", errors)
    _validate_sequence_of_strings(data, "warnings", errors)
    statistics = data.get("statistics")
    if not isinstance(statistics, Mapping):
        errors.append("voice plan statistics must be a mapping")
    return errors


def validate_assignment_report(data: Mapping[str, Any]) -> list[str]:
    errors = _validate_required_keys(data, _REQUIRED_ASSIGNMENT_REPORT_KEYS, "assignment report")
    for key in (
        "reused_bindings",
        "new_bindings",
        "manual_overrides",
        "locked_assignments",
        "deferred_characters",
        "unavailable_voices",
        "scarcity_events",
        "similarity_conflicts",
        "scene_conflicts",
        "scoring_summaries",
    ):
        _validate_sequence_of_mappings(data, key, errors)
    _validate_sequence_of_strings(data, "fallback_tiers_used", errors)
    _validate_sequence_of_strings(data, "validation_warnings", errors)
    if not isinstance(data.get("narrator_choice"), Mapping):
        errors.append("assignment report narrator_choice must be a mapping")
    if not isinstance(data.get("final_statistics"), Mapping):
        errors.append("assignment report final_statistics must be a mapping")
    return errors


def validate_series_bindings(data: Mapping[str, Any]) -> list[str]:
    errors = _validate_required_keys(data, _REQUIRED_SERIES_BINDINGS_KEYS, "series bindings")
    bindings = data.get("bindings")
    if not isinstance(bindings, Sequence) or isinstance(bindings, (str, bytes)):
        errors.append("series bindings bindings must be a sequence")
    narrator = data.get("narrator")
    if narrator is not None and not isinstance(narrator, Mapping):
        errors.append("series bindings narrator must be a mapping or null")
    history = data.get("history")
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        errors.append("series bindings history must be a sequence")
    return errors


def validate_character_profile(data: Mapping[str, Any]) -> list[str]:
    errors = _validate_required_keys(data, _REQUIRED_CHARACTER_PROFILE_KEYS, "character profile")
    if not isinstance(data.get("schema_version"), int):
        errors.append("character profile schema_version must be an integer")
    if not isinstance(data.get("canonical_character_id"), str) or not data.get("canonical_character_id"):
        errors.append("character profile canonical_character_id must be a non-empty string")
    if not isinstance(data.get("canonical_name"), str) or not data.get("canonical_name"):
        errors.append("character profile canonical_name must be a non-empty string")
    if not isinstance(data.get("speaking_frequency"), int):
        errors.append("character profile speaking_frequency must be an integer")
    if not isinstance(data.get("dialogue_count"), int):
        errors.append("character profile dialogue_count must be an integer")
    if not isinstance(data.get("scene_count"), int):
        errors.append("character profile scene_count must be an integer")
    _validate_sequence_of_mappings(data, "scene_relationships", errors)
    _validate_sequence_of_strings(data, "source_aliases", errors)
    if not isinstance(data.get("unresolved_metadata"), Mapping):
        errors.append("character profile unresolved_metadata must be a mapping")
    if not isinstance(data.get("source_provenance"), Mapping):
        errors.append("character profile source_provenance must be a mapping")
    return errors


def validate_character_profile_bundle(data: Mapping[str, Any]) -> list[str]:
    errors = _validate_required_keys(data, _REQUIRED_CHARACTER_PROFILE_BUNDLE_KEYS, "character profile bundle")
    if not isinstance(data.get("schema_version"), int):
        errors.append("character profile bundle schema_version must be an integer")
    if not isinstance(data.get("book_id"), str) or not data.get("book_id"):
        errors.append("character profile bundle book_id must be a non-empty string")
    if not isinstance(data.get("series_id"), str) or not data.get("series_id"):
        errors.append("character profile bundle series_id must be a non-empty string")
    if not isinstance(data.get("source_analysis_path"), str) or not data.get("source_analysis_path"):
        errors.append("character profile bundle source_analysis_path must be a non-empty string")
    source_hashes = data.get("source_hashes")
    if not isinstance(source_hashes, Mapping):
        errors.append("character profile bundle source_hashes must be a mapping")
    else:
        for key, value in source_hashes.items():
            if not isinstance(key, str) or not key:
                errors.append("character profile bundle source_hashes keys must be non-empty strings")
            if not isinstance(value, str) or not value:
                errors.append(f"character profile bundle source_hashes[{key!r}] must be a non-empty string")
    profiles = data.get("profiles")
    if not isinstance(profiles, Sequence) or isinstance(profiles, (str, bytes)):
        errors.append("character profile bundle profiles must be a sequence")
    else:
        for idx, profile in enumerate(profiles):
            if not isinstance(profile, Mapping):
                errors.append(f"character profile bundle profile {idx} must be a mapping")
            else:
                errors.extend([f"profiles[{idx}]: {message}" for message in validate_character_profile(profile)])
    if not isinstance(data.get("statistics"), Mapping):
        errors.append("character profile bundle statistics must be a mapping")
    return errors


def _validate_required_keys(data: Mapping[str, Any], required: set[str], label: str) -> list[str]:
    if not isinstance(data, Mapping):
        return [f"{label} must be a mapping"]
    errors = [f"missing {label} field: {key}" for key in sorted(required - set(data.keys()))]
    return errors


def _validate_sequence_of_mappings(data: Mapping[str, Any], key: str, errors: list[str]) -> None:
    value = data.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(f"{key} must be a sequence")
        return
    for idx, item in enumerate(value):
        if not isinstance(item, Mapping):
            errors.append(f"{key}[{idx}] must be a mapping")


def _validate_sequence_of_strings(data: Mapping[str, Any], key: str, errors: list[str]) -> None:
    value = data.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(f"{key} must be a sequence")
        return
    for idx, item in enumerate(value):
        if not isinstance(item, str):
            errors.append(f"{key}[{idx}] must be a string")


def _validate_optional_string_field(data: Mapping[str, Any], idx: int, key: str, errors: list[str]) -> None:
    value = data.get(key)
    if value is not None and not isinstance(value, str):
        errors.append(f"voice registry entry {idx} {key} must be a string or null")


def _validate_optional_int_field(data: Mapping[str, Any], idx: int, key: str, errors: list[str]) -> None:
    value = data.get(key)
    if value is not None and not isinstance(value, int):
        errors.append(f"voice registry entry {idx} {key} must be an integer or null")


def _validate_string_sequence_field(data: Mapping[str, Any], idx: int, key: str, errors: list[str]) -> None:
    value = data.get(key)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(f"voice registry entry {idx} {key} must be a sequence")
        return
    for item_idx, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"voice registry entry {idx} {key}[{item_idx}] must be a non-empty string")
