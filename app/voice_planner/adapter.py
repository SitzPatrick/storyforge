from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .models import CharacterProfile, CharacterProfileBundle, SceneRelationship, dataclass_to_dict
from .schema import canonical_json_dumps, validate_character_profile_bundle

_REQUIRED_INPUT_FILES = (
    "normalized_story.json",
    "normalized_entities.json",
    "normalized_dialogue.json",
    "normalized_scenes.json",
)
_OPTIONAL_INPUT_FILES = ("normalization_report.json",)


class CharacterProfileAdapterError(ValueError):
    pass


def hash_phase3b_inputs(analysis_dir: str | Path) -> dict[str, str]:
    root = Path(analysis_dir)
    hashes: dict[str, str] = {}
    for name in (*_REQUIRED_INPUT_FILES, *_OPTIONAL_INPUT_FILES):
        file_path = root / name
        if file_path.exists():
            hashes[name] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return dict(sorted(hashes.items()))


def load_character_profiles(analysis_dir: str | Path) -> CharacterProfileBundle:
    root = Path(analysis_dir)
    before_hashes = hash_phase3b_inputs(root)
    story = _read_required_json(root / "normalized_story.json")
    entities = _read_required_json(root / "normalized_entities.json")
    dialogue = _read_required_json(root / "normalized_dialogue.json")
    scenes = _read_required_json(root / "normalized_scenes.json")
    report = _read_optional_json(root / "normalization_report.json")

    profiles, statistics = _build_profiles(story, entities, dialogue, scenes, report, before_hashes)
    bundle = CharacterProfileBundle(
        schema_version=1,
        book_id=statistics["book_id"],
        series_id=statistics["series_id"],
        source_analysis_path=str(root),
        source_hashes=before_hashes,
        profiles=profiles,
        normalization_report=report,
        statistics=statistics,
    )
    bundle_dict = dataclass_to_dict(bundle)
    validation_errors = validate_character_profile_bundle(bundle_dict)
    if validation_errors:
        raise CharacterProfileAdapterError("; ".join(validation_errors))

    after_hashes = hash_phase3b_inputs(root)
    if after_hashes != before_hashes:
        raise CharacterProfileAdapterError("normalized Phase 3B input hashes changed during adapter run")
    return bundle


def serialize_character_profiles(bundle: CharacterProfileBundle | dict[str, Any]) -> str:
    return canonical_json_dumps(bundle)


