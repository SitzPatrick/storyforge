from __future__ import annotations

from collections import defaultdict
from hashlib import sha256

from .aliases import build_manual_alias_index, resolve_manual_alias
from .filters import (
    appearance_count,
    build_reject_index,
    display_name,
    entity_key,
    normalize_key,
    normalize_text,
    rejection_rule,
    slugify,
    source_record_id,
    unique_preserve,
)


def _stable_entity_id(entity_type: str, canonical_name: str) -> str:
    slug = slugify(canonical_name)
    return f"{entity_type[:-1] if entity_type.endswith('s') else entity_type}_{slug}"


def _source_reference_id(ref: dict) -> str:
    chapter = ref.get("chapter", "unknown")
    paragraph = ref.get("paragraph_index", "unknown")
    digest = str(ref.get("source_text_hash") or "")[:12]
    return f"chapter_{chapter}_paragraph_{paragraph}_{digest}"


def _raw_category_item_count(record: dict, category: str) -> int:
    if category == "characters":
        return int(record.get("dialogue_count") or 0) + int(record.get("narration_mentions") or 0)
    return len(record.get("source_references") or [])


def _merge_reason(item: dict) -> str:
    if item["resolution"] == "alias_match":
        if item["matched_alias"] != item["canonical_name"]:
            return "configured alias merge"
        return "configured canonical name"
    if item["resolution"] == "canonical_match":
        return "case-insensitive exact match"
    if item["resolution"] == "article_normalized_match":
        return "article-normalized merge"
    return item["reason"]


def _sort_entities(records: list[dict]) -> list[dict]:
    return sorted(records, key=lambda r: (-int(r.get("appearance_count") or 0), r["canonical_name"].lower(), r["id"]))


