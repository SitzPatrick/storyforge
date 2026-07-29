from __future__ import annotations

from .filters import display_name, normalize_key, strip_leading_article


def build_manual_alias_index(alias_config: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, str | float]]:
    index: dict[str, dict[str, str | float]] = {}
    for entity_type, mapping in (alias_config or {}).items():
        for canonical_name, aliases in (mapping or {}).items():
            canonical_display = display_name(canonical_name)
            canonical_key = normalize_key(canonical_display)
            if canonical_key and canonical_key not in index:
                index[canonical_key] = {
                    "entity_type": entity_type,
                    "canonical_name": canonical_display,
                    "speaker_resolution": "canonical_match",
                    "matched_alias": canonical_display,
                    "confidence": 1.0,
                    "reason": "configured canonical name",
                }
            for alias in aliases or []:
                alias_display = display_name(alias)
                for candidate in {normalize_key(alias_display), normalize_key(strip_leading_article(alias_display))}:
                    if candidate and candidate not in index:
                        index[candidate] = {
                            "entity_type": entity_type,
                            "canonical_name": canonical_display,
                            "speaker_resolution": "alias_match",
                            "matched_alias": alias_display,
                            "confidence": 1.0,
                            "reason": "configured alias merge",
                        }
    return index


def resolve_manual_alias(name: str | None, alias_index: dict[str, dict[str, str | float]]) -> dict[str, str | float] | None:
    if not name:
        return None
    key = normalize_key(name)
    if key in alias_index:
        return alias_index[key]
    stripped = normalize_key(strip_leading_article(name))
    if stripped in alias_index:
        return alias_index[stripped]
    return None


def build_resolution_index(canonical_entities: dict[str, list[dict]]) -> dict[str, dict[str, str | float]]:
    index: dict[str, dict[str, str | float]] = {}
    for entity_type, records in canonical_entities.items():
        for record in records:
            canonical_name = record["canonical_name"]
            canonical_key = normalize_key(canonical_name)
            index.setdefault(
                canonical_key,
                {
                    "entity_type": entity_type,
                    "canonical_name": canonical_name,
                    "canonical_id": record["id"],
                    "speaker_resolution": "canonical_match",
                    "matched_alias": canonical_name,
                    "confidence": 1.0,
                    "reason": "normalized canonical name",
                },
            )
            for alias in record.get("aliases", []):
                alias_key = normalize_key(alias)
                if alias_key and alias_key not in index:
                    index[alias_key] = {
                        "entity_type": entity_type,
                        "canonical_name": canonical_name,
                        "canonical_id": record["id"],
                        "speaker_resolution": "alias_match",
                        "matched_alias": alias,
                        "confidence": 1.0,
                        "reason": "normalized alias",
                    }
    return index
