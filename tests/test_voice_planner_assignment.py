from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.config import load_settings
from app.voice_planner import (
    AssignmentContext,
    AssignmentProvenance,
    BudgetContext,
    CandidateScore,
    CharacterProfile,
    ConflictConfig,
    ConflictReport,
    DialogueProximityEvidence,
    PlanningReport,
    SceneConflictContext,
    ScoreComponent,
    ScoreContext,
    SeriesBindings,
    SeriesVoiceBinding,
    VoiceAssignment,
    VoiceBudget,
    VoiceCapability,
    VoicePlan,
    analyze_scene_conflicts,
    assign_voices,
    calculate_voice_budget,
    serialize_assignment_report,
    serialize_voice_plan,
)


def _profile(
    canonical_character_id: str,
    *,
    canonical_name: str | None = None,
    role: str | None = None,
    prominence: str | None = None,
    speaking_frequency: int = 0,
    dialogue_count: int = 0,
    scene_count: int = 0,
    likely_recurrence: bool | None = None,
    first_appearance_order: int | None = None,
) -> CharacterProfile:
    return CharacterProfile(
        schema_version=1,
        canonical_character_id=canonical_character_id,
        canonical_name=canonical_name or canonical_character_id,
        role=role,
        prominence=prominence,
        speaking_frequency=speaking_frequency,
        first_appearance_order=first_appearance_order,
        likely_recurrence=likely_recurrence,
        age_bucket=None,
        gender_presentation=None,
        species_or_archetype=None,
        scene_relationships=[],
        dialogue_count=dialogue_count,
        scene_count=scene_count,
        source_aliases=[],
        unresolved_metadata={},
        source_provenance={},
    )


def _voice(
    provider: str,
    provider_voice_id: str,
    *,
    display_name: str | None = None,
    availability: str = "available",
    quality_score: float = 0.8,
    base_priority: int = 10,
    similarity_cluster: str | None = None,
) -> VoiceCapability:
    return VoiceCapability(
        schema_version=1,
        voice_id=f"{provider}.{provider_voice_id}",
        provider=provider,
        provider_voice_id=provider_voice_id,
        display_name=display_name or provider_voice_id,
        gender_presentation=None,
        age_presentation=None,
        archetype_tags=[],
        style_tags=[],
        similarity_cluster=similarity_cluster,
        quality_score=quality_score,
        latency_estimate_ms=None,
        supported_languages=["en-US"],
        sample_rate_hz=None,
        supported_controls=["rate", "pitch"],
        licensing_information=None,
        availability=availability,
        base_priority=base_priority,
        notes=None,
    )


def _candidate(
    provider: str,
    provider_voice_id: str,
    *,
    total_score: int,
    binding_precedence: str = "no binding",
    binding_rank: int = 4,
    quality_points: int = 0,
    base_priority: int = 0,
    eligible: bool = True,
) -> CandidateScore:
    return CandidateScore(
        provider=provider,
        provider_voice_id=provider_voice_id,
        voice_id=f"{provider}.{provider_voice_id}",
        registry_key=f"{provider}::{provider_voice_id}",
        total_score=total_score,
        eligible=eligible,
        eligibility_status="eligible" if eligible else "ineligible",
        ineligibility_reasons=[] if eligible else ["test ineligible"],
        score_components=[ScoreComponent(name="base", points=total_score, reason="test")],
        bonuses=[],
        penalties=[],
        tie_break_metadata={
            "binding_precedence_rank": binding_rank,
            "total_score": total_score,
            "scene_separation_score": 0,
            "registry_base_priority": base_priority,
            "registry_quality_points": quality_points,
            "provider_sort_key": provider,
            "provider_voice_id_sort_key": provider_voice_id,
        },
        rationale="test candidate",
        binding_precedence=binding_precedence,
        binding_precedence_rank=binding_rank,
        scene_separation_score=0,
        registry_base_priority=base_priority,
        registry_quality_points=quality_points,
    )


def _scene(scene_id: str, order: int, character_ids: list[str], speaking_character_ids: list[str]) -> dict[str, object]:
    return {
        "scene_id": scene_id,
        "scene_order": order,
        "character_ids": character_ids,
        "speaking_character_ids": speaking_character_ids,
    }


