from __future__ import annotations

import re

from .filters import normalize_key, unique_preserve

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _validate_source_references(record: dict) -> list[str]:
    errors: list[str] = []
    if "source_reference" in record:
        ref = record.get("source_reference")
        if not isinstance(ref, dict):
            errors.append("missing source_reference")
            return errors
        if not ref.get("source_text_hash") or not _HEX64.match(str(ref.get("source_text_hash"))):
            errors.append("invalid source_reference.source_text_hash")
        if not record.get("source_text_hash") or not _HEX64.match(str(record.get("source_text_hash"))):
            errors.append("invalid source_text_hash")
        return errors

    refs = record.get("source_references")
    if isinstance(refs, list) and refs:
        for ref in refs:
            if not isinstance(ref, dict):
                errors.append("invalid source_references entry")
                continue
            if not ref.get("source_text_hash") or not _HEX64.match(str(ref.get("source_text_hash"))):
                errors.append("invalid source_references.source_text_hash")
    return errors


def validate_normalized(normalized_entities: dict, normalized_dialogue: dict, normalized_scenes: dict) -> list[str]:
    errors: list[str] = []

    active_entities = normalized_entities
    for category in ("characters", "places", "organizations"):
        records = list(active_entities.get(category) or [])
        ids = [record.get("id") for record in records]
        if len(ids) != len(set(ids)):
            errors.append(f"duplicate {category} ids")
        active_names = {normalize_key(record.get("canonical_name")) for record in records}
        for record in records:
            errors.extend(_validate_source_references(record))
            if not record.get("status") == "active":
                errors.append(f"inactive canonical record in {category}")
        for rejected in normalized_entities.get("rejected", []):
            if rejected.get("original_category") == category and normalize_key(rejected.get("original_name")) in active_names:
                errors.append(f"rejected entity surfaced in active {category}")

    dialogue_records = list(normalized_dialogue.get("dialogue") or [])
    dialogue_keys: set[tuple] = set()
    character_ids = {record.get("id") for record in active_entities.get("characters") or []}
    for record in dialogue_records:
        if not record.get("quoted_text"):
            errors.append("missing dialogue quoted_text")
        errors.extend(_validate_source_references(record))
        key = (
            record.get("chapter"),
            record.get("paragraph_index"),
            record.get("source_document_id"),
            record.get("source_text_hash"),
            record.get("quoted_text"),
            record.get("speaker"),
        )
        if key in dialogue_keys:
            errors.append("exact duplicate dialogue record remains")
        dialogue_keys.add(key)
        speaker_id = record.get("speaker_id")
        if speaker_id is not None and speaker_id not in character_ids:
            errors.append(f"unresolved dialogue speaker id {speaker_id}")
        if not record.get("speaker_resolution"):
            errors.append("missing dialogue speaker_resolution")

    place_ids = {record.get("id") for record in active_entities.get("places") or []}
    org_ids = {record.get("id") for record in active_entities.get("organizations") or []}
    scene_records = list(normalized_scenes.get("scenes") or [])
    for scene in scene_records:
        errors.extend(_validate_source_references(scene))
        for entry in scene.get("characters") or []:
            if entry.get("id") not in character_ids:
                errors.append(f"unresolved scene character id {entry.get('id')}")
        for entry in scene.get("places") or []:
            if entry.get("id") not in place_ids:
                errors.append(f"unresolved scene place id {entry.get('id')}")
        for entry in scene.get("organizations") or []:
            if entry.get("id") not in org_ids:
                errors.append(f"unresolved scene organization id {entry.get('id')}")

    return unique_preserve(errors)
