from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.voice_planner import (
    AssignmentProvenance,
    BudgetContext,
    CharacterProfile,
    ConflictConfig,
    ConflictReport,
    SceneConflictContext,
    ScoreContext,
    SeriesBindings,
    SeriesVoiceBinding,
    VoiceBudget,
    analyze_scene_conflicts,
    build_character_pairs,
    calculate_voice_budget,
    classify_character_tier,
    load_character_profiles,
    load_voice_registry,
    score_voice_candidate,
    score_voice_candidates,
    serialize_conflict_report,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
PROFILE_FIXTURE_DIR = FIXTURE_DIR / "normalized_analysis_sample"
VOICE_REGISTRY_FIXTURE = FIXTURE_DIR / "voice_registry.sample.json"


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
    archetype_tags: list[str] | None = None,
    style_tags: list[str] | None = None,
    supported_languages: list[str] | None = None,
    supported_controls: list[str] | None = None,
    similarity_cluster: str | None = None,
    voice_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "voice_id": voice_id or f"{provider}.{provider_voice_id}",
        "provider": provider,
        "provider_voice_id": provider_voice_id,
        "display_name": display_name or provider_voice_id,
        "availability": availability,
        "quality_score": quality_score,
        "base_priority": base_priority,
        "archetype_tags": archetype_tags or [],
        "style_tags": style_tags or [],
        "supported_languages": supported_languages or ["en-US"],
        "supported_controls": supported_controls or ["rate", "pitch"],
        "similarity_cluster": similarity_cluster,
    }


def _scene(scene_id: str, order: int, character_ids: list[str], speaking_character_ids: list[str]) -> dict[str, object]:
    return {
        "scene_id": scene_id,
        "scene_order": order,
        "character_ids": character_ids,
        "speaking_character_ids": speaking_character_ids,
    }


def _dialogue(dialogue_id: str, scene_id: str, order: int, speaker_character_id: str | None, text: str) -> dict[str, object]:
    payload = {
        "dialogue_id": dialogue_id,
        "scene_id": scene_id,
        "order": order,
        "quote_count": 1,
        "word_count": len(text.split()),
        "text": text,
    }
    if speaker_character_id is not None:
        payload["speaker_character_id"] = speaker_character_id
    else:
        payload["speaker_character_id"] = None
        payload["speaker_label"] = "Unknown"
    return payload


def _context(
    profiles: list[CharacterProfile],
    scenes: list[dict[str, object]],
    dialogues: list[dict[str, object]],
    *,
    candidate_scores_by_character: dict[str, tuple[object, ...]] | None = None,
    series_bindings: SeriesBindings | None = None,
    voice_budget: VoiceBudget | None = None,
    config: ConflictConfig | None = None,
    registry: dict[str, object] | None = None,
) -> SceneConflictContext:
    return SceneConflictContext(
        character_profiles=tuple(profiles),
        scene_records=tuple(scenes),
        dialogue_records=tuple(dialogues),
        candidate_scores_by_character=candidate_scores_by_character or {},
        series_bindings=series_bindings,
        voice_budget=voice_budget,
        voice_registry=registry,
        config=config or ConflictConfig.default(),
    )


def _candidate_scores_for(profile: CharacterProfile, voices: list[dict[str, object]], role: str = "character") -> tuple[object, ...]:
    return tuple(score_voice_candidates(voices, ScoreContext(role=role, character_profile=profile)))


def test_build_character_pairs_is_deterministic_and_non_interacting_pairs_stay_conflict_free():
    profiles = [
        _profile("alpha", role="supporting", prominence="supporting recurring", speaking_frequency=2, dialogue_count=2, scene_count=1),
        _profile("beta", role="supporting", prominence="supporting recurring", speaking_frequency=2, dialogue_count=2, scene_count=1),
    ]
    scenes = [
        _scene("scene-001", 1, ["alpha"], ["alpha"]),
        _scene("scene-002", 2, ["beta"], ["beta"]),
    ]
    dialogues = [
        _dialogue("d1", "scene-001", 1, "alpha", "Alpha only."),
        _dialogue("d2", "scene-002", 2, "beta", "Beta only."),
    ]
    registry = load_voice_registry(VOICE_REGISTRY_FIXTURE)
    candidate_scores = {
        "alpha": _candidate_scores_for(profiles[0], [
            _voice("elevenlabs", "rachel", similarity_cluster="cluster-r"),
        ]),
        "beta": _candidate_scores_for(profiles[1], [
            _voice("openai", "nova", similarity_cluster="cluster-o"),
        ]),
    }
    report = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, registry=registry))
    rerun = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, registry=registry))

    assert build_character_pairs(profiles) == [("alpha", "beta")]
    assert len(report.pair_evidence) == 1
    evidence = report.pair_evidence[0]
    assert evidence.shared_scene_count == 0
    assert evidence.shared_speaking_scene_count == 0
    assert evidence.dialogue_proximity.adjacent_dialogue_pairs == 0
    assert evidence.dialogue_proximity.alternating_dialogue_transitions == 0
    assert report.conflicts == []
    assert serialize_conflict_report(report) == serialize_conflict_report(rerun)