def _dialogue(dialogue_id: str, scene_id: str, order: int, speaker_character_id: str | None, text: str) -> dict[str, object]:
    return {
        "dialogue_id": dialogue_id,
        "scene_id": scene_id,
        "order": order,
        "quote_count": 1,
        "word_count": len(text.split()),
        "text": text,
        "speaker_character_id": speaker_character_id,
    }


def _budget(profiles: list[CharacterProfile], candidate_scores: dict[str, tuple[CandidateScore, ...]], series_bindings: SeriesBindings | None = None) -> VoiceBudget:
    return calculate_voice_budget(
        BudgetContext(
            character_profiles=tuple(profiles),
            candidate_scores=tuple(score for scores in candidate_scores.values() for score in scores),
            series_bindings=series_bindings,
            narrator_required=bool(series_bindings and series_bindings.narrator),
        )
    )


def _conflict_report(
    profiles: list[CharacterProfile],
    scenes: list[dict[str, object]],
    dialogues: list[dict[str, object]],
    candidate_scores: dict[str, tuple[CandidateScore, ...]],
    registry: dict[str, object],
    series_bindings: SeriesBindings | None = None,
    voice_budget: VoiceBudget | None = None,
) -> ConflictReport:
    return analyze_scene_conflicts(
        SceneConflictContext(
            character_profiles=tuple(profiles),
            scene_records=tuple(scenes),
            dialogue_records=tuple(dialogues),
            candidate_scores_by_character=candidate_scores,
            voice_budget=voice_budget,
            series_bindings=series_bindings,
            voice_registry=registry,
            config=load_settings().voice_planner.conflicts,
        )
    )


def _registry() -> dict[str, object]:
    return {
        "schema_version": 1,
        "registry_version": "test",
        "voices": [
            _voice("alpha", "v1", display_name="Alpha One", similarity_cluster="cluster-a", quality_score=0.95, base_priority=100),
            _voice("beta", "v2", display_name="Beta Two", similarity_cluster="cluster-b", quality_score=0.92, base_priority=90),
            _voice("gamma", "v3", display_name="Gamma Three", similarity_cluster="cluster-c", quality_score=0.70, base_priority=10),
            _voice("solo", "only", display_name="Solo Voice", similarity_cluster="cluster-solo", quality_score=0.99, base_priority=100),
        ],
    }


def test_global_assignment_engine_prefers_cast_wide_optimum_over_greedy_top_picks():
    profiles = [
        _profile("a", role="protagonist", prominence="major recurring", speaking_frequency=10, dialogue_count=8, scene_count=6, likely_recurrence=True),
        _profile("b", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=5, likely_recurrence=True),
        _profile("c", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=5, likely_recurrence=True),
        _profile("d", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=5, likely_recurrence=True),
    ]
    registry = _registry()
    candidate_scores = {
        "a": (
            _candidate("alpha", "v1", total_score=100, quality_points=95, base_priority=100),
            _candidate("beta", "v2", total_score=99, quality_points=92, base_priority=90),
        ),
        "b": (
            _candidate("alpha", "v1", total_score=98, quality_points=95, base_priority=100),
            _candidate("gamma", "v3", total_score=60, quality_points=70, base_priority=10),
        ),
        "c": (
            _candidate("alpha", "v1", total_score=98, quality_points=95, base_priority=100),
            _candidate("gamma", "v3", total_score=60, quality_points=70, base_priority=10),
        ),
        "d": (
            _candidate("alpha", "v1", total_score=98, quality_points=95, base_priority=100),
            _candidate("gamma", "v3", total_score=60, quality_points=70, base_priority=10),
        ),
    }
    scenes = [
        _scene("s1", 1, ["a", "b"], ["a", "b"]),
        _scene("s2", 2, ["a", "c"], ["a", "c"]),
        _scene("s3", 3, ["a", "d"], ["a", "d"]),
    ]
    dialogues = [
        _dialogue("d1", "s1", 1, "a", "A."),
        _dialogue("d2", "s1", 2, "b", "B."),
        _dialogue("d3", "s2", 3, "a", "A again."),
        _dialogue("d4", "s2", 4, "c", "C."),
        _dialogue("d5", "s3", 5, "a", "A third."),
        _dialogue("d6", "s3", 6, "d", "D."),
    ]
    budget = _budget(profiles, candidate_scores)
    report = _conflict_report(profiles, scenes, dialogues, candidate_scores, registry, voice_budget=budget)
    context = AssignmentContext(
        book_id="book-1",
        series_id="series-1",
        source_analysis_path="/tmp/analysis",
        source_analysis_hash="analysis-hash",
        source_voice_registry_hash="registry-hash",
        source_series_bindings_hash=None,
        character_profiles=tuple(profiles),
        registry=registry,
        series_bindings=None,
        candidate_scores_by_character=candidate_scores,
        voice_budget=budget,
        conflict_report=report,
        config=load_settings().voice_planner,
    )

    result = assign_voices(context)

    assert [character.canonical_character_id for character in result.voice_plan.characters] == ["a", "b", "c", "d"]
    assert result.voice_plan.characters[0].assignment.provider_voice_id == "v2"
    assert [character.assignment.provider_voice_id for character in result.voice_plan.characters[1:]] == ["v1", "v1", "v1"]
    assert result.assignment_report.total_characters == 4
    assert result.assignment_report.reused_voices >= 1
    assert result.assignment_report.unresolved_conflicts == 0
    assert result.assignment_report.optimization_statistics["deterministic"] is True
    assert result.assignment_report.optimization_statistics["search_strategy"] == "branch_and_bound"