def _read_required_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise CharacterProfileAdapterError(f"missing required normalized file: {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CharacterProfileAdapterError(
            f"malformed JSON in {path.name}: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(data, dict):
        raise CharacterProfileAdapterError(f"normalized file must contain a JSON object: {path.name}")
    return data


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return _read_required_json(path)


def _build_profiles(
    story: dict[str, Any],
    entities: dict[str, Any],
    dialogue: dict[str, Any],
    scenes: dict[str, Any],
    report: dict[str, Any] | None,
    source_hashes: dict[str, str],
) -> tuple[list[CharacterProfile], dict[str, Any]]:
    book_id = _first_nonempty_string(story, ("book_id", "bookId", "title"), default="")
    series_id = _first_nonempty_string(story, ("series_id", "seriesId"), default="")
    if not book_id:
        raise CharacterProfileAdapterError("normalized_story.json missing book_id")
    if not series_id:
        raise CharacterProfileAdapterError("normalized_story.json missing series_id")

    character_records = _extract_character_records(entities)
    character_map: dict[str, dict[str, Any]] = {}
    for record in character_records:
        if _is_rejected_entity(record):
            continue
        character_id = _first_nonempty_string(record, ("canonical_character_id", "character_id", "id"), default="")
        if not character_id:
            raise CharacterProfileAdapterError("normalized_entities.json contains a character without canonical_character_id")
        if character_id in character_map:
            raise CharacterProfileAdapterError(f"duplicate canonical character ID: {character_id}")
        character_map[character_id] = record

    scene_rows = _extract_scene_records(scenes)
    scene_index = _index_scenes(scene_rows)
    dialogue_rows = _extract_dialogue_records(dialogue)
    dialogue_index = _index_dialogue(dialogue_rows)

    scene_memberships: dict[str, set[str]] = {character_id: set() for character_id in character_map}
    scene_speakers: dict[str, set[str]] = {scene_id: set() for scene_id in scene_index}
    character_dialogue_ids: dict[str, list[str]] = {character_id: [] for character_id in character_map}
    character_dialogue_counts: dict[str, int] = {character_id: 0 for character_id in character_map}
    character_first_appearance: dict[str, tuple[int, str]] = {}
    ignored_dialogue_records = 0

    for dialogue_record in dialogue_rows:
        speaker_id = _first_nonempty_string(dialogue_record, ("speaker_character_id", "speaker_id", "speaker"), default="")
        dialogue_id = _first_nonempty_string(dialogue_record, ("dialogue_id", "id"), default="")
        scene_id = _first_nonempty_string(dialogue_record, ("scene_id", "sceneId"), default="")
        if speaker_id and speaker_id in character_map:
            character_dialogue_counts[speaker_id] += 1
            character_dialogue_ids[speaker_id].append(dialogue_id or f"{scene_id}:{len(character_dialogue_ids[speaker_id]) + 1}")
        else:
            ignored_dialogue_records += 1

    for scene_id, scene in scene_index.items():
        members = set(_extract_scene_character_ids(scene)) & set(character_map)
        speakers = set(_extract_scene_speaker_ids(scene)) & set(character_map)
        scene_memberships.update({character_id: scene_memberships.get(character_id, set()) | {scene_id} for character_id in members})
        scene_speakers[scene_id] = speakers or scene_speakers.get(scene_id, set())
        scene_order = scene["_scene_order"]
        for character_id in members:
            current = character_first_appearance.get(character_id)
            candidate = (scene_order, scene_id)
            if current is None or candidate < current:
                character_first_appearance[character_id] = candidate

    profile_rows: list[CharacterProfile] = []
    for character_id in sorted(character_map.keys(), key=lambda cid: (character_first_appearance.get(cid, (10**9, cid))[0], cid)):
        record = character_map[character_id]
        relationships = _build_relationships(character_id, character_map, scene_index, scene_memberships, scene_speakers)
        profile_rows.append(
            CharacterProfile(
                schema_version=1,
                canonical_character_id=character_id,
                canonical_name=_first_nonempty_string(record, ("canonical_name", "name"), default=character_id) or character_id,
                role=_maybe_string(record.get("role")),
                prominence=_maybe_string(record.get("prominence")),
                speaking_frequency=character_dialogue_counts[character_id],
                first_appearance_order=character_first_appearance.get(character_id, (None, None))[0],
                likely_recurrence=_maybe_bool(record.get("likely_recurrence")),
                age_bucket=_maybe_string(record.get("age_bucket")),
                gender_presentation=_maybe_string(record.get("gender_presentation")),
                species_or_archetype=_maybe_string(record.get("species_or_archetype")),
                scene_relationships=relationships,
                dialogue_count=character_dialogue_counts[character_id],
                scene_count=len(scene_memberships.get(character_id, set())),
                source_aliases=_sorted_unique_strings(_extract_aliases(record)),
                unresolved_metadata=_collect_unresolved_metadata(record),
                source_provenance=_build_source_provenance(record, story, scene_memberships.get(character_id, set()), character_dialogue_ids[character_id], source_hashes, report),
                notes=_maybe_string(record.get("notes")),
            )
        )

    statistics = {
        "book_id": book_id,
        "series_id": series_id,
        "character_count": len(profile_rows),
        "scene_count": len(scene_index),
        "dialogue_record_count": len(dialogue_rows),
        "attributed_dialogue_record_count": sum(character_dialogue_counts.values()),
        "unresolved_dialogue_record_count": ignored_dialogue_records,
        "shared_scene_relationship_count": sum(len(profile.scene_relationships) for profile in profile_rows),
    }
    return profile_rows, statistics


def _build_relationships(
    character_id: str,
    character_map: dict[str, dict[str, Any]],
    scene_index: dict[str, dict[str, Any]],
    scene_memberships: dict[str, set[str]],
    scene_speakers: dict[str, set[str]],
) -> list[SceneRelationship]:
    relation_stats: dict[str, dict[str, Any]] = {}
    for scene_id, scene in scene_index.items():
        members = sorted(set(_extract_scene_character_ids(scene)) & set(character_map))
        if character_id not in members:
            continue
        speakers = set(_extract_scene_speaker_ids(scene)) & set(character_map)
        scene_order = scene["_scene_order"]
        for other_id in members:
            if other_id == character_id:
                continue
            key = other_id
            stats = relation_stats.setdefault(
                key,
                {
                    "shared_scene_count": 0,
                    "shared_speaking_scene_count": 0,
                    "first_shared_scene_id": scene_id,
                    "first_shared_scene_order": scene_order,
                },
            )
            stats["shared_scene_count"] += 1
            if character_id in speakers and other_id in speakers:
                stats["shared_speaking_scene_count"] += 1
            current_first = (stats["first_shared_scene_order"], stats["first_shared_scene_id"])
            candidate = (scene_order, scene_id)
            if candidate < current_first:
                stats["first_shared_scene_order"] = scene_order
                stats["first_shared_scene_id"] = scene_id
    relationships: list[SceneRelationship] = []
    for other_id in sorted(relation_stats.keys()):
        stats = relation_stats[other_id]
        relationships.append(
            SceneRelationship(
                related_character_id=other_id,
                related_character_name=_first_nonempty_string(character_map[other_id], ("canonical_name", "name"), default=other_id) or other_id,
                shared_scene_count=int(stats["shared_scene_count"]),
                shared_speaking_scene_count=int(stats["shared_speaking_scene_count"]),
                first_shared_scene_id=stats["first_shared_scene_id"],
                first_shared_scene_order=int(stats["first_shared_scene_order"]),
            )
        )
    return relationships


def _build_source_provenance(
    record: dict[str, Any],
    story: dict[str, Any],
    scene_ids: Iterable[str],
    dialogue_ids: Iterable[str],
    source_hashes: dict[str, str],
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    provenance = _maybe_mapping(record.get("source_provenance"))
    return {
        "book_id": story.get("book_id"),
        "series_id": story.get("series_id"),
        "source_analysis_hash": story.get("source_analysis_hash"),
        "source_analysis_path": story.get("source_analysis_path"),
        "source_hashes": dict(source_hashes),
        "scene_ids": sorted({str(scene_id) for scene_id in scene_ids}),
        "dialogue_ids": sorted({str(dialogue_id) for dialogue_id in dialogue_ids if dialogue_id}),
        "entity_id": _first_nonempty_string(record, ("entity_id", "id"), default=None),
        "entity_ids": _sorted_unique_strings(_maybe_iterable(record.get("entity_ids"))),
        "normalization_report_hash": _report_hash(report),
        "source_provenance": provenance,
    }


def _collect_unresolved_metadata(record: dict[str, Any]) -> dict[str, Any]:
    known = {
        "schema_version",
        "canonical_character_id",
        "character_id",
        "id",
        "canonical_name",
        "name",
        "role",
        "prominence",
        "speaking_frequency",
        "first_appearance_order",
        "likely_recurrence",
        "age_bucket",
        "gender_presentation",
        "species_or_archetype",
        "scene_relationships",
        "dialogue_count",
        "scene_count",
        "source_aliases",
        "aliases",
        "unresolved_metadata",
        "source_provenance",
        "notes",
        "status",
        "rejected",
        "category",
        "entity_ids",
        "entity_id",
    }
    unresolved = _maybe_mapping(record.get("unresolved_metadata"))
    remainder = {key: value for key, value in record.items() if key not in known}
    merged = {**remainder, **unresolved}
    return merged


def _extract_character_records(entities: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(entities.get("characters"), list):
        return [entry for entry in entities["characters"] if isinstance(entry, dict)]
    if isinstance(entities.get("entities"), list):
        return [entry for entry in entities["entities"] if isinstance(entry, dict) and _is_character_entity(entry)]
    raise CharacterProfileAdapterError("normalized_entities.json missing characters list")


def _extract_scene_records(scenes: dict[str, Any]) -> list[dict[str, Any]]:
    rows = scenes.get("scenes")
    if not isinstance(rows, list):
        raise CharacterProfileAdapterError("normalized_scenes.json missing scenes list")
    return [row for row in rows if isinstance(row, dict)]


def _extract_dialogue_records(dialogue: dict[str, Any]) -> list[dict[str, Any]]:
    rows = dialogue.get("dialogue")
    if rows is None:
        rows = dialogue.get("records")
    if not isinstance(rows, list):
        raise CharacterProfileAdapterError("normalized_dialogue.json missing dialogue list")
    return [row for row in rows if isinstance(row, dict)]


def _index_scenes(scene_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    scene_index: dict[str, dict[str, Any]] = {}
    for default_order, scene in enumerate(scene_rows, start=1):
        scene_id = _first_nonempty_string(scene, ("scene_id", "id"), default="")
        if not scene_id:
            raise CharacterProfileAdapterError("normalized_scenes.json contains a scene without scene_id")
        scene_copy = dict(scene)
        scene_copy["_scene_order"] = _first_int(scene, ("scene_order", "order", "index"), default=default_order)
        scene_index[scene_id] = scene_copy
    return dict(sorted(scene_index.items(), key=lambda item: (item[1]["_scene_order"], item[0])))


def _index_dialogue(dialogue_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    dialogue_index: dict[str, dict[str, Any]] = {}
    for default_order, dialogue in enumerate(dialogue_rows, start=1):
        dialogue_id = _first_nonempty_string(dialogue, ("dialogue_id", "id"), default="")
        if not dialogue_id:
            raise CharacterProfileAdapterError("normalized_dialogue.json contains a dialogue record without dialogue_id")
        dialogue_copy = dict(dialogue)
        dialogue_copy["_dialogue_order"] = _first_int(dialogue, ("order", "dialogue_order", "index"), default=default_order)
        dialogue_index[dialogue_id] = dialogue_copy
    return dict(sorted(dialogue_index.items(), key=lambda item: (item[1]["_dialogue_order"], item[0])))


def _extract_scene_character_ids(scene: dict[str, Any]) -> list[str]:
    values = scene.get("character_ids")
    if values is None:
        values = scene.get("characters")
    return _sorted_unique_strings(_maybe_iterable(values))


def _extract_scene_speaker_ids(scene: dict[str, Any]) -> list[str]:
    values = scene.get("speaking_character_ids")
    if values is None:
        values = scene.get("speakers")
    if values is None:
        values = scene.get("speaking_characters")
    return _sorted_unique_strings(_maybe_iterable(values))


def _is_rejected_entity(record: dict[str, Any]) -> bool:
    if bool(record.get("rejected")):
        return True
    status = record.get("status")
    return isinstance(status, str) and status.lower() in {"rejected", "discarded", "suppressed"}


def _is_character_entity(record: dict[str, Any]) -> bool:
    category = record.get("category")
    entity_type = record.get("entity_type")
    entity_kind = record.get("kind")
    return any(
        isinstance(value, str) and value.lower() in {"character", "characters", "person", "people"}
        for value in (category, entity_type, entity_kind)
    )


def _maybe_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _maybe_iterable(value: Any) -> Iterable[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return []


def _maybe_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _maybe_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _first_nonempty_string(data: dict[str, Any], keys: tuple[str, ...], default: str | None = None) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _first_int(data: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = data.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return default


def _sorted_unique_strings(values: Iterable[Any]) -> list[str]:
    unique = {str(value) for value in values if value is not None and str(value)}
    return sorted(unique)


def _extract_aliases(record: dict[str, Any]) -> Iterable[str]:
    aliases = record.get("source_aliases")
    if aliases is None:
        aliases = record.get("aliases")
    if isinstance(aliases, list):
        return aliases
    if isinstance(aliases, dict):
        flattened: list[str] = []
        for value in aliases.values():
            if isinstance(value, list):
                flattened.extend(value)
            elif isinstance(value, str):
                flattened.append(value)
        return flattened
    if isinstance(aliases, str):
        return [aliases]
    return []


def _report_hash(report: dict[str, Any] | None) -> str | None:
    if report is None:
        return None
    return hashlib.sha256(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
