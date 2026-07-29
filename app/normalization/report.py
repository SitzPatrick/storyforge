from __future__ import annotations

from datetime import datetime, timezone


def build_report(*, analysis_dir, output_dir, raw_hashes_before, raw_hashes_after, raw_story, normalized_entities, normalized_dialogue, normalized_scenes, validation_errors, duplicate_dialogue_records, rejected_entities, alias_merges) -> dict:
    active_chars = normalized_entities.get("characters") or []
    active_places = normalized_entities.get("places") or []
    active_orgs = normalized_entities.get("organizations") or []
    rejected_chars = [item for item in rejected_entities if item.get("original_category") == "characters"]
    rejected_places = [item for item in rejected_entities if item.get("original_category") == "places"]
    rejected_orgs = [item for item in rejected_entities if item.get("original_category") == "organizations"]
    dialogue_stats = normalized_dialogue.get("statistics") or {}
    scene_stats = normalized_scenes.get("statistics") or {}
    return {
        "analysis_dir": str(analysis_dir),
        "normalized_output_dir": str(output_dir),
        "raw_input_hashes_before": raw_hashes_before,
        "raw_input_hashes_after": raw_hashes_after,
        "raw_input_hashes_match": raw_hashes_before == raw_hashes_after,
        "raw_counts": {
            "characters": len(raw_story.get("characters") or []),
            "places": len(raw_story.get("places") or []),
            "organizations": len(raw_story.get("organizations") or []),
            "dialogue": int(dialogue_stats.get("raw_count") or 0),
            "scenes": int(scene_stats.get("raw_count") or 0),
        },
        "canonical_counts": {
            "characters": len(active_chars),
            "places": len(active_places),
            "organizations": len(active_orgs),
        },
        "rejected_counts": {
            "characters": len(rejected_chars),
            "places": len(rejected_places),
            "organizations": len(rejected_orgs),
            "total": len(rejected_entities),
        },
        "alias_merge_count": len(alias_merges),
        "duplicate_dialogue_records_removed": len(duplicate_dialogue_records),
        "dialogue_speakers_resolved_to_canonical_ids": int(dialogue_stats.get("resolved_count") or 0),
        "unresolved_dialogue_speakers": int(dialogue_stats.get("unresolved_count") or 0),
        "rejected_generic_dialogue_labels": int(dialogue_stats.get("rejected_generic_labels") or 0),
        "scene_statistics": scene_stats,
        "invalid_references_found": len(validation_errors),
        "validation_errors": validation_errors,
        "alias_merges": alias_merges,
        "rejected_entities": rejected_entities,
        "duplicate_dialogue_records": duplicate_dialogue_records,
    }
