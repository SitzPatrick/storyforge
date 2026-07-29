from __future__ import annotations

from .filters import build_reject_index, normalize_key, normalize_text, rejection_rule, unique_preserve


def _scene_labels(scene: dict, key: str) -> list[str]:
    values = scene.get(key) or []
    labels: list[str] = []
    for value in values:
        if isinstance(value, dict):
            labels.append(str(value.get("name") or value.get("label") or value.get("text") or ""))
        else:
            labels.append(str(value))
    return labels


def _resolve_labels(labels: list[str], lookup_index: dict[str, dict], reject_index: set[str]) -> tuple[list[dict], list[str]]:
    active: list[dict] = []
    unresolved: list[str] = []
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    for label in labels:
        text = normalize_text(label)
        key = normalize_key(text)
        match = lookup_index.get(key) or lookup_index.get(normalize_key(text.replace("’", "'")))
        if match:
            entity_id = match["canonical_id"]
            if entity_id not in seen_ids:
                active.append({"id": entity_id, "name": match["canonical_name"]})
                seen_ids.add(entity_id)
            continue
        if rejection_rule(text, reject_index) or key in {"unknown", "unknown location", "not specified", "none", "n/a"}:
            if text and text not in seen_labels:
                unresolved.append(text)
                seen_labels.add(text)
            continue
        if text and text not in seen_labels:
            unresolved.append(text)
            seen_labels.add(text)
    return active, unresolved


def normalize_scenes(scenes: list[dict], lookup_indexes: dict[str, dict[str, dict]], settings) -> dict:
    char_reject = build_reject_index(settings.normalization.rejection_labels.get("characters", []))
    place_reject = build_reject_index(settings.normalization.rejection_labels.get("places", []))
    org_reject = build_reject_index(settings.normalization.rejection_labels.get("organizations", []))
    normalized: list[dict] = []
    sensible_boundaries = 0
    accurate_summaries = 0
    character_hits = 0
    place_hits = 0

    for scene in scenes:
        character_labels = _scene_labels(scene, "participating_characters")
        place_labels = _scene_labels(scene, "locations")
        organization_labels = _scene_labels(scene, "organizations")

        characters, unresolved_characters = _resolve_labels(character_labels, lookup_indexes.get("characters", {}), char_reject)
        places, unresolved_places = _resolve_labels(place_labels, lookup_indexes.get("places", {}), place_reject)
        organizations, unresolved_organizations = _resolve_labels(organization_labels, lookup_indexes.get("organizations", {}), org_reject)

        character_hits += len(characters)
        place_hits += len(places)
        if characters or places:
            sensible_boundaries += 1
        if scene.get("summary"):
            accurate_summaries += 1

        normalized.append(
            {
                "chapter": scene.get("chapter"),
                "scene_number": scene.get("scene_number"),
                "start_paragraph": scene.get("start_paragraph"),
                "end_paragraph": scene.get("end_paragraph"),
                "summary": scene.get("summary"),
                "source_document_id": scene.get("source_document_id"),
                "source_reference": scene.get("source_reference"),
                "source_text_hash": scene.get("source_text_hash"),
                "characters": characters,
                "places": places,
                "organizations": organizations,
                "unresolved_character_labels": unique_preserve(unresolved_characters),
                "unresolved_place_labels": unique_preserve(unresolved_places),
                "unresolved_organization_labels": unique_preserve(unresolved_organizations),
            }
        )

    total = len(scenes) or 1
    return {
        "scenes": normalized,
        "statistics": {
            "raw_count": len(scenes),
            "normalized_count": len(normalized),
            "sensible_boundary_count": sensible_boundaries,
            "accurate_summary_count": accurate_summaries,
            "character_list_hit_count": character_hits,
            "place_list_hit_count": place_hits,
        },
    }