def test_locked_binding_and_narrator_continuity_are_preserved_and_unavailable_locks_are_reported():
    profiles = [
        _profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=10, dialogue_count=8, scene_count=6, likely_recurrence=True),
        _profile("major", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=5, likely_recurrence=True),
    ]
    registry = _registry()
    bindings = SeriesBindings(
        schema_version=1,
        series_id="series-2",
        narrator=SeriesVoiceBinding(
            target_kind="narrator",
            provider="beta",
            provider_voice_id="v2",
            voice_id="beta.v2",
            locked=False,
            manual_override=False,
            inherited=True,
            assignment_confidence=0.8,
            assignment_reason="continuity",
            assignment_timestamp="2026-07-29T00:00:00Z",
            provenance=AssignmentProvenance(source="series", reason="continuity", basis="history"),
            history=[],
        ),
        bindings=[
            SeriesVoiceBinding(
                target_kind="character",
                canonical_character_id="lead",
                provider="alpha",
                provider_voice_id="v1",
                voice_id="alpha.v1",
                locked=True,
                manual_override=True,
                inherited=False,
                assignment_confidence=0.99,
                assignment_reason="manual lock",
                assignment_timestamp="2026-07-29T00:00:00Z",
                provenance=AssignmentProvenance(source="manual", reason="lock", basis="user"),
                history=[],
            ),
            SeriesVoiceBinding(
                target_kind="character",
                canonical_character_id="major",
                provider="ghost",
                provider_voice_id="missing",
                voice_id="ghost.missing",
                locked=True,
                manual_override=False,
                inherited=True,
                assignment_confidence=0.8,
                assignment_reason="missing inherited voice",
                assignment_timestamp="2026-07-29T00:00:00Z",
                provenance=AssignmentProvenance(source="series", reason="inherit", basis="history"),
                unavailable=True,
                history=[],
            ),
        ],
        history=[],
        updated_at="2026-07-29T00:00:00Z",
    )
    narrator_candidates = (
        _candidate("alpha", "v1", total_score=80, quality_points=95, base_priority=100),
        _candidate("beta", "v2", total_score=70, quality_points=92, base_priority=90),
    )
    candidate_scores = {
        "lead": (
            _candidate("alpha", "v1", total_score=100, binding_precedence="locked manual override", binding_rank=0, quality_points=95, base_priority=100),
            _candidate("beta", "v2", total_score=95, quality_points=92, base_priority=90),
        ),
        "major": (
            _candidate("beta", "v2", total_score=98, quality_points=92, base_priority=90),
            _candidate("gamma", "v3", total_score=60, quality_points=70, base_priority=10),
        ),
    }
    scenes = [_scene("s1", 1, ["lead", "major"], ["lead", "major"])]
    dialogues = [_dialogue("d1", "s1", 1, "lead", "Lead."), _dialogue("d2", "s1", 2, "major", "Major.")]
    budget = _budget(profiles, candidate_scores, bindings)
    report = _conflict_report(profiles, scenes, dialogues, candidate_scores, registry, series_bindings=bindings, voice_budget=budget)
    context = AssignmentContext(
        book_id="book-2",
        series_id="series-2",
        source_analysis_path="/tmp/analysis",
        source_analysis_hash="analysis-hash",
        source_voice_registry_hash="registry-hash",
        source_series_bindings_hash="bindings-hash",
        character_profiles=tuple(profiles),
        registry=registry,
        series_bindings=bindings,
        candidate_scores_by_character=candidate_scores,
        narrator_candidates=narrator_candidates,
        voice_budget=budget,
        conflict_report=report,
        config=load_settings().voice_planner,
    )

    result = assign_voices(context)

    narrator_assignment = result.voice_plan.narrator.assignment
    assert narrator_assignment is not None
    lead_assignment = next(character.assignment for character in result.voice_plan.characters if character.canonical_character_id == "lead")
    major_assignment = next(character.assignment for character in result.voice_plan.characters if character.canonical_character_id == "major")

    assert narrator_assignment.provider_voice_id == "v2"
    assert narrator_assignment.continuity_status == "inherited-continuity"
    assert narrator_assignment.source == "inherited series binding"
    assert lead_assignment.provider_voice_id == "v1"
    assert lead_assignment.locked is True
    assert result.assignment_report.total_locked_assignments == 1
    assert result.assignment_report.manual_overrides_honored == 1
    assert any(entry["provider_voice_id"] == "missing" for entry in result.assignment_report.unavailable_voices)
    assert major_assignment.provider_voice_id in {"v2", "v3"}
    assert major_assignment.continuity_status == "new-assignment"
    assert result.assignment_report.inherited_bindings_reused == 0