def test_frequent_shared_speaking_scenes_create_hard_distinction_conflicts():
    bundle = load_character_profiles(PROFILE_FIXTURE_DIR)
    profiles = [profile for profile in bundle.profiles if profile.canonical_character_id in {"bobby-pendragon", "courtney-chetwynde"}]
    scenes = [
        _scene("scene-001", 1, ["bobby-pendragon", "courtney-chetwynde"], ["bobby-pendragon", "courtney-chetwynde"]),
        _scene("scene-002", 2, ["bobby-pendragon", "courtney-chetwynde"], ["bobby-pendragon", "courtney-chetwynde"]),
    ]
    dialogues = [
        _dialogue("d1", "scene-001", 1, "bobby-pendragon", "We should go."),
        _dialogue("d2", "scene-001", 2, "courtney-chetwynde", "I am coming."),
        _dialogue("d3", "scene-002", 3, "bobby-pendragon", "This is strange."),
        _dialogue("d4", "scene-002", 4, "courtney-chetwynde", "Let's move."),
    ]
    registry = load_voice_registry(VOICE_REGISTRY_FIXTURE)
    candidate_scores = {
        "bobby-pendragon": _candidate_scores_for(profiles[0], [
            _voice("kokoro", "af_bella", similarity_cluster="cluster-k1"),
            _voice("openai", "nova", similarity_cluster="cluster-o"),
        ]),
        "courtney-chetwynde": _candidate_scores_for(profiles[1], [
            _voice("kokoro", "af_bella", similarity_cluster="cluster-k1"),
            _voice("openai", "nova", similarity_cluster="cluster-o"),
        ]),
    }
    report = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, registry=registry))
    pair = report.conflicts[0]

    assert pair.character_a_id == "bobby-pendragon"
    assert pair.character_b_id == "courtney-chetwynde"
    assert pair.shared_scene_count == 2
    assert pair.shared_speaking_scene_count == 2
    assert pair.dialogue_proximity.alternating_dialogue_transitions >= 2
    assert pair.severity in {"high", "critical"}
    assert pair.same_voice_prohibition is True
    assert pair.distinct_voice_requirement is True
    assert any(category == "same-scene speaking conflict" for category in pair.categories)
    assert any(category == "alternating-dialogue conflict" for category in pair.categories)


@pytest.mark.parametrize(
    "policy, expected_prohibition, expected_reason_fragment",
    [
        ("allow", False, "narrator sharing allowed"),
        ("discourage", False, "narrator sharing discouraged"),
        ("prohibit", True, "narrator sharing prohibited"),
    ],
)
def test_narrator_sharing_policy_modes(policy: str, expected_prohibition: bool, expected_reason_fragment: str):
    narrator = _profile("narrator", role="narrator", prominence="primary", speaking_frequency=1, dialogue_count=1, scene_count=1)
    major = _profile("major", role="protagonist", prominence="major recurring", speaking_frequency=10, dialogue_count=8, scene_count=6, likely_recurrence=True)
    profiles = [narrator, major]
    scenes = [_scene("scene-001", 1, ["narrator", "major"], ["narrator", "major"])]
    dialogues = [
        _dialogue("d1", "scene-001", 1, "narrator", "Once upon a time."),
        _dialogue("d2", "scene-001", 2, "major", "Let's move."),
    ]
    voice = _voice("openai", "nova", similarity_cluster="cluster-o")
    candidate_scores = {
        "narrator": _candidate_scores_for(narrator, [voice], role="narrator"),
        "major": _candidate_scores_for(major, [voice], role="character"),
    }
    report = analyze_scene_conflicts(_context(
        profiles,
        scenes,
        dialogues,
        candidate_scores_by_character=candidate_scores,
        config=replace(ConflictConfig.default(), narrator_separation_policy=policy),
    ))
    pair = report.conflicts[0]

    assert pair.narrator_conflict is True
    assert pair.same_voice_prohibition is expected_prohibition
    assert expected_reason_fragment in pair.conflict_reason
    if policy == "prohibit":
        assert pair.reuse_eligibility is False
    else:
        assert pair.reuse_eligibility is True


