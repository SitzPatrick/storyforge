from __future__ import annotations

import copy
import json
import os
import wave
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from app.assembler import CHAPTER_ASSEMBLER_VERSION, ChapterAssemblyConfig, ChapterAssemblyError, ChapterSpacingConfig, assemble_chapters
from app.renderer.cache import build_render_cache_key
from app.voice_planner import CharacterPlan, EditableVoicePlan, NarratorPlan, VoiceAssignment, VoicePlan, build_synthesis_manifest


def _assignment(provider: str, provider_voice_id: str, *, source: str = "global optimum", continuity_status: str = "new-assignment") -> VoiceAssignment:
    return VoiceAssignment(
        voice_id=f"{provider}.{provider_voice_id}",
        provider=provider,
        provider_voice_id=provider_voice_id,
        locked=False,
        source=source,
        continuity_status=continuity_status,
        rationale="test",
        notes=None,
        generated=True,
    )


def _voice_plan() -> VoicePlan:
    return VoicePlan(
        schema_version=1,
        planner_version="test-planner",
        book_id="book-9",
        series_id="series-9",
        source_analysis_hash="analysis-hash",
        source_analysis_path="/tmp/analysis.json",
        narrator=NarratorPlan(assignment=_assignment("beta", "narrator"), rationale="narrator test"),
        characters=[
            CharacterPlan(canonical_character_id="ada", canonical_name="Ada", role="protagonist", prominence="major", speaking_frequency=10, first_appearance=1, likely_recurrence=True, assignment=_assignment("alpha", "v1")),
            CharacterPlan(canonical_character_id="ben", canonical_name="Ben", role="supporting", prominence="secondary", speaking_frequency=4, first_appearance=2, likely_recurrence=True, assignment=_assignment("beta", "v2")),
        ],
        warnings=[],
        statistics={"total_characters": 2},
        user_editable_notes=[],
    )