def test_deterministic_serialization_and_tie_breaks_are_byte_identical():
    profiles = [
        _profile("one", role="supporting", prominence="supporting recurring", speaking_frequency=3, dialogue_count=3, scene_count=3, likely_recurrence=True),
        _profile("two", role="supporting", prominence="supporting recurring", speaking_frequency=3, dialogue_count=3, scene_count=3, likely_recurrence=True),
    ]
    registry = _registry()
    candidate_scores = {
        "one": (
            _candidate("beta", "v2", total_score=70, quality_points=92, base_priority=90),
            _candidate("alpha", "v1", total_score=70, quality_points=95, base_priority=100),
        ),
        "two": (
            _candidate("beta", "v2", total_score=70, quality_points=92, base_priority=90),
            _candidate("alpha", "v1", total_score=70, quality_points=95, base_priority=100),
        ),
    }
    scenes = [_scene("s1", 1, ["one", "two"], ["one", "two"])]
    dialogues = [_dialogue("d1", "s1", 1, "one", "One."), _dialogue("d2", "s1", 2, "two", "Two.")]
    budget = _budget(profiles, candidate_scores)
    report = _conflict_report(profiles, scenes, dialogues, candidate_scores, registry, voice_budget=budget)
    context = AssignmentContext(
        book_id="book-3",
        series_id="series-3",
        source_analysis_path="/tmp/analysis",
        source_analysis_hash="analysis-hash",
        source_voice_registry_hash="registry-hash",
        source_series_bindings_hash=None,
        character_profiles=tuple(profiles),
        registry=registry,
        series_bindings=None,
        candidate_scores_by_character=candidate_scores,
        voice_budget=budget,
        conflict_report=report,
        config=load_settings().voice_planner,
    )

    first = assign_voices(context)
    second = assign_voices(context)

    assert serialize_voice_plan(first.voice_plan) == serialize_voice_plan(second.voice_plan)
    assert serialize_assignment_report(first.assignment_report) == serialize_assignment_report(second.assignment_report)
    assert [character.assignment.provider_voice_id for character in first.voice_plan.characters] == ["v1", "v2"]
    assert [character.assignment.provider_voice_id for character in second.voice_plan.characters] == ["v1", "v2"]


