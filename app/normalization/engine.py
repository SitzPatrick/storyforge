from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..config import load_settings
from .aliases import build_resolution_index
from .dialogue import normalize_dialogue
from .entities import normalize_entities
from .report import build_report
from .scenes import normalize_scenes
from .validation import validate_normalized

RAW_FILENAMES = ["story.json", "entities.json", "dialogue.json", "scenes.json", "cache.json"]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _canonical_story_copy(raw_story: dict, normalized_entities: dict, normalized_scenes: dict, output_dir: Path, analysis_dir: Path, raw_hashes_before: dict[str, str], raw_hashes_after: dict[str, str]) -> dict:
    story = dict(raw_story)
    story["characters"] = normalized_entities.get("characters") or []
    story["places"] = normalized_entities.get("places") or []
    story["organizations"] = normalized_entities.get("organizations") or []
    story["scenes"] = normalized_scenes.get("scenes") or []
    story["normalization"] = {
        "normalized": True,
        "analysis_dir": str(analysis_dir),
        "output_dir": str(output_dir),
        "raw_input_hashes_before": raw_hashes_before,
        "raw_input_hashes_after": raw_hashes_after,
    }
    return story


def normalize_analysis(analysis_dir: str | Path, output_dir: str | Path | None = None, config_path: str | Path | None = None) -> dict:
    settings = load_settings(config_path)
    analysis_dir = Path(analysis_dir)
    if output_dir is None:
        output_dir = analysis_dir.parent / settings.normalization.output_dir_name
    output_dir = Path(output_dir)

    raw_paths = {name: analysis_dir / name for name in RAW_FILENAMES}
    raw_hashes_before = {name: _sha256_file(path) for name, path in raw_paths.items()}

    raw_story = _load_json(raw_paths["story.json"])
    raw_entities = _load_json(raw_paths["entities.json"])
    raw_dialogue = _load_json(raw_paths["dialogue.json"])
    raw_scenes = _load_json(raw_paths["scenes.json"])
    raw_cache = _load_json(raw_paths["cache.json"])

    normalized_entities_bundle = normalize_entities(raw_story, settings)
    character_index = build_resolution_index({"characters": normalized_entities_bundle["characters"]})
    place_index = build_resolution_index({"places": normalized_entities_bundle["places"]})
    organization_index = build_resolution_index({"organizations": normalized_entities_bundle["organizations"]})
    lookup_indexes = {
        "characters": character_index,
        "places": place_index,
        "organizations": organization_index,
    }
    normalized_dialogue_bundle = normalize_dialogue(raw_dialogue, character_index, settings)
    normalized_scenes_bundle = normalize_scenes(raw_scenes, lookup_indexes, settings)

    normalized_entities = {
        "characters": normalized_entities_bundle["characters"],
        "places": normalized_entities_bundle["places"],
        "organizations": normalized_entities_bundle["organizations"],
        "statistics": {
            "raw_counts": {
                "characters": len(raw_story.get("characters") or []),
                "places": len(raw_story.get("places") or []),
                "organizations": len(raw_story.get("organizations") or []),
            },
            "canonical_counts": {
                "characters": len(normalized_entities_bundle["characters"]),
                "places": len(normalized_entities_bundle["places"]),
                "organizations": len(normalized_entities_bundle["organizations"]),
            },
        },
        "rejected": normalized_entities_bundle["rejected"],
        "rejected_entities": normalized_entities_bundle["rejected"],
        "alias_merges": normalized_entities_bundle["alias_merges"],
    }

    normalized_story = _canonical_story_copy(raw_story, normalized_entities, normalized_scenes_bundle, output_dir, analysis_dir, raw_hashes_before, raw_hashes_before)
    validation_errors = validate_normalized(normalized_entities, normalized_dialogue_bundle, normalized_scenes_bundle)
    raw_hashes_after = {name: _sha256_file(path) for name, path in raw_paths.items()}
    if raw_hashes_after != raw_hashes_before:
        validation_errors = [*validation_errors, "raw input files changed during normalization"]

    normalized_story["normalization"]["raw_input_hashes_after"] = raw_hashes_after

    report = build_report(
        analysis_dir=analysis_dir,
        output_dir=output_dir,
        raw_hashes_before=raw_hashes_before,
        raw_hashes_after=raw_hashes_after,
        raw_story=raw_story,
        normalized_entities=normalized_entities,
        normalized_dialogue=normalized_dialogue_bundle,
        normalized_scenes=normalized_scenes_bundle,
        validation_errors=validation_errors,
        duplicate_dialogue_records=normalized_dialogue_bundle.get("removed_duplicates") or [],
        rejected_entities=normalized_entities_bundle["rejected"],
        alias_merges=normalized_entities_bundle["alias_merges"],
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "normalized_story.json").write_text(_dump_json(normalized_story), encoding="utf-8")
    (output_dir / "normalized_entities.json").write_text(_dump_json(normalized_entities), encoding="utf-8")
    (output_dir / "normalized_dialogue.json").write_text(_dump_json(normalized_dialogue_bundle), encoding="utf-8")
    (output_dir / "normalized_scenes.json").write_text(_dump_json(normalized_scenes_bundle), encoding="utf-8")
    (output_dir / "normalization_report.json").write_text(_dump_json(report), encoding="utf-8")

    result = {
        "analysis_dir": str(analysis_dir),
        "output_dir": str(output_dir),
        "raw_hashes_before": raw_hashes_before,
        "raw_hashes_after": raw_hashes_after,
        "normalized_story": normalized_story,
        "normalized_entities": normalized_entities,
        "normalized_dialogue": normalized_dialogue_bundle,
        "normalized_scenes": normalized_scenes_bundle,
        "report": report,
        "validation_errors": validation_errors,
        "raw_cache": raw_cache,
        "raw_entities": raw_entities,
    }
    if validation_errors:
        result["status"] = "warning"
    else:
        result["status"] = "ok"
    return result
