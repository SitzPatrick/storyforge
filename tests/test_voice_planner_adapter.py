from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.voice_planner import CharacterProfileAdapterError, canonical_json_dumps, hash_phase3b_inputs, load_character_profiles, serialize_character_profiles, validate_character_profile_bundle

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "normalized_analysis_sample"


def _copy_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "analysis_normalized"
    shutil.copytree(FIXTURE_DIR, target)
    return target


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_character_profiles_load_and_are_deterministic(tmp_path: Path):
    analysis_dir = _copy_fixture(tmp_path)
    before_hashes = hash_phase3b_inputs(analysis_dir)

    bundle = load_character_profiles(analysis_dir)
    after_hashes = hash_phase3b_inputs(analysis_dir)

    assert before_hashes == after_hashes
    assert bundle.schema_version == 1
    assert bundle.book_id == "merchant-of-death"
    assert bundle.series_id == "pendragon"
    assert bundle.statistics["character_count"] == 4
    assert bundle.statistics["dialogue_record_count"] == 7
    assert bundle.statistics["attributed_dialogue_record_count"] == 6
    assert bundle.statistics["unresolved_dialogue_record_count"] == 1

    profile_ids = [profile.canonical_character_id for profile in bundle.profiles]
    assert profile_ids == ["bobby-pendragon", "courtney-chetwynde", "uncle-press", "sirah"]

    bobby = bundle.profiles[0]
    assert bobby.canonical_name == "Bobby Pendragon"
    assert bobby.role == "protagonist"
    assert bobby.prominence == "major recurring"
    assert bobby.speaking_frequency == 2
    assert bobby.dialogue_count == 2
    assert bobby.scene_count == 2
    assert bobby.first_appearance_order == 1
    assert bobby.likely_recurrence is True
    assert bobby.age_bucket == "teen"
    assert bobby.gender_presentation == "male"
    assert bobby.species_or_archetype == "human"
    assert bobby.source_aliases == ["Bobby", "Pendragon"]
    assert bobby.unresolved_metadata == {"home_world": "unknown"}
    assert bobby.source_provenance["scene_ids"] == ["scene-001", "scene-002"]
    assert bobby.source_provenance["dialogue_ids"] == ["d1", "d3"]
    assert bobby.source_provenance["source_analysis_hash"] == "045ffec1589c48c2e33f48c37460b472c38363328611aabb69d4db239a3858e8"

    courtney = bundle.profiles[1]
    assert courtney.scene_count == 2
    assert courtney.dialogue_count == 2
    assert courtney.first_appearance_order == 1
    assert courtney.unresolved_metadata == {"age_bucket": "unknown"}

    uncle = bundle.profiles[2]
    assert uncle.dialogue_count == 2
    assert uncle.scene_count == 2
    assert uncle.first_appearance_order == 2
    assert [rel.related_character_id for rel in uncle.scene_relationships] == ["bobby-pendragon", "courtney-chetwynde", "sirah"]
    assert uncle.scene_relationships[0].shared_scene_count == 1
    assert uncle.scene_relationships[0].shared_speaking_scene_count == 1
    assert uncle.scene_relationships[0].first_shared_scene_id == "scene-002"

    sirah = bundle.profiles[3]
    assert sirah.dialogue_count == 0
    assert sirah.scene_count == 1
    assert sirah.likely_recurrence is None
    assert sirah.gender_presentation is None
    assert sirah.unresolved_metadata["species_or_archetype"] == "unknown"

    serialized_1 = serialize_character_profiles(bundle)
    serialized_2 = serialize_character_profiles(bundle)
    assert serialized_1 == serialized_2
    assert json.loads(serialized_1)["profiles"][0]["canonical_character_id"] == "bobby-pendragon"
    assert validate_character_profile_bundle(json.loads(serialized_1)) == []


def test_missing_required_normalized_file_fails_clearly(tmp_path: Path):
    analysis_dir = _copy_fixture(tmp_path)
    (analysis_dir / "normalized_scenes.json").unlink()
    with pytest.raises(CharacterProfileAdapterError, match="missing required normalized file: normalized_scenes.json"):
        load_character_profiles(analysis_dir)


def test_malformed_json_fails_clearly(tmp_path: Path):
    analysis_dir = _copy_fixture(tmp_path)
    (analysis_dir / "normalized_dialogue.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CharacterProfileAdapterError, match="malformed JSON in normalized_dialogue.json"):
        load_character_profiles(analysis_dir)


def test_input_hash_helper_tracks_all_phase3b_files(tmp_path: Path):
    analysis_dir = _copy_fixture(tmp_path)
    hashes = hash_phase3b_inputs(analysis_dir)
    assert list(hashes) == [
        "normalization_report.json",
        "normalized_dialogue.json",
        "normalized_entities.json",
        "normalized_scenes.json",
        "normalized_story.json",
    ]
    for name, digest in hashes.items():
        assert digest == _hash_file(analysis_dir / name)