def test_locked_same_voice_conflicts_are_reported_without_mutating_bindings():
    profiles = [
        _profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=12, dialogue_count=10, scene_count=7, likely_recurrence=True),
        _profile("major", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=6, likely_recurrence=True),
    ]
    scenes = [_scene("scene-001", 1, ["lead", "major"], ["lead", "major"])]
    dialogues = [
        _dialogue("d1", "scene-001", 1, "lead", "Go now."),
        _dialogue("d2", "scene-001", 2, "major", "I will."),
    ]
    bindings = SeriesBindings(
        schema_version=1,
        series_id="pendragon",
        narrator=SeriesVoiceBinding(
            target_kind="narrator",
            provider="openai",
            provider_voice_id="nova",
            voice_id="openai.nova",
            locked=True,
            manual_override=True,
            inherited=False,
            assignment_confidence=1.0,
            assignment_reason="locked narrator",
            assignment_timestamp="2026-07-29T00:00:00Z",
            provenance=AssignmentProvenance(source="manual", reason="locked", basis="direct"),
            user_notes=None,
            unavailable=False,
            history=[],
        ),
        bindings=[
            SeriesVoiceBinding(target_kind="character", canonical_character_id="lead", provider="openai", provider_voice_id="nova", voice_id="openai.nova", locked=True, manual_override=True, inherited=False, history=[]),
            SeriesVoiceBinding(target_kind="character", canonical_character_id="major", provider="openai", provider_voice_id="nova", voice_id="openai.nova", locked=True, manual_override=False, inherited=True, history=[]),
        ],
        history=[],
        updated_at="2026-07-29T00:00:00Z",
    )
    candidate_scores = {
        "lead": _candidate_scores_for(profiles[0], [_voice("openai", "nova", similarity_cluster="cluster-o")]),
        "major": _candidate_scores_for(profiles[1], [_voice("openai", "nova", similarity_cluster="cluster-o")]),
    }
    before = json.dumps({"narrator": bindings.narrator.__dict__ if bindings.narrator else None, "bindings": [binding.__dict__ for binding in bindings.bindings]}, sort_keys=True, default=str)
    report = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, series_bindings=bindings))
    after = json.dumps({"narrator": bindings.narrator.__dict__ if bindings.narrator else None, "bindings": [binding.__dict__ for binding in bindings.bindings]}, sort_keys=True, default=str)
    pair = report.conflicts[0]

    assert before == after
    assert pair.locked_binding_conflict is True
    assert pair.same_voice_prohibition is True
    assert pair.unresolved_conflict_status == "hard"
    assert any(category == "locked-binding conflict" for category in pair.categories)


def test_similarity_cluster_overlap_sets_penalty_and_can_prohibit_high_conflict_pairs():
    profiles = [
        _profile("a", role="protagonist", prominence="major recurring", speaking_frequency=10, dialogue_count=8, scene_count=6, likely_recurrence=True),
        _profile("b", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=6, likely_recurrence=True),
    ]
    scenes = [
        _scene("scene-001", 1, ["a", "b"], ["a", "b"]),
        _scene("scene-002", 2, ["a", "b"], ["a", "b"]),
        _scene("scene-003", 3, ["a", "b"], ["a", "b"]),
    ]
    dialogues = [
        _dialogue("d1", "scene-001", 1, "a", "A."),
        _dialogue("d2", "scene-001", 2, "b", "B."),
        _dialogue("d3", "scene-002", 3, "a", "A again."),
        _dialogue("d4", "scene-002", 4, "b", "B again."),
        _dialogue("d5", "scene-003", 5, "a", "A third."),
        _dialogue("d6", "scene-003", 6, "b", "B third."),
    ]
    voices = [
        _voice("provider1", "v1", similarity_cluster="cluster-same"),
        _voice("provider2", "v2", similarity_cluster="cluster-same"),
    ]
    candidate_scores = {
        "a": _candidate_scores_for(profiles[0], voices),
        "b": _candidate_scores_for(profiles[1], voices),
    }
    report = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, registry={"voices": voices}))
    pair = report.conflicts[0]

    assert pair.similarity_cluster_penalty > 0
    assert pair.same_similarity_cluster is True
    assert pair.severity in {"high", "critical"}
    assert any(category == "similarity-cluster conflict" for category in pair.categories)


