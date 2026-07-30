from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.voice_planner import (
    CharacterPlan,
    EditableVoicePlan,
    ManualOverride,
    NarratorPlan,
    VoiceAssignment,
    VoiceCapability,
    VoicePlan,
    apply_manual_override,
    build_synthesis_manifest,
    compare_synthesis_manifests,
    load_synthesis_manifest,
    load_editable_voice_plan,
    merge_voice_plans,
    resolve_effective_voice_plan,
    save_synthesis_manifest_atomic,
    serialize_synthesis_manifest,
    validate_synthesis_manifest,
)


def _snapshot(value):
    return copy.deepcopy(value)


def _assert_unchanged(value, baseline):
    assert value == baseline


def _assignment(provider: str, provider_voice_id: str, *, source: str = "global optimum", continuity_status: str = "new-assignment", locked: bool = False, notes: str | None = None) -> VoiceAssignment:
    return VoiceAssignment(
        voice_id=f"{provider}.{provider_voice_id}",
        provider=provider,
        provider_voice_id=provider_voice_id,
        locked=locked,
        source=source,
        continuity_status=continuity_status,
        rationale="test",
        notes=notes,
        generated=True,
    )


def _voice_plan(*, lead_voice: str = "v1", support_voice: str = "v2", narrator_voice: str = "v2") -> VoicePlan:
    return VoicePlan(
        schema_version=1,
        planner_version="test-planner",
        book_id="book-9",
        series_id="series-9",
        source_analysis_hash="analysis-hash",
        source_analysis_path="/tmp/analysis.json",
        narrator=NarratorPlan(assignment=_assignment("beta", narrator_voice, source="global optimum"), rationale="narrator test"),
        characters=[
            CharacterPlan(canonical_character_id="ada", canonical_name="Ada", role="protagonist", prominence="major", speaking_frequency=10, first_appearance=1, likely_recurrence=True, assignment=_assignment("alpha", lead_voice, notes="lead notes")),
            CharacterPlan(canonical_character_id="ben", canonical_name="Ben", role="supporting", prominence="secondary", speaking_frequency=4, first_appearance=2, likely_recurrence=True, assignment=_assignment("beta", support_voice, notes="support notes")),
        ],
        warnings=[],
        statistics={"total_characters": 2},
        user_editable_notes=[],
    )


