from __future__ import annotations

import json

import pytest

from app.voice_planner import (
    AssignmentProvenance,
    CharacterPlan,
    NarratorPlan,
    PlanningReport,
    SceneConflict,
    VoiceAssignment,
    VoiceCapability,
    VoicePlan,
    canonical_json_dumps,
)
from app.voice_planner.schema import SCHEMA_VERSIONS, validate_assignment_report, validate_voice_plan, validate_voice_registry


def test_schema_versions_exist():
    assert SCHEMA_VERSIONS["voice_capability"] == 1
    assert SCHEMA_VERSIONS["voice_plan"] == 1
    assert SCHEMA_VERSIONS["assignment_report"] == 1
    assert SCHEMA_VERSIONS["series_bindings"] == 1


def test_voice_plan_serialization_is_deterministic():
    assignment = VoiceAssignment(
        voice_id="kokoro.af_bella",
        provider="kokoro",
        provider_voice_id="af_bella",
        locked=True,
        source="manual",
        confidence=0.98,
        notes="locked narrator",
        generated=False,
        provenance=AssignmentProvenance(
            source="manual",
            reason="user override",
            basis="narrator",
            selected_from=["kokoro.af_bella"],
            score=100.0,
        ),
    )
    plan = VoicePlan(
        schema_version=1,
        planner_version="phase4b",
        book_id="merchant-of-death",
        series_id="pendragon",
        source_analysis_hash="abc123",
        source_analysis_path="/output/book/analysis",
        narrator=NarratorPlan(assignment=assignment, rationale="stable narrator"),
        characters=[
            CharacterPlan(
                canonical_character_id="bobby-pendragon",
                canonical_name="Bobby Pendragon",
                role="protagonist",
                prominence="major recurring",
                speaking_frequency=42,
                first_appearance=1,
                likely_recurrence=True,
                age_bucket="teen",
                gender_presentation="male",
                species_or_archetype="human",
                scene_relationships=[{"scene_id": "scene-1", "speaks": True}],
                unresolved_metadata={"note": "derived"},
                assignment=assignment,
            )
        ],
        conflicts=[SceneConflict(scene_id="scene-1", character_a="bobby-pendragon", character_b="uncle-press", conflict_type="soft_similarity", penalty=0.25)],
        warnings=["scarcity warning"],
        statistics={"total_characters": 1, "assigned": 1},
    )

    payload_1 = canonical_json_dumps(plan)
    payload_2 = canonical_json_dumps(plan)
    assert payload_1 == payload_2
    decoded = json.loads(payload_1)
    assert decoded["schema_version"] == 1
    assert decoded["narrator"]["assignment"]["locked"] is True
    assert decoded["characters"][0]["assignment"]["source"] == "manual"
    assert decoded["characters"][0]["assignment"]["generated"] is False


def test_validation_rules_reject_invalid_required_fields():
    invalid_registry = {
        "schema_version": 1,
        "voices": [
            {"schema_version": 1, "voice_id": "v1", "provider": "kokoro"},
            {"schema_version": 1, "voice_id": "v2", "provider": "kokoro", "provider_voice_id": "dup", "display_name": "Voice 2", "availability": "available", "quality_score": 0.1, "base_priority": 1},
            {"schema_version": 1, "voice_id": "v3", "provider": "kokoro", "provider_voice_id": "dup", "display_name": "Voice 3", "availability": "available", "quality_score": 0.2, "base_priority": 2},
        ],
    }
    errors = validate_voice_registry(invalid_registry)
    assert any("missing voice registry entry" in error for error in errors)
    assert any("duplicate voice registry key" in error for error in errors)

    invalid_plan = {
        "schema_version": 1,
        "planner_version": "phase4b",
        "book_id": "b",
        "series_id": "s",
        "source_analysis_hash": "h",
        "source_analysis_path": "/tmp/x",
        "narrator": [],
        "characters": "bad",
        "conflicts": [],
        "scarcity_events": [],
        "warnings": [],
        "statistics": {},
    }
    plan_errors = validate_voice_plan(invalid_plan)
    assert "voice plan narrator must be a mapping" in plan_errors
    assert "voice plan characters must be a sequence" in plan_errors

    invalid_report = {
        "schema_version": 1,
        "book_id": "b",
        "series_id": "s",
        "plan_hash": "ph",
        "generated_at": "now",
        "narrator_choice": {},
        "reused_bindings": [],
        "new_bindings": [],
        "manual_overrides": [],
        "locked_assignments": [],
        "deferred_characters": [],
        "unavailable_voices": [],
        "scarcity_events": [],
        "similarity_conflicts": [],
        "scene_conflicts": [],
        "fallback_tiers_used": [],
        "scoring_summaries": [],
        "validation_warnings": [],
        "final_statistics": {},
    }
    report_errors = validate_assignment_report(invalid_report)
    assert report_errors == []


def test_report_schema_and_generated_fields_are_distinct():
    report = PlanningReport(
        schema_version=1,
        book_id="merchant-of-death",
        series_id="pendragon",
        plan_hash="abc123",
        generated_at="2026-01-01T00:00:00Z",
        narrator_choice={"voice_id": "kokoro.af_bella", "reason": "stable"},
        final_statistics={"assigned": 1},
    )
    data = json.loads(canonical_json_dumps(report))
    assert data["schema_version"] == 1
    assert data["narrator_choice"]["voice_id"] == "kokoro.af_bella"
    assert data["final_statistics"]["assigned"] == 1

    vc = VoiceCapability(
        schema_version=1,
        voice_id="kokoro.af_bella",
        provider="kokoro",
        provider_voice_id="af_bella",
        display_name="AF Bella",
        supported_controls=["pitch", "rate"],
        supported_languages=["en-US"],
        quality_score=0.91,
        availability="available",
        base_priority=10,
    )
    assert json.loads(canonical_json_dumps(vc))["provider_voice_id"] == "af_bella"
