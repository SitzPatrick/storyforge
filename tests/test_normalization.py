from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.normalization import normalize_analysis

REJECTED_NAMES = {
    "I",
    "me",
    "my",
    "mine",
    "myself",
    "you",
    "your",
    "yours",
    "yourself",
    "yourselves",
    "he",
    "him",
    "his",
    "himself",
    "she",
    "her",
    "hers",
    "herself",
    "we",
    "us",
    "our",
    "ours",
    "ourselves",
    "they",
    "them",
    "their",
    "theirs",
    "themselves",
    "it",
    "its",
    "itself",
    "someone",
    "somebody",
    "anyone",
    "anybody",
    "everyone",
    "everybody",
    "person",
    "man",
    "woman",
    "girl",
    "boy",
    "people",
    "villagers",
    "miners",
    "knight",
    "here",
    "there",
    "place",
    "unknown",
    "unknown location",
    "not specified",
    "none",
    "n/a",
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_ref(label: str, chapter: int, paragraph: int) -> dict:
    digest = sha256_text(label)
    return {
        "chapter": chapter,
        "excerpt": f"excerpt for {label}",
        "paragraph_index": paragraph,
        "source_document_id": digest,
        "source_text_hash": digest,
    }


def make_entity(name: str, category: str, chapter: int, paragraph: int, *, dialogue_count: int = 0, narration_mentions: int = 0) -> dict:
    digest = sha256_text(f"{category}:{name}:{chapter}:{paragraph}")
    return {
        "id": f"{category}_{digest[:10]}",
        "name": name,
        "aliases": [],
        "age": None,
        "gender": None,
        "role": None,
        "first_chapter": chapter,
        "chapters": [chapter],
        "dialogue_count": dialogue_count,
        "narration_mentions": narration_mentions,
        "source_references": [make_ref(name, chapter, paragraph)],
    }


def write_sample_analysis(tmp_path: Path) -> Path:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()

    characters = [
        make_entity("Bobby", "characters", 1, 1, dialogue_count=3, narration_mentions=7),
        make_entity("Bobby Pendragon", "characters", 1, 2, dialogue_count=4, narration_mentions=8),
        make_entity("Pendragon", "characters", 1, 3, dialogue_count=1, narration_mentions=2),
        make_entity("Mark", "characters", 1, 4, dialogue_count=2, narration_mentions=3),
        make_entity("Mark Dimond", "characters", 1, 5, dialogue_count=2, narration_mentions=5),
        make_entity("Courtney", "characters", 1, 6, dialogue_count=2, narration_mentions=3),
        make_entity("Courtney Chetwynde", "characters", 1, 7, dialogue_count=2, narration_mentions=4),
        make_entity("Uncle Press", "characters", 1, 8, dialogue_count=4, narration_mentions=6),
        make_entity("Press", "characters", 1, 9, dialogue_count=1, narration_mentions=2),
        make_entity("I", "characters", 1, 10, dialogue_count=2, narration_mentions=3),
        make_entity("He", "characters", 1, 11, dialogue_count=1, narration_mentions=3),
        make_entity("She", "characters", 1, 12, dialogue_count=1, narration_mentions=2),
        make_entity("you", "characters", 1, 13, dialogue_count=1, narration_mentions=1),
        make_entity("they", "characters", 1, 14, dialogue_count=1, narration_mentions=1),
        make_entity("me", "characters", 1, 15, dialogue_count=1, narration_mentions=1),
        make_entity("we", "characters", 1, 16, dialogue_count=1, narration_mentions=1),
        make_entity("us", "characters", 1, 17, dialogue_count=1, narration_mentions=1),
        make_entity("miners", "characters", 1, 18, dialogue_count=1, narration_mentions=1),
        make_entity("villagers", "characters", 1, 19, dialogue_count=1, narration_mentions=1),
        make_entity("knight", "characters", 1, 20, dialogue_count=1, narration_mentions=1),
        make_entity("someone", "characters", 1, 21, dialogue_count=1, narration_mentions=1),
        make_entity("the girl", "characters", 1, 22, dialogue_count=1, narration_mentions=1),
        make_entity("a woman", "characters", 1, 23, dialogue_count=1, narration_mentions=1),
        make_entity("heavy man", "characters", 1, 24, dialogue_count=1, narration_mentions=1),
        make_entity("the quig", "characters", 1, 25, dialogue_count=1, narration_mentions=1),
        make_entity("here", "characters", 1, 26, dialogue_count=1, narration_mentions=1),
        make_entity("place", "characters", 1, 27, dialogue_count=1, narration_mentions=1),
        make_entity("unknown location", "characters", 1, 28, dialogue_count=1, narration_mentions=1),
        make_entity("Not specified", "characters", 1, 29, dialogue_count=1, narration_mentions=1),
        make_entity("Loor", "characters", 1, 30, dialogue_count=4, narration_mentions=8),
        make_entity("Osa", "characters", 1, 31, dialogue_count=3, narration_mentions=5),
        make_entity("Alder", "characters", 1, 32, dialogue_count=3, narration_mentions=6),
        make_entity("Rellin", "characters", 1, 33, dialogue_count=2, narration_mentions=4),
        make_entity("Figgis", "characters", 1, 34, dialogue_count=2, narration_mentions=3),
        make_entity("Kagan", "characters", 1, 35, dialogue_count=2, narration_mentions=4),
    ]

    places = [
        make_entity("2 Linden Place", "places", 1, 40),
        make_entity("The Milago", "places", 1, 41),
        make_entity("Milago", "places", 1, 42),
        make_entity("Denduron", "places", 1, 43),
        make_entity("here", "places", 1, 44),
        make_entity("unknown location", "places", 1, 45),
        make_entity("Not specified", "places", 1, 46),
    ]

    organizations = [
        make_entity("Pendragon", "organizations", 1, 50),
        make_entity("Bedoowan", "organizations", 1, 51),
        make_entity("Knights", "organizations", 1, 52),
        make_entity("The Milago", "organizations", 1, 53),
        make_entity("Milago", "organizations", 1, 54),
    ]

    story = {
        "title": "The Merchant of Death",
        "author": "D.J. MacHale",
        "source_document_id": sha256_text("merchant-of-death"),
        "source_signature": {"source_hash": sha256_text("merchant-of-death")},
        "characters": characters,
        "places": places,
        "organizations": organizations,
        "scenes": [
            {
                "chapter": 1,
                "scene_number": 1,
                "start_paragraph": 1,
                "end_paragraph": 20,
                "summary": "Bobby meets Uncle Press and heads into the early conflict.",
                "participating_characters": ["Bobby", "Uncle Press", "he", "the girl"],
                "locations": ["2 Linden Place", "here", "unknown location"],
                "organizations": ["Pendragon", "The Milago"],
                "source_document_id": sha256_text("merchant-of-death"),
                "source_reference": make_ref("scene-1", 1, 20),
                "source_text_hash": sha256_text("scene-1"),
            },
            {
                "chapter": 2,
                "scene_number": 2,
                "start_paragraph": 21,
                "end_paragraph": 40,
                "summary": "Courtney and Mark appear as the situation broadens.",
                "participating_characters": ["Courtney", "Mark", "Press", "someone"],
                "locations": ["Denduron", "Not specified"],
                "organizations": ["Bedoowan", "Knights"],
                "source_document_id": sha256_text("merchant-of-death"),
                "source_reference": make_ref("scene-2", 2, 40),
                "source_text_hash": sha256_text("scene-2"),
            },
        ],
        "dialogue": [],
    }

    dialogue = [
        {
            "chapter": 1,
            "paragraph_index": 345,
            "quoted_text": "Traveler.",
            "speaker": "Bobby",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("traveler-dup", 1, 345),
            "source_text_hash": sha256_text("traveler-dup"),
        },
        {
            "chapter": 1,
            "paragraph_index": 345,
            "quoted_text": "Traveler.",
            "speaker": "Bobby",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("traveler-dup", 1, 345),
            "source_text_hash": sha256_text("traveler-dup"),
        },
        {
            "chapter": 1,
            "paragraph_index": 400,
            "quoted_text": "Traveler.",
            "speaker": "Mark",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("traveler-separate", 1, 400),
            "source_text_hash": sha256_text("traveler-separate"),
        },
        {
            "chapter": 2,
            "paragraph_index": 527,
            "quoted_text": "Denduron,",
            "speaker": "unknown",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("denduron-dup", 2, 527),
            "source_text_hash": sha256_text("denduron-dup"),
        },
        {
            "chapter": 2,
            "paragraph_index": 527,
            "quoted_text": "Denduron,",
            "speaker": "unknown",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("denduron-dup", 2, 527),
            "source_text_hash": sha256_text("denduron-dup"),
        },
        {
            "chapter": 2,
            "paragraph_index": 528,
            "quoted_text": "Let’s go!",
            "speaker": "you",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("lets-go", 2, 528),
            "source_text_hash": sha256_text("lets-go"),
        },
        {
            "chapter": 3,
            "paragraph_index": 100,
            "quoted_text": "I need to get back.",
            "speaker": "I",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("need-back", 3, 100),
            "source_text_hash": sha256_text("need-back"),
        },
        {
            "chapter": 3,
            "paragraph_index": 101,
            "quoted_text": "We should hurry.",
            "speaker": "we",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("should-hurry", 3, 101),
            "source_text_hash": sha256_text("should-hurry"),
        },
        {
            "chapter": 3,
            "paragraph_index": 102,
            "quoted_text": "Press will know.",
            "speaker": "Press",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("press-knows", 3, 102),
            "source_text_hash": sha256_text("press-knows"),
        },
        {
            "chapter": 3,
            "paragraph_index": 103,
            "quoted_text": "Bobby knows.",
            "speaker": "Bobby Pendragon",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("bobby-knows", 3, 103),
            "source_text_hash": sha256_text("bobby-knows"),
        },
        {
            "chapter": 3,
            "paragraph_index": 104,
            "quoted_text": "Unclear.",
            "speaker": "unknown",
            "source_document_id": sha256_text("merchant-of-death"),
            "source_reference": make_ref("unclear", 3, 104),
            "source_text_hash": sha256_text("unclear"),
        },
    ]

    raw = dict(story)
    raw["dialogue"] = dialogue

    for name in ["story.json", "entities.json", "dialogue.json", "scenes.json", "cache.json"]:
        if name == "story.json":
            payload = raw
        elif name == "entities.json":
            payload = {"characters": characters, "places": places, "organizations": organizations}
        elif name == "dialogue.json":
            payload = dialogue
        elif name == "scenes.json":
            payload = story["scenes"]
        else:
            payload = {"analysis_version": "test", "source_hash": sha256_text("merchant-of-death")}
        (analysis_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    return analysis_dir


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_normalization_rejects_aliases_and_deduplicates(tmp_path: Path):
    analysis_dir = write_sample_analysis(tmp_path)
    output_dir = tmp_path / "analysis_normalized"
    raw_hashes_before = {name: hash_file(analysis_dir / name) for name in ["story.json", "entities.json", "dialogue.json", "scenes.json", "cache.json"]}

    result = normalize_analysis(analysis_dir, output_dir)
    assert result["validation_errors"] == []
    assert result["raw_hashes_before"] == result["raw_hashes_after"]

    normalized_entities = read_json(output_dir / "normalized_entities.json")
    normalized_dialogue = read_json(output_dir / "normalized_dialogue.json")
    normalized_scenes = read_json(output_dir / "normalized_scenes.json")
    report = read_json(output_dir / "normalization_report.json")

    active_character_names = {item["canonical_name"] for item in normalized_entities["characters"]}
    assert {"Bobby Pendragon", "Mark Dimond", "Courtney Chetwynde", "Uncle Press"}.issubset(active_character_names)
    assert not (REJECTED_NAMES & active_character_names)

    rejected_names = {item["original_name"] for item in report["rejected_entities"]}
    assert "I" in rejected_names
    assert "miners" in rejected_names
    assert "unknown location" in rejected_names

    alias_merge_map = {item["canonical_name"]: item for item in report["alias_merges"]}
    assert alias_merge_map["Bobby Pendragon"]["aliases"]
    assert alias_merge_map["Mark Dimond"]["aliases"] == ["Mark"]
    assert alias_merge_map["Courtney Chetwynde"]["aliases"] == ["Courtney"]
    assert alias_merge_map["Uncle Press"]["aliases"] == ["Press"]

    dialogue_records = normalized_dialogue["dialogue"]
    assert sum(1 for item in dialogue_records if item["quoted_text"] == "Traveler." and item["chapter"] == 1 and item["paragraph_index"] == 345) == 1
    assert sum(1 for item in dialogue_records if item["quoted_text"] == "Denduron," and item["chapter"] == 2 and item["paragraph_index"] == 527) == 1
    assert any(item["speaker_resolution"] == "unknown" and item["speaker_id"] is None for item in dialogue_records)
    assert any(item["speaker_resolution"] == "alias_match" and item["speaker_name"] == "Uncle Press" for item in dialogue_records)
    assert any(item["speaker_resolution"] == "canonical_match" and item["speaker_name"] == "Bobby Pendragon" for item in dialogue_records)

    scene0 = normalized_scenes["scenes"][0]
    assert any(item["name"] == "Bobby Pendragon" for item in scene0["characters"])
    assert any(item["name"] == "Uncle Press" for item in scene0["characters"])
    assert any(item["name"] == "2 Linden Place" for item in scene0["places"])
    assert any(item["name"] == "Milago" for item in scene0["organizations"])
    assert "Pendragon" in scene0["unresolved_organization_labels"]
    assert scene0["unresolved_character_labels"]
    assert scene0["unresolved_place_labels"]

    assert report["raw_input_hashes_match"] is True
    assert report["duplicate_dialogue_records_removed"] == 2
    assert report["dialogue_speakers_resolved_to_canonical_ids"] >= 2
    assert report["unresolved_dialogue_speakers"] >= 1
    assert report["invalid_references_found"] == 0
    assert raw_hashes_before == report["raw_input_hashes_before"] == report["raw_input_hashes_after"]


def test_normalization_is_deterministic_and_raw_files_untouched(tmp_path: Path):
    analysis_dir = write_sample_analysis(tmp_path)
    output_dir = tmp_path / "analysis_normalized"
    raw_hashes_before = {name: hash_file(analysis_dir / name) for name in ["story.json", "entities.json", "dialogue.json", "scenes.json", "cache.json"]}

    first = normalize_analysis(analysis_dir, output_dir)
    first_hashes = {path.name: hash_file(path) for path in sorted(output_dir.glob("*.json"))}

    second = normalize_analysis(analysis_dir, output_dir)
    second_hashes = {path.name: hash_file(path) for path in sorted(output_dir.glob("*.json"))}

    assert first["raw_hashes_before"] == first["raw_hashes_after"] == raw_hashes_before
    assert second["raw_hashes_before"] == second["raw_hashes_after"] == raw_hashes_before
    assert first_hashes == second_hashes

    # Byte-identical outputs on repeat run.
    for path in sorted(output_dir.glob("*.json")):
        assert path.read_bytes() == path.read_bytes()


def test_scene_and_dialogue_resolution_stays_conservative(tmp_path: Path):
    analysis_dir = write_sample_analysis(tmp_path)
    output_dir = tmp_path / "analysis_normalized"
    result = normalize_analysis(analysis_dir, output_dir)

    normalized_entities = result["normalized_entities"]
    normalized_dialogue = result["normalized_dialogue"]
    normalized_scenes = result["normalized_scenes"]

    character_ids = {item["id"] for item in normalized_entities["characters"]}
    place_ids = {item["id"] for item in normalized_entities["places"]}
    organization_ids = {item["id"] for item in normalized_entities["organizations"]}

    for record in normalized_dialogue["dialogue"]:
        if record["speaker_id"] is not None:
            assert record["speaker_id"] in character_ids
        else:
            assert record["speaker_resolution"] in {"unknown", "rejected_generic_label"}

    for scene in normalized_scenes["scenes"]:
        for item in scene["characters"]:
            assert item["id"] in character_ids
        for item in scene["places"]:
            assert item["id"] in place_ids
        for item in scene["organizations"]:
            assert item["id"] in organization_ids

    assert any(record["speaker_resolution"] == "rejected_generic_label" for record in normalized_dialogue["dialogue"])
    assert any(record["speaker_resolution"] == "unknown" for record in normalized_dialogue["dialogue"])