def _registry(*, unavailable_lead: bool = False, include_pitch: bool = False) -> dict[str, object]:
    alpha_availability = "unavailable" if unavailable_lead else "available"
    voices = [
        {"schema_version": 1, "voice_id": "alpha.v1", "provider": "alpha", "provider_voice_id": "v1", "display_name": "Alpha V1", "availability": alpha_availability, "quality_score": 0.95, "base_priority": 100, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
        {"schema_version": 1, "voice_id": "beta.v2", "provider": "beta", "provider_voice_id": "v2", "display_name": "Beta V2", "availability": "available", "quality_score": 0.92, "base_priority": 90, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
        {"schema_version": 1, "voice_id": "gamma.v3", "provider": "gamma", "provider_voice_id": "v3", "display_name": "Gamma V3", "availability": "available", "quality_score": 0.7, "base_priority": 10, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
    ]
    if include_pitch:
        voices.append({"schema_version": 1, "voice_id": "delta.v4", "provider": "delta", "provider_voice_id": "v4", "display_name": "Delta V4", "availability": "available", "quality_score": 0.6, "base_priority": 5, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None})
    return {"schema_version": 1, "registry_version": "test", "voices": voices}


def _config() -> dict[str, object]:
    return {
        "voice_planner": {
            "schema_version": 1,
            "renderer_contract_version": 1,
            "default_unresolved_speaker_policy": "reject",
            "manifest_filename": "synthesis_manifest.json",
        }
    }


def _normalized_story(*, alias: str = "Ad", unresolved: bool = False, unsupported_control: bool = False, source_order_variant: bool = False, duplicate_segment_id: bool = False, voice_note: str | None = None, performance_note: str | None = None) -> dict[str, object]:
    segments = [
        {
            "segment_id": "narration-1",
            "segment_type": "narration",
            "scene_id": "scene-1",
            "chapter": 1,
            "source_order": 1,
            "source_text": "The morning light filled the room.",
            "synthesis_text": "The morning light filled the room.",
            "source_text_hash": "n1",
            "source_reference": {"chapter": 1, "paragraph_index": 1, "source_document_id": "book-9", "source_text_hash": "n1", "excerpt": "The morning light filled the room."},
        },
        {
            "segment_id": "dialogue-1",
            "segment_type": "dialogue",
            "scene_id": "scene-1",
            "chapter": 1,
            "source_order": 2,
            "speaker": alias,
            "speaker_type": "alias" if alias != "Ada" else "character",
            "source_text": '"We should go now," Ada said.',
            "synthesis_text": 'We should go now.',
            "source_text_hash": "d1",
            "source_reference": {"chapter": 1, "paragraph_index": 2, "source_document_id": "book-9", "source_text_hash": "d1", "excerpt": '"We should go now," Ada said.'},
            "controls": {"rate": 1.0} if not unsupported_control else {"pitch": 1.2},
            "pronunciation_notes": voice_note,
            "performance_notes": performance_note,
        },
        {
            "segment_id": "narration-2",
            "segment_type": "narration",
            "scene_id": "scene-2",
            "chapter": 1,
            "source_order": 3,
            "source_text": "Ben nodded in agreement.",
            "synthesis_text": "Ben nodded in agreement.",
            "source_text_hash": "n2",
            "source_reference": {"chapter": 1, "paragraph_index": 3, "source_document_id": "book-9", "source_text_hash": "n2", "excerpt": "Ben nodded in agreement."},
        },
        {
            "segment_id": "dialogue-2",
            "segment_type": "dialogue",
            "scene_id": "scene-2",
            "chapter": 1,
            "source_order": 4,
            "speaker": "Unknown Traveler" if unresolved else "Ben",
            "speaker_type": "unresolved" if unresolved else "character",
            "source_text": '"Not yet," Ben whispered.',
            "synthesis_text": 'Not yet.',
            "source_text_hash": "d2",
            "source_reference": {"chapter": 1, "paragraph_index": 4, "source_document_id": "book-9", "source_text_hash": "d2", "excerpt": '"Not yet," Ben whispered.'},
        },
    ]
    if source_order_variant:
        segments = [segments[2], segments[0], segments[3], segments[1]]
    if duplicate_segment_id:
        segments[3] = dict(segments[3], segment_id="dialogue-1")
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
            {"canonical_character_id": "ada", "canonical_name": "Ada", "aliases": ["Ad", "A."], "source_aliases": ["Ad", "A."]},
            {"canonical_character_id": "ben", "canonical_name": "Ben", "aliases": [], "source_aliases": []},
        ],
        "scenes": [
            {"scene_id": "scene-1", "chapter": 1, "scene_number": 1, "start_paragraph": 1, "end_paragraph": 2, "summary": "Ada speaks with Ben.", "source_document_id": "book-9", "source_text_hash": "scene-1"},
            {"scene_id": "scene-2", "chapter": 1, "scene_number": 2, "start_paragraph": 3, "end_paragraph": 4, "summary": "Ben and a traveler speak.", "source_document_id": "book-9", "source_text_hash": "scene-2"},
        ],
        "dialogue": [
            {"dialogue_id": "d1", "scene_id": "scene-1", "chapter": 1, "paragraph_index": 2, "speaker": alias, "quoted_text": 'We should go now.', "source_document_id": "book-9", "source_text_hash": "d1", "source_reference": {"chapter": 1, "paragraph_index": 2, "source_document_id": "book-9", "source_text_hash": "d1", "excerpt": '"We should go now," Ada said.'}},
            {"dialogue_id": "d2", "scene_id": "scene-2", "chapter": 1, "paragraph_index": 4, "speaker": "Unknown Traveler" if unresolved else "Ben", "quoted_text": 'Not yet.', "source_document_id": "book-9", "source_text_hash": "d2", "source_reference": {"chapter": 1, "paragraph_index": 4, "source_document_id": "book-9", "source_text_hash": "d2", "excerpt": '"Not yet," Ben whispered.'}},
        ],
        "narration_paragraphs": [
            {"chapter": 1, "paragraph_index": 1, "text": "The morning light filled the room.", "source_document_id": "book-9", "source_text_hash": "n1", "source_reference": {"chapter": 1, "paragraph_index": 1, "source_document_id": "book-9", "source_text_hash": "n1", "excerpt": "The morning light filled the room."}},
            {"chapter": 1, "paragraph_index": 3, "text": "Ben nodded in agreement.", "source_document_id": "book-9", "source_text_hash": "n2", "source_reference": {"chapter": 1, "paragraph_index": 3, "source_document_id": "book-9", "source_text_hash": "n2", "excerpt": "Ben nodded in agreement."}},
        ],
        "segments": segments,
        "source_artifacts": {
            "normalized_story": "analysis/normalized_story.json",
            "story": "analysis/story.json",
            "entities": "analysis/entities.json",
            "scenes": "analysis/scenes.json",
            "dialogue": "analysis/dialogue.json",
        },
        "requested_voice_controls": {"rate": 1.0},
    }


def _voice_registry() -> dict[str, object]:
    return {
        "schema_version": 1,
        "registry_version": "test",
        "voices": [
            {"schema_version": 1, "voice_id": "alpha.v1", "provider": "alpha", "provider_voice_id": "v1", "display_name": "Alpha V1", "availability": "available", "quality_score": 0.95, "base_priority": 100, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
            {"schema_version": 1, "voice_id": "beta.v2", "provider": "beta", "provider_voice_id": "v2", "display_name": "Beta V2", "availability": "available", "quality_score": 0.92, "base_priority": 90, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
            {"schema_version": 1, "voice_id": "gamma.v3", "provider": "gamma", "provider_voice_id": "v3", "display_name": "Gamma V3", "availability": "available", "quality_score": 0.7, "base_priority": 10, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
            {"schema_version": 1, "voice_id": "ghost.missing", "provider": "ghost", "provider_voice_id": "missing", "display_name": "Ghost Missing", "availability": "unavailable", "quality_score": 0.1, "base_priority": 1, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
        ],
    }


def _editable_plan() -> EditableVoicePlan:
    return build_synthesis_manifest(
        _normalized_story(),
        _voice_plan(),
        _voice_registry(),
        _config(),
        unresolved_speaker_policy="block",
    ).manifest.voice_plan


def _manual_override_plan() -> EditableVoicePlan:
    prior = load_editable_voice_plan(_voice_plan(), registry=_voice_registry())
    return apply_manual_override(
        prior,
        ManualOverride(
            target_kind="character",
            canonical_character_id="ada",
            requested_provider="alpha",
            requested_provider_voice_id="v1",
            locked=True,
            manual_override=True,
            notes="keep her bright",
            pronunciation_notes="Ada pronounced AY-dah",
            casting_notes="Cast a bright, youthful voice",
            override_reason="preferred performance",
        ),
        registry=_voice_registry(),
    )


def test_build_manifest_basic_order_and_round_trip_and_immutability(tmp_path: Path):
    story = _normalized_story()
    plan = _voice_plan()
    registry = _voice_registry()
    config = _config()

    story_baseline = _snapshot(story)
    plan_baseline = _snapshot(plan)
    registry_baseline = _snapshot(registry)
    config_baseline = _snapshot(config)

    result = build_synthesis_manifest(story, plan, registry, config, unresolved_speaker_policy="block")
    manifest = result.manifest

    _assert_unchanged(story, story_baseline)
    _assert_unchanged(plan, plan_baseline)
    _assert_unchanged(registry, registry_baseline)
    _assert_unchanged(config, config_baseline)

    assert manifest.book_id == "book-9"
    assert [unit.segment_type for unit in manifest.render_units] == ["narration", "dialogue", "narration", "dialogue"]
    assert len({unit.render_unit_id for unit in manifest.render_units}) == len(manifest.render_units)
    assert result.validation_report.ready_state == "ready"
    assert result.validation_report.total_render_units == 4
    assert result.validation_report.narration_units == 2
    assert result.validation_report.dialogue_units == 2

    payload = serialize_synthesis_manifest(manifest)
    assert payload == serialize_synthesis_manifest(manifest)

    path = tmp_path / "synthesis_manifest.json"
    save_synthesis_manifest_atomic(path, manifest)
    loaded = load_synthesis_manifest(path)
    assert loaded == manifest
    assert path.read_text(encoding="utf-8") == payload + "\n"


def test_alias_resolution_and_unresolved_speaker_policies():
    story = _normalized_story(alias="A.", unresolved=True)
    plan = _voice_plan()
    registry = _voice_registry()
    config = _config()

    blocked = build_synthesis_manifest(story, plan, registry, config, unresolved_speaker_policy="block")
    assert any(unit.canonical_speaker_id == "ada" for unit in blocked.manifest.render_units)
    assert any(unit.blocked_reason for unit in blocked.manifest.render_units if unit.canonical_speaker_id is None)
    assert blocked.validation_report.ready_state == "blocked"

    omitted = build_synthesis_manifest(story, plan, registry, config, unresolved_speaker_policy="omit")
    assert omitted.validation_report.skipped_units == 1
    assert all(unit.speaker_type != "unresolved" for unit in omitted.manifest.render_units)

    with pytest.raises(ValueError):
        build_synthesis_manifest(story, plan, registry, config)


def test_unavailable_effective_voice_blocks_without_substitution_and_requested_voice_remains_auditable():
    story = _normalized_story()
    plan = _manual_override_plan()
    registry = _voice_registry()
    registry["voices"][0]["availability"] = "unavailable"

    result = build_synthesis_manifest(story, plan, registry, _config(), unresolved_speaker_policy="block")
    lead_unit = next(unit for unit in result.manifest.render_units if unit.canonical_speaker_id == "ada")
    assert lead_unit.requested_provider == "alpha"
    assert lead_unit.requested_provider_voice_id == "v1"
    assert lead_unit.assigned_provider == "alpha"
    assert lead_unit.assigned_provider_voice_id == "v1"
    assert lead_unit.blocked_reason is not None
    assert result.validation_report.unavailable_voices >= 1


def test_requested_and_effective_override_can_diverge_without_changing_audited_request():
    registry = _voice_registry()
    registry["voices"][2]["availability"] = "unavailable"
    plan = load_editable_voice_plan(_voice_plan(), registry=_voice_registry())
    diverged_plan = apply_manual_override(
        plan,
        ManualOverride(
            target_kind="character",
            canonical_character_id="ada",
            requested_provider="gamma",
            requested_provider_voice_id="v3",
            locked=False,
            manual_override=True,
            notes="prefer the alternate take",
            override_reason="requested voice unavailable",
        ),
        registry=registry,
    )
    result = build_synthesis_manifest(_normalized_story(), diverged_plan, registry, _config(), unresolved_speaker_policy="block")
    ada_unit = next(unit for unit in result.manifest.render_units if unit.canonical_speaker_id == "ada")
    base_unit = build_synthesis_manifest(_normalized_story(), _voice_plan(), _voice_registry(), _config(), unresolved_speaker_policy="block").manifest.render_units[1]
    assert ada_unit.requested_provider == "gamma"
    assert ada_unit.requested_provider_voice_id == "v3"
    assert ada_unit.assigned_provider == base_unit.assigned_provider
    assert ada_unit.assigned_provider_voice_id == base_unit.assigned_provider_voice_id
    assert ada_unit.synthesis_input_hash == base_unit.synthesis_input_hash


def test_unsupported_control_warns_and_is_excluded_from_effective_controls():
    story = _normalized_story(unsupported_control=True, voice_note="Ada pronunciation note", performance_note="pace slowly")
    plan = _voice_plan()
    registry = _voice_registry()

    result = build_synthesis_manifest(story, plan, registry, _config(), unresolved_speaker_policy="block")
    dialogue_unit = next(unit for unit in result.manifest.render_units if unit.segment_type == "dialogue")
    assert dialogue_unit.warnings
    assert "pitch" not in dialogue_unit.effective_renderer_controls
    assert result.validation_report.unsupported_controls >= 1


def test_duplicate_source_ids_fail_and_reordered_inputs_are_byte_identical():
    story = _normalized_story(duplicate_segment_id=True)
    plan = _voice_plan()
    registry = _voice_registry()

    with pytest.raises(ValueError):
        build_synthesis_manifest(story, plan, registry, _config(), unresolved_speaker_policy="block")

    ordered = build_synthesis_manifest(_normalized_story(), plan, registry, _config(), unresolved_speaker_policy="block")
    reordered_story = _normalized_story(source_order_variant=True)
    reordered = build_synthesis_manifest(reordered_story, plan, registry, _config(), unresolved_speaker_policy="block")
    assert serialize_synthesis_manifest(ordered.manifest) == serialize_synthesis_manifest(reordered.manifest)


def test_text_voice_note_and_render_contract_hashes_change_only_when_expected():
    registry = _voice_registry()
    config = _config()
    base = build_synthesis_manifest(_normalized_story(), _voice_plan(), registry, config, unresolved_speaker_policy="block").manifest
    text_changed = build_synthesis_manifest(_normalized_story(), _voice_plan(), registry, config, unresolved_speaker_policy="block").manifest
    voice_changed_plan = _voice_plan(lead_voice="v3")
    voice_changed = build_synthesis_manifest(_normalized_story(), voice_changed_plan, registry, config, unresolved_speaker_policy="block").manifest
    note_only_story = _normalized_story()
    note_only_story["segments"][1]["notes"] = "audit only"
    note_only = build_synthesis_manifest(note_only_story, _voice_plan(), registry, config, unresolved_speaker_policy="block").manifest
    pronunciation_story = _normalized_story()
    pronunciation_story["segments"][1]["pronunciation_notes"] = "Ada pronounced AY-dah"
    pronunciation_note = build_synthesis_manifest(pronunciation_story, _voice_plan(), registry, config, unresolved_speaker_policy="block").manifest
    perf_note_story = _normalized_story()
    perf_note_story["segments"][1]["performance_notes"] = "emphasize the final word"
    perf_note = build_synthesis_manifest(perf_note_story, _voice_plan(), registry, config, unresolved_speaker_policy="block").manifest

    assert base.render_units[1].synthesis_input_hash == text_changed.render_units[1].synthesis_input_hash
    assert base.render_units[1].synthesis_input_hash != voice_changed.render_units[1].synthesis_input_hash
    assert base.render_units[1].synthesis_input_hash == note_only.render_units[1].synthesis_input_hash
    assert base.render_units[1].synthesis_input_hash != pronunciation_note.render_units[1].synthesis_input_hash
    assert base.render_units[1].synthesis_input_hash != perf_note.render_units[1].synthesis_input_hash


def test_compare_manifests_reports_added_removed_and_changed_units():
    registry = _voice_registry()
    config = _config()
    baseline = build_synthesis_manifest(_normalized_story(), _voice_plan(), registry, config, unresolved_speaker_policy="block").manifest
    changed_story = _normalized_story()
    changed_story["segments"] = changed_story["segments"] + [{
        "segment_id": "narration-3",
        "segment_type": "narration",
        "scene_id": "scene-2",
        "chapter": 1,
        "source_order": 5,
        "source_text": "A new paragraph appeared.",
        "synthesis_text": "A new paragraph appeared.",
        "source_text_hash": "n3",
        "source_reference": {"chapter": 1, "paragraph_index": 5, "source_document_id": "book-9", "source_text_hash": "n3", "excerpt": "A new paragraph appeared."},
    }]
    changed = build_synthesis_manifest(changed_story, _voice_plan(lead_voice="v3"), registry, config, unresolved_speaker_policy="block").manifest
    diff = compare_synthesis_manifests(baseline, changed)
    assert diff.added_unit_ids
    assert diff.changed_unit_ids
    assert diff.removed_unit_ids == []


def test_atomic_failure_preserves_existing_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = build_synthesis_manifest(_normalized_story(), _voice_plan(), _voice_registry(), _config(), unresolved_speaker_policy="block").manifest
    path = tmp_path / "synthesis_manifest.json"
    save_synthesis_manifest_atomic(path, manifest)
    original = path.read_bytes()

    import app.voice_planner.synthesis_manifest as synthesis_manifest_module

    monkeypatch.setattr(synthesis_manifest_module.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        save_synthesis_manifest_atomic(path, manifest)
    assert path.read_bytes() == original


def test_validation_rejects_unsupported_version_and_hash_mismatch():
    manifest = build_synthesis_manifest(_normalized_story(), _voice_plan(), _voice_registry(), _config(), unresolved_speaker_policy="block").manifest
    payload = json.loads(serialize_synthesis_manifest(manifest))
    payload["schema_version"] = 99
    with pytest.raises(ValueError):
        load_synthesis_manifest(payload)
    payload = json.loads(serialize_synthesis_manifest(manifest))
    payload["manifest_content_hash"] = "bogus"
    with pytest.raises(ValueError):
        load_synthesis_manifest(payload)