def _voice_registry() -> dict[str, object]:
    return {
        "schema_version": 1,
        "registry_version": "test",
        "voices": [
            {"schema_version": 1, "voice_id": "alpha.v1", "provider": "alpha", "provider_voice_id": "v1", "display_name": "Alpha V1", "availability": "available", "quality_score": 0.95, "base_priority": 100, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
            {"schema_version": 1, "voice_id": "beta.v2", "provider": "beta", "provider_voice_id": "v2", "display_name": "Beta V2", "availability": "available", "quality_score": 0.92, "base_priority": 90, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
            {"schema_version": 1, "voice_id": "beta.narrator", "provider": "beta", "provider_voice_id": "narrator", "display_name": "Beta Narrator", "availability": "available", "quality_score": 0.93, "base_priority": 95, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
        ],
    }


def _story(*, reordered: bool = False) -> dict[str, object]:
    segments = [
        {"segment_id": "narration-1", "segment_type": "narration", "scene_id": "scene-1", "chapter": 1, "source_order": 1, "source_text": "The morning light filled the room.", "synthesis_text": "The morning light filled the room.", "source_text_hash": "n1", "source_reference": {"chapter": 1, "paragraph_index": 1, "source_document_id": "book-9", "source_text_hash": "n1", "excerpt": "The morning light filled the room."}},
        {"segment_id": "dialogue-1", "segment_type": "dialogue", "scene_id": "scene-1", "chapter": 1, "source_order": 2, "speaker": "Ada", "speaker_type": "character", "source_text": '"We should go now," Ada said.', "synthesis_text": 'We should go now.', "source_text_hash": "d1", "source_reference": {"chapter": 1, "paragraph_index": 2, "source_document_id": "book-9", "source_text_hash": "d1", "excerpt": '"We should go now," Ada said.'}, "controls": {"rate": 1.0}},
        {"segment_id": "narration-2", "segment_type": "narration", "scene_id": "scene-2", "chapter": 2, "source_order": 3, "source_text": "Ben nodded in agreement.", "synthesis_text": "Ben nodded in agreement.", "source_text_hash": "n2", "source_reference": {"chapter": 2, "paragraph_index": 1, "source_document_id": "book-9", "source_text_hash": "n2", "excerpt": "Ben nodded in agreement."}},
        {"segment_id": "narration-3", "segment_type": "narration", "scene_id": "scene-2", "chapter": 2, "source_order": 4, "source_text": "A new paragraph appeared.", "synthesis_text": "A new paragraph appeared.", "source_text_hash": "n3", "source_reference": {"chapter": 2, "paragraph_index": 2, "source_document_id": "book-9", "source_text_hash": "n3", "excerpt": "A new paragraph appeared."}},
    ]
    if reordered:
        segments = [segments[2], segments[0], segments[3], segments[1]]
    return {
        "schema_version": 1,
        "book_id": "book-9",
        "series_id": "series-9",
        "title": "River City Nights",
        "author": "Test Author",
        "language": "en",
        "source_analysis_hash": "analysis-hash",
        "source_analysis_path": "/tmp/analysis.json",
        "source_document_id": "book-9",
        "source_signature": {"sha256": "analysis-hash"},
        "characters": [
            {"canonical_character_id": "ada", "canonical_name": "Ada", "aliases": [], "source_aliases": []},
            {"canonical_character_id": "ben", "canonical_name": "Ben", "aliases": [], "source_aliases": []},
        ],
        "scenes": [
            {"scene_id": "scene-1", "chapter": 1, "scene_number": 1, "start_paragraph": 1, "end_paragraph": 2, "summary": "Ada speaks with Ben.", "source_document_id": "book-9", "source_text_hash": "scene-1"},
            {"scene_id": "scene-2", "chapter": 2, "scene_number": 1, "start_paragraph": 1, "end_paragraph": 2, "summary": "Ben reflects.", "source_document_id": "book-9", "source_text_hash": "scene-2"},
        ],
        "segments": segments,
        "source_artifacts": {"normalized_story": "analysis/normalized_story.json", "story": "analysis/story.json"},
    }


def _config(*, spacing: ChapterSpacingConfig | None = None, fallback: str = "reject") -> ChapterAssemblyConfig:
    return ChapterAssemblyConfig(
        assembly_root=Path("/tmp/storyforge-assembly"),
        segment_root=Path("/tmp/storyforge-segments"),
        assembly_contract_version=1,
        assembler_version=CHAPTER_ASSEMBLER_VERSION,
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
        fallback_chapter_mode=fallback,
        empty_chapter_policy="reject",
        missing_segment_policy="block",
        spacing=spacing or ChapterSpacingConfig(chapter_start_ms=0, chapter_end_ms=0, narration_to_dialogue_ms=125, dialogue_to_narration_ms=250, narration_to_narration_ms=75, dialogue_to_dialogue_ms=60, scene_boundary_ms=40, default_between_segments_ms=33),
    )


def _manifest(*, reordered: bool = False):
    return build_synthesis_manifest(_story(reordered=reordered), _voice_plan(), _voice_registry(), {"voice_planner": {"schema_version": 1, "renderer_contract_version": 1, "default_unresolved_speaker_policy": "reject", "manifest_filename": "synthesis_manifest.json"}}, unresolved_speaker_policy="reject").manifest


def _make_wav(path: Path, *, duration_ms: int, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> bytes:
    frame_count = max(1, round(sample_rate * duration_ms / 1000))
    payload = bytes((index % 253 for index in range(frame_count * channels * sample_width)))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)
    return path.read_bytes()


def _segment_sidecar(manifest, unit, audio_path: Path, audio_bytes: bytes, *, duration_ms: int, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> dict[str, object]:
    frame_count = max(1, round(sample_rate * duration_ms / 1000))
    duration_seconds = frame_count / sample_rate
    return {
        "render_unit_id": unit.render_unit_id,
        "canonical_segment_id": unit.canonical_segment_id,
        "synthesis_input_hash": unit.synthesis_input_hash,
        "renderer_contract_version": manifest.renderer_contract_version,
        "provider": unit.assigned_provider,
        "provider_voice_id": unit.assigned_provider_voice_id,
        "provider_adapter_version": "kokoro-adapter-1",
        "model_version": "model-v1",
        "output_format": "wav",
        "sample_rate_hz": sample_rate,
        "channel_count": channels,
        "sample_width_bytes": sample_width,
        "deterministic_seed": None,
        "manifest_content_hash": manifest.manifest_content_hash,
        "cache_key": build_render_cache_key(
            {
                "render_unit_id": unit.render_unit_id,
                "synthesis_input_hash": unit.synthesis_input_hash,
                "renderer_contract_version": manifest.renderer_contract_version,
                "provider": unit.assigned_provider,
                "provider_voice_id": unit.assigned_provider_voice_id,
                "provider_adapter_version": "kokoro-adapter-1",
                "model_version": "model-v1",
                "output_format": "wav",
                "sample_rate_hz": sample_rate,
                "channel_count": channels,
                "sample_width_bytes": sample_width,
                "deterministic_seed": None,
            }
        ),
        "artifact_relative_path": unit.output_artifact_key,
        "validation_result": "passed",
        "attempt_outcome": "rendered",
        "warnings": [],
        "errors": [],
        "audio_content_hash": sha256(audio_bytes).hexdigest(),
        "frame_count": frame_count,
        "duration_seconds": duration_seconds,
    }


def _write_segments(manifest, segment_root: Path, *, durations_ms: dict[str, int] | None = None) -> None:
    durations_ms = durations_ms or {}
    for unit in manifest.render_units:
        duration_ms = durations_ms.get(unit.render_unit_id, 60 if unit.segment_type == "narration" else 80)
        audio_path = segment_root / unit.output_artifact_key
        audio_bytes = _make_wav(audio_path, duration_ms=duration_ms)
        sidecar = _segment_sidecar(manifest, unit, audio_path, audio_bytes, duration_ms=duration_ms)
        sidecar_path = Path(str(audio_path) + ".json")
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(json.dumps(sidecar, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

def _chapter_structure(manifest) -> dict[str, object]:
    units = sorted(manifest.render_units, key=lambda unit: unit.source_order)
    return {
        "chapters": [
            {
                "chapter_id": "chapter-1",
                "chapter_order": 1,
                "chapter_title": "Chapter 1",
                "source_section_id": "section-1",
                "render_unit_ids": [units[0].render_unit_id, units[1].render_unit_id],
            },
            {
                "chapter_id": "chapter-2",
                "chapter_order": 2,
                "chapter_title": "Chapter 2",
                "source_section_id": "section-2",
                "render_unit_ids": [units[2].render_unit_id, units[3].render_unit_id],
            },
        ]
    }


def _assembly_root(tmp_path: Path) -> ChapterAssemblyConfig:
    config = _config()
    return replace(config, assembly_root=tmp_path / "assembly", segment_root=tmp_path / "segments")


def _run(manifest, tmp_path: Path, *, structure: dict[str, object] | None = None, config: ChapterAssemblyConfig | None = None):
    config = config or _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    return assemble_chapters(manifest, chapter_structure_source=structure or _chapter_structure(manifest), config=config)


def test_basic_chapter_assembly_writes_deterministic_audio_and_sidecar(tmp_path: Path):
    manifest = _manifest()
    report = _run(manifest, tmp_path)

    assert report.total_chapters == 2
    assert report.completed_chapters == 2
    assert report.cache_hit_chapters == 0
    assert report.newly_assembled_chapters == 2
    assert report.blocked_chapters == 0
    assert report.completion_status == "complete"
    assert [result.chapter_order for result in report.chapter_results] == [1, 2]
    first = report.chapter_results[0]
    wav_path = Path(first.output_artifact_path)
    sidecar_path = Path(first.sidecar_path)
    assert wav_path.exists()
    assert sidecar_path.exists()
    assert first.status == "assembled"
    assert first.cache_hit is False
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert payload["chapter_id"] == "chapter-1"
    assert payload["validation_result"] == "passed"
    assert payload["chapter_input_hash"] == first.chapter_input_hash
    assert payload["ordered_render_unit_ids"] == list(first.render_unit_ids)
    assert payload["speech_frame_count"] + payload["silence_frame_count"] == payload["frame_count"]
    with wave.open(str(wav_path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 24000
        assert handle.getnframes() == payload["frame_count"]


def test_cache_hit_rerun_does_not_rewrite(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    first = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    chapter_path = Path(first.chapter_results[0].output_artifact_path)
    sidecar_path = Path(first.chapter_results[0].sidecar_path)
    chapter_mtime = chapter_path.stat().st_mtime_ns
    sidecar_mtime = sidecar_path.stat().st_mtime_ns

    second = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    assert second.cache_hit_chapters == 2
    assert chapter_path.stat().st_mtime_ns == chapter_mtime
    assert sidecar_path.stat().st_mtime_ns == sidecar_mtime
    assert [result.status for result in second.chapter_results] == ["cache-hit", "cache-hit"]


def test_changed_one_segment_reassembles_only_one_chapter(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    initial = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)

    changed_unit = sorted(manifest.render_units, key=lambda unit: unit.source_order)[2]
    changed_audio_path = config.segment_root / changed_unit.output_artifact_key
    changed_audio_bytes = _make_wav(changed_audio_path, duration_ms=140)
    changed_sidecar = _segment_sidecar(manifest, changed_unit, changed_audio_path, changed_audio_bytes, duration_ms=140)
    changed_sidecar_path = Path(str(changed_audio_path) + ".json")
    changed_sidecar_path.write_text(json.dumps(changed_sidecar, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    rerun = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    assert rerun.cache_hit_chapters == 1
    assert rerun.newly_assembled_chapters == 1
    assert [result.status for result in rerun.chapter_results] == ["cache-hit", "assembled"]
    assert Path(initial.chapter_results[0].output_artifact_path).read_bytes() == Path(rerun.chapter_results[0].output_artifact_path).read_bytes()


def test_notes_only_manifest_edit_preserves_cache_hits(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    notes_only_manifest = replace(manifest, manifest_content_hash="manifest-hash-notes-only")
    rerun = assemble_chapters(notes_only_manifest, chapter_structure_source=_chapter_structure(notes_only_manifest), config=config)
    assert rerun.cache_hit_chapters == 2
    assert rerun.newly_assembled_chapters == 0


def test_spacing_change_invalidates_only_affected_chapter(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)

    changed_spacing = replace(config.spacing, narration_to_dialogue_ms=250)
    rerun = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=replace(config, spacing=changed_spacing))
    assert rerun.newly_assembled_chapters >= 1
    assert rerun.completed_chapters == 2


def test_missing_segment_blocks_chapter_and_preserves_prior_artifact(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    initial = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    missing_unit = sorted(manifest.render_units, key=lambda unit: unit.source_order)[0]
    (config.segment_root / missing_unit.output_artifact_key).unlink()
    rerun = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    assert rerun.blocked_chapters == 1
    assert rerun.cache_hit_chapters == 1
    assert Path(initial.chapter_results[0].output_artifact_path).exists()
    assert Path(initial.chapter_results[0].sidecar_path).exists()


def test_corrupt_sidecar_blocks_chapter_and_preserves_prior_artifact(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    initial = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    corrupt_unit = sorted(manifest.render_units, key=lambda unit: unit.source_order)[0]
    sidecar_path = Path(str(config.segment_root / corrupt_unit.output_artifact_key) + ".json")
    sidecar_path.write_text("{not-json", encoding="utf-8")
    rerun = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    assert rerun.blocked_chapters == 1
    assert Path(initial.chapter_results[0].output_artifact_path).exists()


def test_corrupt_audio_blocks_chapter_and_preserves_prior_artifact(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    initial = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    corrupt_unit = sorted(manifest.render_units, key=lambda unit: unit.source_order)[0]
    audio_path = config.segment_root / corrupt_unit.output_artifact_key
    audio_path.write_bytes(b"broken")
    rerun = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    assert rerun.blocked_chapters == 1
    assert Path(initial.chapter_results[0].output_artifact_path).exists()


def test_atomic_audio_replacement_failure_preserves_previous_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    initial = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    original_audio = Path(initial.chapter_results[0].output_artifact_path).read_bytes()
    original_sidecar = Path(initial.chapter_results[0].sidecar_path).read_text(encoding="utf-8")

    calls = {"count": 0}
    original_replace = os.replace

    def fail_first_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 1:
            raise OSError("audio replace failed")
        return original_replace(src, dst)

    monkeypatch.setattr("app.assembler.engine.os.replace", fail_first_replace)
    changed_spacing = replace(config.spacing, narration_to_dialogue_ms=250)
    rerun = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=replace(config, spacing=changed_spacing))
    assert rerun.failed_chapters >= 1
    assert Path(initial.chapter_results[0].output_artifact_path).read_bytes() == original_audio
    assert Path(initial.chapter_results[0].sidecar_path).read_text(encoding="utf-8") == original_sidecar


def test_atomic_sidecar_replacement_failure_restores_previous_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    initial = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=config)
    original_audio = Path(initial.chapter_results[0].output_artifact_path).read_bytes()
    original_sidecar = Path(initial.chapter_results[0].sidecar_path).read_text(encoding="utf-8")

    calls = {"count": 0}
    original_replace = os.replace

    def fail_second_replace(src, dst):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("sidecar replace failed")
        return original_replace(src, dst)

    monkeypatch.setattr("app.assembler.engine.os.replace", fail_second_replace)
    changed_spacing = replace(config.spacing, narration_to_dialogue_ms=250)
    rerun = assemble_chapters(manifest, chapter_structure_source=_chapter_structure(manifest), config=replace(config, spacing=changed_spacing))
    assert rerun.failed_chapters >= 1
    assert Path(initial.chapter_results[0].output_artifact_path).read_bytes() == original_audio
    assert Path(initial.chapter_results[0].sidecar_path).read_text(encoding="utf-8") == original_sidecar


def test_duplicate_membership_and_unsafe_output_path_are_rejected(tmp_path: Path):
    manifest = _manifest()
    config = _assembly_root(tmp_path)
    config.assembly_root.mkdir(parents=True, exist_ok=True)
    config.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest, config.segment_root)
    units = sorted(manifest.render_units, key=lambda unit: unit.source_order)
    duplicate_structure = {"chapters": [{"chapter_id": "chapter-1", "chapter_order": 1, "render_unit_ids": [units[0].render_unit_id, units[1].render_unit_id]}, {"chapter_id": "chapter-2", "chapter_order": 2, "render_unit_ids": [units[1].render_unit_id, units[3].render_unit_id]}]}
    with pytest.raises(ChapterAssemblyError):
        assemble_chapters(manifest, chapter_structure_source=duplicate_structure, config=config)

    unsafe_structure = {"chapters": [{"chapter_id": "../escape", "chapter_order": 1, "render_unit_ids": [units[0].render_unit_id]}]}
    with pytest.raises(ChapterAssemblyError):
        assemble_chapters(manifest, chapter_structure_source=unsafe_structure, config=config)


def test_reordered_source_collections_produce_byte_identical_output(tmp_path: Path):
    manifest_a = _manifest(reordered=False)
    manifest_b = _manifest(reordered=True)
    config_a = replace(_assembly_root(tmp_path), assembly_root=tmp_path / "assembly-a", segment_root=tmp_path / "segments-a")
    config_b = replace(_assembly_root(tmp_path), assembly_root=tmp_path / "assembly-b", segment_root=tmp_path / "segments-b")
    config_a.assembly_root.mkdir(parents=True, exist_ok=True)
    config_a.segment_root.mkdir(parents=True, exist_ok=True)
    config_b.assembly_root.mkdir(parents=True, exist_ok=True)
    config_b.segment_root.mkdir(parents=True, exist_ok=True)
    _write_segments(manifest_a, config_a.segment_root)
    _write_segments(manifest_b, config_b.segment_root)
    report_a = assemble_chapters(manifest_a, chapter_structure_source=_chapter_structure(manifest_a), config=config_a)
    report_b = assemble_chapters(manifest_b, chapter_structure_source=_chapter_structure(manifest_b), config=config_b)

    assert report_a.chapter_results[0].output_artifact_relative_path == report_b.chapter_results[0].output_artifact_relative_path
    assert Path(report_a.chapter_results[0].output_artifact_path).read_bytes() == Path(report_b.chapter_results[0].output_artifact_path).read_bytes()