def test_critical_scarcity_relaxes_lower_tier_conflicts_first():
    profiles = [
        _profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=12, dialogue_count=10, scene_count=7, likely_recurrence=True),
        _profile("support", role="supporting", prominence="supporting recurring", speaking_frequency=4, dialogue_count=3, scene_count=4, likely_recurrence=True),
        _profile("minor", speaking_frequency=1, dialogue_count=1, scene_count=1),
    ]
    scenes = [
        _scene("scene-001", 1, ["lead", "support"], ["lead", "support"]),
        _scene("scene-002", 2, ["lead", "support"], ["lead", "support"]),
        _scene("scene-003", 3, ["support", "minor"], ["support", "minor"]),
        _scene("scene-004", 4, ["support", "minor"], ["support", "minor"]),
    ]
    dialogues = [
        _dialogue("d1", "scene-001", 1, "lead", "Lead."),
        _dialogue("d2", "scene-001", 2, "support", "Support."),
        _dialogue("d3", "scene-002", 3, "lead", "Lead again."),
        _dialogue("d4", "scene-002", 4, "support", "Support again."),
        _dialogue("d5", "scene-003", 5, "support", "Support third."),
        _dialogue("d6", "scene-003", 6, "minor", "Minor."),
        _dialogue("d7", "scene-004", 7, "support", "Support fourth."),
        _dialogue("d8", "scene-004", 8, "minor", "Minor again."),
    ]
    candidate_scores = {
        "lead": _candidate_scores_for(profiles[0], [_voice("provider1", "v1", similarity_cluster="cluster-x")]),
        "support": _candidate_scores_for(profiles[1], [_voice("provider2", "v2", similarity_cluster="cluster-x")]),
        "minor": _candidate_scores_for(profiles[2], [_voice("provider3", "v3", similarity_cluster="cluster-y")]),
    }
    budget = replace(
        calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(candidate_scores["lead"] + candidate_scores["support"] + candidate_scores["minor"]), narrator_required=False)),
        scarcity_level="critical",
    )
    report = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, voice_budget=budget))
    lookup = {(tuple(sorted((pair.character_a_id, pair.character_b_id)))): pair for pair in report.conflicts}

    assert budget.scarcity_level == "critical"
    assert lookup[("lead", "support")].unresolved_conflict_status in {"hard", "relaxable"}
    assert lookup[("minor", "support")].applicable_scarcity_relaxation in {"scarcity-relaxed", "lower-tier-reuse"}
    assert lookup[("minor", "support")].reuse_eligibility is True
    assert any(pair.unresolved_conflict_status in {"hard", "relaxable"} for pair in report.conflicts)


def test_conflict_report_serializes_deterministically_and_consumes_real_fixture_inputs():
    bundle = load_character_profiles(PROFILE_FIXTURE_DIR)
    registry = load_voice_registry(VOICE_REGISTRY_FIXTURE)
    profiles = [profile for profile in bundle.profiles if profile.canonical_character_id in {"bobby-pendragon", "courtney-chetwynde", "uncle-press"}]
    scenes = [
        _scene("scene-001", 1, ["bobby-pendragon", "courtney-chetwynde"], ["bobby-pendragon", "courtney-chetwynde"]),
        _scene("scene-002", 2, ["bobby-pendragon", "uncle-press"], ["bobby-pendragon", "uncle-press"]),
        _scene("scene-003", 3, ["courtney-chetwynde", "uncle-press"], ["courtney-chetwynde", "uncle-press"]),
    ]
    dialogues = [
        _dialogue("d1", "scene-001", 1, "bobby-pendragon", "We should go."),
        _dialogue("d2", "scene-001", 2, "courtney-chetwynde", "I am coming."),
        _dialogue("d3", "scene-002", 3, "bobby-pendragon", "This is strange."),
        _dialogue("d4", "scene-002", 4, None, "[unresolved speaker]"),
        _dialogue("d5", "scene-002", 5, "uncle-press", "Trust me."),
        _dialogue("d6", "scene-003", 6, "courtney-chetwynde", "Look there."),
        _dialogue("d7", "scene-003", 7, "uncle-press", "Follow the path."),
    ]
    candidate_scores = {
        profile.canonical_character_id: _candidate_scores_for(profile, [
            _voice("kokoro", "af_bella", similarity_cluster="cluster-k1"),
            _voice("openai", "nova", similarity_cluster="cluster-o"),
            _voice("elevenlabs", "rachel", similarity_cluster="cluster-r"),
        ], role="character")
        for profile in profiles
    }
    budget = calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(score for scores in candidate_scores.values() for score in scores), narrator_required=True))
    report = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, voice_budget=budget, registry=registry))
    rerun = analyze_scene_conflicts(_context(profiles, scenes, dialogues, candidate_scores_by_character=candidate_scores, voice_budget=budget, registry=registry))

    assert isinstance(report, ConflictReport)
    assert serialize_conflict_report(report) == serialize_conflict_report(rerun)
    assert report.summary["pair_count"] == len(report.pair_evidence)
    assert report.summary["conflict_count"] == len(report.conflicts)
