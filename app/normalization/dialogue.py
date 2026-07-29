from __future__ import annotations

from collections.abc import Iterable

from .filters import build_reject_index, normalize_key, normalize_text, rejection_rule, source_reference_id, unique_preserve

_UNKNOWN_VALUES = {"unknown", "not specified", "none", "n/a"}


def _dedup_key(record: dict) -> tuple:
    quoted = normalize_text(record.get("quoted_text"))
    speaker = normalize_text(record.get("speaker"))
    return (
        int(record.get("chapter") or -1),
        int(record.get("paragraph_index") or -1),
        str(record.get("source_document_id") or ""),
        str(record.get("source_text_hash") or ""),
        quoted,
        speaker,
    )


def _resolve_speaker(original_speaker: str | None, lookup_index: dict[str, dict], reject_index: set[str]) -> dict:
    text = normalize_text(original_speaker)
    key = normalize_key(text)
    if not text or key in _UNKNOWN_VALUES:
        return {
            "speaker_id": None,
            "speaker_name": None,
            "speaker_confidence": 0.0,
            "speaker_resolution": "unknown",
        }

    match = lookup_index.get(key)
    if match:
        return {
            "speaker_id": match["canonical_id"],
            "speaker_name": match["canonical_name"],
            "speaker_confidence": float(match.get("confidence", 1.0)),
            "speaker_resolution": match.get("match_type") or match.get("speaker_resolution") or "canonical_match",
        }

    stripped = normalize_key(text.replace("\u2019", "'"))
    if stripped in lookup_index:
        match = lookup_index[stripped]
        return {
            "speaker_id": match["canonical_id"],
            "speaker_name": match["canonical_name"],
            "speaker_confidence": float(match.get("confidence", 1.0)),
            "speaker_resolution": match.get("match_type") or match.get("speaker_resolution") or "canonical_match",
        }

    if rejection_rule(text, reject_index) or text.lower() == text:
        return {
            "speaker_id": None,
            "speaker_name": None,
            "speaker_confidence": 0.0,
            "speaker_resolution": "rejected_generic_label",
        }

    return {
        "speaker_id": None,
        "speaker_name": None,
        "speaker_confidence": 0.0,
        "speaker_resolution": "unknown",
    }


def normalize_dialogue(dialogue: list[dict], lookup_index: dict[str, dict], settings) -> dict:
    reject_index = build_reject_index(settings.normalization.rejection_labels.get("characters", []))
    deduplicate = bool(settings.normalization.deduplicate_dialogue)
    normalized: list[dict] = []
    removed_duplicates: list[dict] = []
    seen_keys: set[tuple] = set()
    resolved_count = 0
    unresolved_count = 0
    rejected_count = 0

    for record in dialogue:
        key = _dedup_key(record)
        if deduplicate and key in seen_keys:
            removed_duplicates.append({
                "chapter": record.get("chapter"),
                "paragraph_index": record.get("paragraph_index"),
                "quoted_text": record.get("quoted_text"),
                "speaker": record.get("speaker"),
                "source_text_hash": record.get("source_text_hash"),
            })
            continue
        seen_keys.add(key)

        speaker_info = _resolve_speaker(record.get("speaker"), lookup_index, reject_index)
        if speaker_info["speaker_id"]:
            resolved_count += 1
        elif speaker_info["speaker_resolution"] == "rejected_generic_label":
            rejected_count += 1
        else:
            unresolved_count += 1

        normalized.append(
            {
                "chapter": record.get("chapter"),
                "paragraph_index": record.get("paragraph_index"),
                "quoted_text": record.get("quoted_text"),
                "source_document_id": record.get("source_document_id"),
                "source_reference": record.get("source_reference"),
                "source_text_hash": record.get("source_text_hash"),
                "speaker": record.get("speaker"),
                "original_speaker": record.get("speaker"),
                **speaker_info,
            }
        )

    return {
        "dialogue": normalized,
        "statistics": {
            "raw_count": len(dialogue),
            "normalized_count": len(normalized),
            "removed_duplicates": len(removed_duplicates),
            "resolved_count": resolved_count,
            "unresolved_count": unresolved_count,
            "rejected_generic_labels": rejected_count,
        },
        "removed_duplicates": removed_duplicates,
    }