def test_single_voice_mode_uses_one_voice_for_every_assignment():
    profiles = [
        _profile("lead", role="protagonist", prominence="primary or lead", speaking_frequency=8, dialogue_count=6, scene_count=5, likely_recurrence=True),
        _profile("support", role="supporting", prominence="supporting recurring", speaking_frequency=4, dialogue_count=3, scene_count=3, likely_recurrence=True),
    ]
    registry = _registry()
    narrator_candidates = (
        _candidate("beta", "v2", total_score=100, quality_points=92, base_priority=90),
    )
    candidate_scores = {
        "lead": (
            _candidate("alpha", "v1", total_score=80, quality_points=95, base_priority=100),
            _candidate("gamma", "v3", total_score=70, quality_points=70, base_priority=10),
        ),
        "support": (
            _candidate("alpha", "v1", total_score=60, quality_points=95, base_priority=100),
            _candidate("gamma", "v3", total_score=55, quality_points=70, base_priority=10),
        ),
    }
    scenes = [_scene("s1", 1, ["lead", "support"], ["lead", "support"])]
    dialogues = [_dialogue("d1", "s1", 1, "lead", "Lead."), _dialogue("d2", "s1", 2, "support", "Support.")]
    budget = _budget(profiles, candidate_scores)
    report = _conflict_report(profiles, scenes, dialogues, candidate_scores, registry, voice_budget=budget)
    context = AssignmentContext(
        book_id="book-single-voice",
        series_id="series-single-voice",
        source_analysis_path="/tmp/analysis",
        source_analysis_hash="analysis-hash",
        source_voice_registry_hash="registry-hash",
        source_series_bindings_hash=None,
        character_profiles=tuple(profiles),
        registry=registry,
        series_bindings=None,
        candidate_scores_by_character=candidate_scores,
        narrator_candidates=narrator_candidates,
        voice_budget=budget,
        conflict_report=report,
        config=replace(load_settings().voice_planner, single_voice_mode=True),
    )

    result = assign_voices(context)

    narrator_assignment = result.voice_plan.narrator.assignment
    assert narrator_assignment.provider_voice_id == "v2"
    character_assignments = []
    for character in result.voice_plan.characters:
        assert character.assignment is not None
        character_assignments.append(character.assignment)
    assert {assignment.provider_voice_id for assignment in character_assignments} == {"v2"}
    assert all(assignment.source == "single voice mode" for assignment in character_assignments)
    assert result.assignment_report.optimization_statistics["search_strategy"] == "single_voice"
    assert result.assignment_report.optimization_statistics["single_voice_mode"] is True
    assert result.assignment_report.final_statistics["unique_voices"] == 1
    assert result.assignment_report.reused_voices == 1


def test_critical_scarcity_reuses_the_only_voice_available():
    profiles = [
        _profile("a", speaking_frequency=1, dialogue_count=1, scene_count=1),
        _profile("b", speaking_frequency=1, dialogue_count=1, scene_count=1),
        _profile("c", speaking_frequency=1, dialogue_count=1, scene_count=1),
    ]
    registry = {
        "schema_version": 1,
        "registry_version": "test",
        "voices": [_voice("solo", "only", display_name="Solo Voice", similarity_cluster="cluster-solo", quality_score=0.99, base_priority=100)],
    }
    candidate_scores = {
        "a": (_candidate("solo", "only", total_score=100, quality_points=99, base_priority=100),),
        "b": (_candidate("solo", "only", total_score=100, quality_points=99, base_priority=100),),
        "c": (_candidate("solo", "only", total_score=100, quality_points=99, base_priority=100),),
    }
    scenes = [_scene("s1", 1, ["a", "b", "c"], ["a", "b", "c"])]
    dialogues = [_dialogue("d1", "s1", 1, "a", "A."), _dialogue("d2", "s1", 2, "b", "B."), _dialogue("d3", "s1", 3, "c", "C.")]
    budget = _budget(profiles, candidate_scores)
    report = _conflict_report(profiles, scenes, dialogues, candidate_scores, registry, voice_budget=budget)
    context = AssignmentContext(
        book_id="book-4",
        series_id="series-4",
        source_analysis_path="/tmp/analysis",
        source_analysis_hash="analysis-hash",
        source_voice_registry_hash="registry-hash",
        source_series_bindings_hash=None,
        character_profiles=tuple(profiles),
        registry=registry,
        series_bindings=None,
        candidate_scores_by_character=candidate_scores,
        voice_budget=budget,
        conflict_report=report,
        config=load_settings().voice_planner,
    )

    result = assign_voices(context)

    assert result.assignment_report.scarcity_level == "critical"
    assert result.assignment_report.reused_voices >= 2
    assert {character.assignment.provider_voice_id for character in result.voice_plan.characters} == {"only"}
    assert result.assignment_report.unresolved_conflicts == 0
    assert result.assignment_report.optimization_statistics["deterministic"] is True


