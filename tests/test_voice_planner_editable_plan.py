from __future__ import annotations

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
    VoicePlan,
    apply_manual_override,
    load_editable_voice_plan,
    merge_voice_plans,
    save_voice_plan_atomic,
    serialize_editable_voice_plan,
    set_assignment_lock,
    validate_editable_voice_plan,
)


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
            CharacterPlan(canonical_character_id="lead", canonical_name="Lead", role="protagonist", prominence="major", speaking_frequency=10, first_appearance=1, likely_recurrence=True, assignment=_assignment("alpha", lead_voice, notes="lead notes")),
            CharacterPlan(canonical_character_id="support", canonical_name="Support", role="supporting", prominence="secondary", speaking_frequency=4, first_appearance=2, likely_recurrence=True, assignment=_assignment("beta", support_voice, notes="support notes")),
        ],
        warnings=[],
        statistics={"total_characters": 2},
        user_editable_notes=[],
    )


def _registry(include_ghost: bool = True) -> dict[str, object]:
    voices = [
        {"schema_version": 1, "voice_id": "alpha.v1", "provider": "alpha", "provider_voice_id": "v1", "display_name": "Alpha V1", "availability": "available", "quality_score": 0.95, "base_priority": 100, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
        {"schema_version": 1, "voice_id": "beta.v2", "provider": "beta", "provider_voice_id": "v2", "display_name": "Beta V2", "availability": "available", "quality_score": 0.92, "base_priority": 90, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
        {"schema_version": 1, "voice_id": "gamma.v3", "provider": "gamma", "provider_voice_id": "v3", "display_name": "Gamma V3", "availability": "available", "quality_score": 0.7, "base_priority": 10, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
    ]
    if include_ghost:
        voices.append({"schema_version": 1, "voice_id": "ghost.missing", "provider": "ghost", "provider_voice_id": "missing", "display_name": "Ghost Missing", "availability": "unavailable", "quality_score": 0.1, "base_priority": 1, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None})
    return {"schema_version": 1, "registry_version": "test", "voices": voices}


def _wrap(plan: VoicePlan) -> EditableVoicePlan:
    return load_editable_voice_plan({
        "schema_version": 1,
        "book_id": plan.book_id,
        "series_id": plan.series_id,
        "source_analysis_hash": plan.source_analysis_hash,
        "source_analysis_path": plan.source_analysis_path,
        "generated_plan": plan,
        "editable": {"narrator": {}, "characters": []},
        "edit_history": [],
        "retired_assignments": [],
        "user_notes": [],
        "warnings": [],
        "validation_issues": [],
    })


def test_load_generated_plan_into_editable_container_is_deterministic(tmp_path: Path):
    plan = _voice_plan()
    editable = _wrap(plan)

    assert editable.generated_plan == plan
    assert editable.narrator.assignment_origin == "generated"
    assert all(character.assignment_origin == "generated" for character in editable.characters)
    assert editable.generated_content_hash
    assert editable.user_editable_hash
    assert editable.effective_plan_hash

    payload = serialize_editable_voice_plan(editable)
    assert payload == serialize_editable_voice_plan(editable)
    assert json.loads(payload)["generated_plan"]["book_id"] == "book-9"

    path = tmp_path / "voice_plan.json"
    save_voice_plan_atomic(path, editable)
    reloaded = load_editable_voice_plan(path)
    assert reloaded == editable
    assert path.read_text(encoding="utf-8") == payload + "\n"


def test_manual_character_override_survives_rerun_and_updates_effective_hash():
    prior = _wrap(_voice_plan(lead_voice="v1", support_voice="v2", narrator_voice="v2"))
    manual = apply_manual_override(
        prior,
        ManualOverride(
            target_kind="character",
            canonical_character_id="lead",
            requested_provider="alpha",
            requested_provider_voice_id="v1",
            locked=True,
            manual_override=True,
            notes="keep this performance",
            override_reason="preferred delivery",
        ),
        registry=_registry(),
    )

    rerun_generated = _voice_plan(lead_voice="v3", support_voice="v2", narrator_voice="v2")
    merged = merge_voice_plans(manual, rerun_generated, registry=_registry())

    lead = merged.editable_plan.characters[0]
    assert lead.requested_provider == "alpha"
    assert lead.requested_provider_voice_id == "v1"
    assert lead.locked is True
    assert lead.manual_override is True
    assert lead.assignment_origin == "user"
    assert lead.notes == "keep this performance"
    assert lead.validation_status == "valid"
    assert merged.effective_plan.characters[0].assignment.provider_voice_id == "v1"
    assert merged.editable_plan.effective_plan_hash != prior.effective_plan_hash


def test_manual_narrator_override_preserves_requested_value_and_reports_unavailable_fallback():
    prior = _wrap(_voice_plan())
    edited = apply_manual_override(
        prior,
        ManualOverride(
            target_kind="narrator",
            requested_provider="ghost",
            requested_provider_voice_id="missing",
            locked=True,
            manual_override=True,
            notes="try the unavailable narrator",
            override_reason="audit test",
        ),
        registry=_registry(),
    )
    merged = merge_voice_plans(edited, _voice_plan(), registry=_registry())

    narrator = merged.editable_plan.narrator
    assert narrator.requested_provider == "ghost"
    assert narrator.requested_provider_voice_id == "missing"
    assert narrator.validation_status == "unresolved"
    assert any(issue.code in {"voice-unavailable", "voice-missing"} for issue in narrator.validation_issues)
    assert merged.effective_plan.narrator.assignment.provider_voice_id == "v2"
    assert narrator.locked is True
    assert narrator.manual_override is True


def test_removed_character_is_retired_and_candidate_order_changes_do_not_affect_merge():
    prior = _wrap(_voice_plan())
    reduced_generated = replace(_voice_plan(), characters=[_voice_plan().characters[0]])
    merged = merge_voice_plans(prior, reduced_generated, registry=_registry())

    assert [character.canonical_character_id for character in merged.effective_plan.characters] == ["lead"]
    assert any(record.target_kind == "character" and record.canonical_character_id == "support" for record in merged.editable_plan.retired_assignments)

    reversed_prior = _wrap(_voice_plan())
    reversed_prior = replace(reversed_prior, characters=list(reversed(reversed_prior.characters)))
    first = merge_voice_plans(prior, reduced_generated, registry=_registry())
    second = merge_voice_plans(reversed_prior, reduced_generated, registry=_registry())
    assert serialize_editable_voice_plan(first.editable_plan) == serialize_editable_voice_plan(second.editable_plan)


def test_invalid_and_corrupted_plans_fail_clearly_and_atomic_write_preserves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "voice_plan.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_editable_voice_plan(path)

    bad = _wrap(_voice_plan())
    payload = json.loads(serialize_editable_voice_plan(bad))
    payload["schema_version"] = 99
    with pytest.raises(ValueError):
        load_editable_voice_plan(payload)

    path.write_text(serialize_editable_voice_plan(bad) + "\n", encoding="utf-8")
    original = path.read_bytes()

    import app.voice_planner.editable_plan as editable_plan_module

    monkeypatch.setattr(editable_plan_module.os, "replace", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(OSError, match="replace failed"):
        save_voice_plan_atomic(path, bad)

    assert path.read_bytes() == original


def test_lock_only_edits_survive_without_becoming_manual_overrides():
    plan = _wrap(_voice_plan())
    locked = set_assignment_lock(plan, target_kind="character", canonical_character_id="lead", locked=True, registry=_registry())
    merged = merge_voice_plans(locked, _voice_plan(lead_voice="v3"), registry=_registry())

    lead = merged.editable_plan.characters[0]
    assert lead.locked is True
    assert lead.manual_override is False
    assert lead.assignment_origin == "user"
    effective = merged.effective_plan.characters[0].assignment
    assert effective is not None
    assert effective.provider_voice_id == "v1"


def test_validate_editable_voice_plan_rejects_duplicate_and_unknown_characters():
    payload = json.loads(serialize_editable_voice_plan(_wrap(_voice_plan())))
    payload["characters"].append(dict(payload["characters"][0]))
    payload["characters"][1]["canonical_character_id"] = "ghost"
    issues = validate_editable_voice_plan(payload)
    codes = {issue.code for issue in issues}
    assert "duplicate-character" in codes
    assert "unknown-character" in codes