def normalize_entities(story: dict, settings) -> dict:
    raw_sources = {
        "characters": list(story.get("characters") or []),
        "places": list(story.get("places") or []),
        "organizations": list(story.get("organizations") or []),
    }
    reject_sets = {category: build_reject_index(settings.normalization.rejection_labels.get(category, [])) for category in raw_sources}
    alias_index = build_manual_alias_index(settings.normalization.aliases)

    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    rejected: list[dict] = []
    alias_merges: list[dict] = []

    for category, records in raw_sources.items():
        for record in records:
            name = record.get("name") or ""
            rejection = rejection_rule(name, reject_sets[category])
            if rejection:
                rejected.append(
                    {
                        "original_name": name,
                        "original_category": category,
                        "rejection_rule": rejection,
                        "source_count": _raw_category_item_count(record, category),
                        "source_record_id": source_record_id(record),
                    }
                )
                continue

            manual = resolve_manual_alias(name, alias_index)
            if manual:
                entity_type = str(manual["entity_type"])
                canonical_name = str(manual["canonical_name"])
                resolution = str(manual["speaker_resolution"])
                matched_alias = str(manual["matched_alias"])
                confidence = float(manual["confidence"])
                reason = str(manual["reason"])
            else:
                entity_type = category
                canonical_name = display_name(name)
                key_name = normalize_key(name)
                key_display = normalize_key(canonical_name)
                if entity_type in {"places", "organizations"} and key_name != key_display:
                    resolution = "article_normalized_match"
                    matched_alias = name
                    confidence = 0.9
                    reason = "article-normalized merge"
                else:
                    resolution = "canonical_match"
                    matched_alias = canonical_name
                    confidence = 0.95
                    reason = "case-insensitive exact match"

            canonical_name = display_name(canonical_name)
            group_key = (entity_type, normalize_key(canonical_name))
            grouped[group_key].append(
                {
                    "record": record,
                    "source_category": category,
                    "canonical_name": canonical_name,
                    "entity_type": entity_type,
                    "resolution": resolution,
                    "matched_alias": matched_alias,
                    "confidence": confidence,
                    "reason": reason,
                }
            )

    active: dict[str, list[dict]] = {"characters": [], "places": [], "organizations": []}

    for (entity_type, _), items in grouped.items():
        canonical_name = items[0]["canonical_name"]
        source_names: list[str] = []
        source_record_ids: list[str] = []
        source_categories: list[str] = []
        source_references: list[dict] = []
        aliases: list[str] = []
        appearance_total = 0
        confidences: list[float] = []
        reasons: list[str] = []

        for item in items:
            record = item["record"]
            source_name = normalize_text(record.get("name"))
            source_names.append(source_name)
            source_record_ids.append(source_record_id(record))
            source_categories.append(item["source_category"])
            source_references.extend(record.get("source_references") or [])
            appearance_total += _raw_category_item_count(record, item["source_category"])
            confidences.append(float(item["confidence"]))
            reasons.append(_merge_reason(item))
            if normalize_key(source_name) != normalize_key(canonical_name):
                aliases.append(source_name)
            for alias in record.get("aliases") or []:
                if normalize_key(alias) != normalize_key(canonical_name):
                    aliases.append(normalize_text(alias))

        source_references_sorted = sorted(
            source_references,
            key=lambda ref: (int(ref.get("chapter") or 0), int(ref.get("paragraph_index") or 0), str(ref.get("source_text_hash") or "")),
        )
        dedup_refs: list[dict] = []
        seen_ref_ids: set[str] = set()
        for ref in source_references_sorted:
            ref_id = _source_reference_id(ref)
            if ref_id in seen_ref_ids:
                continue
            seen_ref_ids.add(ref_id)
            dedup_refs.append(ref)

        canonical_record = {
            "id": _stable_entity_id(entity_type, canonical_name),
            "canonical_name": canonical_name,
            "aliases": unique_preserve([alias for alias in aliases if normalize_key(alias) != normalize_key(canonical_name)]),
            "entity_type": entity_type[:-1] if entity_type.endswith("s") else entity_type,
            "source_record_ids": unique_preserve(source_record_ids),
            "source_names": unique_preserve(source_names),
            "source_categories": unique_preserve(source_categories),
            "source_references": dedup_refs,
            "appearance_count": appearance_total,
            "confidence": round(max(confidences) if confidences else 0.0, 2),
            "normalization_reason": "; ".join(unique_preserve(reasons)),
            "status": "active",
        }
        active[entity_type].append(canonical_record)

        if len(items) > 1 or any(item["resolution"] != "canonical_match" for item in items):
            alias_merges.append(
                {
                    "id": canonical_record["id"],
                    "entity_type": canonical_record["entity_type"],
                    "canonical_name": canonical_name,
                    "aliases": canonical_record["aliases"],
                    "source_names": canonical_record["source_names"],
                    "source_record_ids": canonical_record["source_record_ids"],
                    "source_categories": canonical_record["source_categories"],
                    "appearance_count": appearance_total,
                    "normalization_reason": canonical_record["normalization_reason"],
                }
            )

    for category in active:
        active[category] = _sort_entities(active[category])

    return {
        "characters": active["characters"],
        "places": active["places"],
        "organizations": active["organizations"],
        "rejected": rejected,
        "alias_merges": alias_merges,
        "lookup_index": __build_lookup_index(active),
    }


def __build_lookup_index(active: dict[str, list[dict]]) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for category, records in active.items():
        for record in records:
            canonical_name = record["canonical_name"]
            canonical_key = normalize_key(canonical_name)
            index.setdefault(
                canonical_key,
                {
                    "entity_type": category,
                    "canonical_name": canonical_name,
                    "canonical_id": record["id"],
                    "match_type": "canonical_match",
                    "confidence": 1.0,
                },
            )
            for alias in record.get("aliases") or []:
                alias_key = normalize_key(alias)
                if alias_key and alias_key not in index:
                    index[alias_key] = {
                        "entity_type": category,
                        "canonical_name": canonical_name,
                        "canonical_id": record["id"],
                        "match_type": "alias_match",
                        "confidence": 1.0,
                    }
            for source_name in record.get("source_names") or []:
                source_key = normalize_key(source_name)
                if source_key and source_key not in index:
                    index[source_key] = {
                        "entity_type": category,
                        "canonical_name": canonical_name,
                        "canonical_id": record["id"],
                        "match_type": "source_name_match",
                        "confidence": 0.95,
                    }
    return index