def test_narrator_continuity_tracks_binding_provenance_instead_of_role():
    profiles = [_profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=10, dialogue_count=8, scene_count=6, likely_recurrence=True)]
    registry = _registry()
    candidate_scores = {"lead": (_candidate("alpha", "v1", total_score=100, quality_points=95, base_priority=100),)}
    scenes = [_scene("s1", 1, ["lead"], ["lead"])]
    dialogues = [_dialogue("d1", "s1", 1, "lead", "Lead.")]
    narrator_candidates = (
        _candidate("alpha", "v1", total_score=90, quality_points=95, base_priority=100),
        _candidate("beta", "v2", total_score=90, quality_points=92, base_priority=90),
    )
    cases = [
        (
            "inherited series binding",
            SeriesVoiceBinding(
                target_kind="narrator",
                provider="beta",
                provider_voice_id="v2",
                voice_id="beta.v2",
                locked=False,
                manual_override=False,
                inherited=True,
                assignment_confidence=0.8,
                assignment_reason="continuity",
                assignment_timestamp="2026-07-29T00:00:00Z",
                provenance=AssignmentProvenance(source="series", reason="continuity", basis="history"),
                history=[],
            ),
            "v2",
            "inherited-continuity",
            False,
        ),
        (
            "locked inherited series binding",
            SeriesVoiceBinding(
                target_kind="narrator",
                provider="beta",
                provider_voice_id="v2",
                voice_id="beta.v2",
                locked=True,
                manual_override=False,
                inherited=True,
                assignment_confidence=0.8,
                assignment_reason="locked continuity",
                assignment_timestamp="2026-07-29T00:00:00Z",
                provenance=AssignmentProvenance(source="series", reason="continuity", basis="history"),
                history=[],
            ),
            "v2",
            "locked-continuity",
            True,
        ),
        (
            "unlocked manual override",
            SeriesVoiceBinding(
                target_kind="narrator",
                provider="alpha",
                provider_voice_id="v1",
                voice_id="alpha.v1",
                locked=False,
                manual_override=True,
                inherited=False,
                assignment_confidence=0.8,
                assignment_reason="manual override",
                assignment_timestamp="2026-07-29T00:00:00Z",
                provenance=AssignmentProvenance(source="manual", reason="override", basis="user"),
                history=[],
            ),
            "v1",
            "manual-continuity",
            False,
        ),
        (
            "global optimum",
            SeriesVoiceBinding(
                target_kind="narrator",
                provider="ghost",
                provider_voice_id="missing",
                voice_id="ghost.missing",
                locked=True,
                manual_override=False,
                inherited=True,
                assignment_confidence=0.8,
                assignment_reason="missing inherited voice",
                assignment_timestamp="2026-07-29T00:00:00Z",
                provenance=AssignmentProvenance(source="series", reason="continuity", basis="history"),
                unavailable=True,
                history=[],
            ),
            "v2",
            "new-assignment",
            False,
        ),
    ]

    for expected_source, narrator_binding, expected_voice_id, expected_continuity, expect_locked in cases:
        bindings = SeriesBindings(
            schema_version=1,
            series_id="series-narrator",
            narrator=narrator_binding,
            bindings=[],
            history=[],
            updated_at="2026-07-29T00:00:00Z",
        )
        budget = _budget(profiles, candidate_scores, bindings)
        report = _conflict_report(profiles, scenes, dialogues, candidate_scores, registry, series_bindings=bindings, voice_budget=budget)
        context = AssignmentContext(
            book_id="book-narrator",
            series_id="series-narrator",
            source_analysis_path="/tmp/analysis",
            source_analysis_hash="analysis-hash",
            source_voice_registry_hash="registry-hash",
            source_series_bindings_hash="bindings-hash",
            character_profiles=tuple(profiles),
            registry=registry,
            series_bindings=bindings,
            candidate_scores_by_character=candidate_scores,
            narrator_candidates=narrator_candidates,
            voice_budget=budget,
            conflict_report=report,
            config=load_settings().voice_planner,
        )
        result = assign_voices(context)
        narrator_assignment = result.voice_plan.narrator.assignment
        assert narrator_assignment is not None
        assert narrator_assignment.provider_voice_id == expected_voice_id
        assert narrator_assignment.source == expected_source
        assert narrator_assignment.continuity_status == expected_continuity
        assert narrator_assignment.locked is expect_locked
        if expected_source == "new-assignment":
            assert result.assignment_report.unavailable_voices and result.assignment_report.unavailable_voices[0]["provider_voice_id"] == "missing"


def test_assignment_results_are_stable_when_candidate_and_character_input_order_are_reversed():
    profiles_forward = [
        _profile("one", role="supporting", prominence="supporting recurring", speaking_frequency=3, dialogue_count=3, scene_count=3, likely_recurrence=True),
        _profile("two", role="supporting", prominence="supporting recurring", speaking_frequency=3, dialogue_count=3, scene_count=3, likely_recurrence=True),
    ]
    profiles_reversed = list(reversed(profiles_forward))
    registry = _registry()
    candidate_scores_forward = {
        "one": (
            _candidate("beta", "v2", total_score=70, quality_points=92, base_priority=90),
            _candidate("alpha", "v1", total_score=70, quality_points=95, base_priority=100),
        ),
        "two": (
            _candidate("beta", "v2", total_score=70, quality_points=92, base_priority=90),
            _candidate("alpha", "v1", total_score=70, quality_points=95, base_priority=100),
        ),
    }
    candidate_scores_reversed = {
        "two": candidate_scores_forward["two"],
        "one": candidate_scores_forward["one"],
    }
    scenes = [_scene("s1", 1, ["one", "two"], ["one", "two"])]
    dialogues = [_dialogue("d1", "s1", 1, "one", "One."), _dialogue("d2", "s1", 2, "two", "Two.")]

    budget_forward = _budget(profiles_forward, candidate_scores_forward)
    report_forward = _conflict_report(profiles_forward, scenes, dialogues, candidate_scores_forward, registry, voice_budget=budget_forward)
    context_forward = AssignmentContext(
        book_id="book-order",
        series_id="series-order",
        source_analysis_path="/tmp/analysis",
        source_analysis_hash="analysis-hash",
        source_voice_registry_hash="registry-hash",
        source_series_bindings_hash=None,
        character_profiles=tuple(profiles_forward),
        registry=registry,
        series_bindings=None,
        candidate_scores_by_character=candidate_scores_forward,
        voice_budget=budget_forward,
        conflict_report=report_forward,
        config=load_settings().voice_planner,
    )

    budget_reversed = _budget(profiles_reversed, candidate_scores_reversed)
    report_reversed = _conflict_report(profiles_reversed, scenes, dialogues, candidate_scores_reversed, registry, voice_budget=budget_reversed)
    context_reversed = AssignmentContext(
        book_id="book-order",
        series_id="series-order",
        source_analysis_path="/tmp/analysis",
        source_analysis_hash="analysis-hash",
        source_voice_registry_hash="registry-hash",
        source_series_bindings_hash=None,
        character_profiles=tuple(profiles_reversed),
        registry=registry,
        series_bindings=None,
        candidate_scores_by_character=candidate_scores_reversed,
        voice_budget=budget_reversed,
        conflict_report=report_reversed,
        config=load_settings().voice_planner,
    )

    forward = assign_voices(context_forward)
    reversed_result = assign_voices(context_reversed)

    assert serialize_voice_plan(forward.voice_plan) == serialize_voice_plan(reversed_result.voice_plan)
    assert serialize_assignment_report(forward.assignment_report) == serialize_assignment_report(reversed_result.assignment_report)
    assert [character.assignment.provider_voice_id for character in forward.voice_plan.characters] == ["v1", "v2"]
    assert [character.assignment.provider_voice_id for character in reversed_result.voice_plan.characters] == ["v1", "v2"]
    assert forward.voice_plan.statistics["plan_hash"] == reversed_result.voice_plan.statistics["plan_hash"]
