from __future__ import annotations

from pathlib import Path

import pytest

from app.voice_planner import (
    AssignmentProvenance,
    BudgetConfig,
    BudgetContext,
    CharacterProfile,
    ReassignmentHistoryEntry,
    ScoreContext,
    SeriesBindings,
    SeriesVoiceBinding,
    calculate_voice_budget,
    classify_character_tier,
    load_character_profiles,
    score_voice_candidate,
    score_voice_candidates,
    serialize_voice_budget,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
PROFILE_FIXTURE_DIR = FIXTURE_DIR / "normalized_analysis_sample"


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


def test_character_tiers_are_deterministic_and_unknown_metadata_stays_conservative():
    assert (
        classify_character_tier(
            _profile(
                "lead",
                role="protagonist",
                prominence="major recurring",
                speaking_frequency=12,
                dialogue_count=9,
                scene_count=7,
                likely_recurrence=True,
                first_appearance_order=1,
            )
        )
        == "primary or lead characters"
    )
    assert (
        classify_character_tier(
            _profile(
                "major",
                role="supporting",
                prominence="major recurring",
                speaking_frequency=7,
                dialogue_count=5,
                scene_count=6,
                likely_recurrence=True,
                first_appearance_order=4,
            )
        )
        == "major recurring characters"
    )
    assert classify_character_tier(_profile("unknown")) == "unresolved speakers"


def test_abundant_inventory_preserves_distinct_capacity_and_is_serializable():
    bundle = load_character_profiles(PROFILE_FIXTURE_DIR)
    assert bundle.profiles, "fixture should load at least one normalized profile"

    profiles = [
        _profile("narrator", role="narrator", prominence="primary", speaking_frequency=1, dialogue_count=1, scene_count=1),
        _profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=12, dialogue_count=9, scene_count=7, likely_recurrence=True, first_appearance_order=1),
        _profile("major", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=6, likely_recurrence=True, first_appearance_order=2),
        _profile("support", role="supporting", prominence="supporting recurring", speaking_frequency=4, dialogue_count=3, scene_count=4, likely_recurrence=True, first_appearance_order=5),
        _profile("minor", speaking_frequency=1, dialogue_count=1, scene_count=1),
    ]
    candidates = [
        _voice("alpha", "lead", archetype_tags=["lead"], style_tags=["warm"]),
        _voice("beta", "major", archetype_tags=["supporting"], style_tags=["calm"]),
        _voice("gamma", "support", archetype_tags=["supporting"], style_tags=["light"]),
        _voice("delta", "minor", archetype_tags=["supporting"], style_tags=["quick"]),
        _voice("epsilon", "extra", archetype_tags=["supporting"], style_tags=["quick"]),
    ]
    scores = score_voice_candidates(candidates, ScoreContext(role="character", character_profile=profiles[1]))
    narrator_score = score_voice_candidate(_voice("openai", "narrator", archetype_tags=["narrator"], style_tags=["steady"]), ScoreContext(role="narrator"))
    scores = [narrator_score, *scores]

    budget = calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(scores), narrator_required=True))
    rerun = calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(scores), narrator_required=True))

    assert budget.scarcity_level in {"none", "low"}
    assert budget.total_eligible_voice_inventory == 6
    assert budget.narrator_reserved_voice_count == 1
    assert budget.voices_available_for_new_planning == 5
    assert budget.role_tier_capacity["primary or lead characters"] >= 1
    assert budget.role_tier_capacity["major recurring characters"] >= 1
    assert budget.role_tier_capacity["supporting recurring characters"] >= 1
    assert "one-scene or one-off speakers" in budget.shareable_tiers
    assert serialize_voice_budget(budget) == serialize_voice_budget(rerun)


def test_limited_inventory_allocates_lower_tier_reuse_first():
    profiles = [
        _profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=13, dialogue_count=11, scene_count=8, likely_recurrence=True),
        _profile("major", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=6, likely_recurrence=True),
        _profile("support", role="supporting", prominence="supporting recurring", speaking_frequency=4, dialogue_count=3, scene_count=4, likely_recurrence=True),
        _profile("minor", speaking_frequency=1, dialogue_count=1, scene_count=1),
    ]
    scores = score_voice_candidates(
        [
            _voice("alpha", "one", archetype_tags=["lead"], style_tags=["warm"]),
            _voice("beta", "two", archetype_tags=["supporting"], style_tags=["calm"]),
        ],
        ScoreContext(role="character", character_profile=profiles[0]),
    )
    budget = calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(scores), narrator_required=True))

    assert budget.scarcity_level in {"moderate", "high", "critical"}
    assert budget.role_tier_capacity["primary or lead characters"] >= budget.role_tier_capacity["minor speaking characters"]
    assert budget.reuse_allowances["minor speaking characters"].max_reuse >= budget.reuse_allowances["primary or lead characters"].max_reuse
    assert any("lower tiers absorb reuse" in decision for decision in budget.downgrade_decisions)


def test_locked_binding_pressure_counts_valid_locks_and_reports_unavailable_locked_voice():
    bindings = SeriesBindings(
        schema_version=1,
        series_id="pendragon",
        narrator=SeriesVoiceBinding(
            target_kind="narrator",
            provider="openai",
            provider_voice_id="narrator",
            voice_id="openai.narrator",
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
            SeriesVoiceBinding(target_kind="character", canonical_character_id="lead", provider="alpha", provider_voice_id="one", voice_id="alpha.one", locked=True, manual_override=True, inherited=False, history=[]),
            SeriesVoiceBinding(target_kind="character", canonical_character_id="major", provider="alpha", provider_voice_id="one", voice_id="alpha.one", locked=True, manual_override=False, inherited=True, history=[]),
            SeriesVoiceBinding(target_kind="character", canonical_character_id="missing", provider="ghost", provider_voice_id="nope", voice_id="ghost.nope", locked=True, manual_override=False, inherited=True, unavailable=True, history=[]),
        ],
        history=[ReassignmentHistoryEntry(target_kind="character", canonical_character_id="lead", previous_provider="alpha", previous_provider_voice_id="old", new_provider="alpha", new_provider_voice_id="one", timestamp="2026-07-29T00:00:00Z", reason="lock", source="manual", prior_locked=True, manual_change=True)],
        updated_at="2026-07-29T00:00:00Z",
    )
    profiles = [
        _profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=12, dialogue_count=10, scene_count=7, likely_recurrence=True),
        _profile("major", role="supporting", prominence="major recurring", speaking_frequency=8, dialogue_count=6, scene_count=6, likely_recurrence=True),
    ]
    scores = score_voice_candidates(
        [
            _voice("openai", "narrator", archetype_tags=["narrator"], style_tags=["steady"]),
            _voice("alpha", "one", archetype_tags=["lead"], style_tags=["warm"]),
        ],
        ScoreContext(role="character", character_profile=profiles[0]),
    )

    budget = calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(scores), series_bindings=bindings, narrator_required=True))

    assert budget.locked_binding_voice_count == 2
    assert any("ghost.nope" in reason for reason in budget.unresolved_capacity_conflicts)
    assert any("duplicate locked voice" in reason for reason in budget.unresolved_capacity_conflicts)
    assert budget.protected_characters == ["lead", "major"]


def test_critical_inventory_is_explicit_about_unavoidable_reuse():
    profiles = [
        _profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=14, dialogue_count=12, scene_count=9, likely_recurrence=True),
        _profile("minor", speaking_frequency=1, dialogue_count=1, scene_count=1),
        _profile("oneoff", speaking_frequency=1, dialogue_count=1, scene_count=1),
    ]
    scores = [score_voice_candidate(_voice("alpha", "solo", archetype_tags=["narrator", "lead"], style_tags=["steady"]), ScoreContext(role="narrator"))]
    budget = calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(scores), narrator_required=True, config=BudgetConfig.default()))

    assert budget.scarcity_level == "critical"
    assert budget.voices_available_for_new_planning == 0
    assert budget.role_tier_capacity["primary or lead characters"] <= 1
    assert budget.reuse_allowances["one-scene or one-off speakers"].policy in {"permitted", "preferred_due_to_scarcity"}
    assert budget.summary_statistics["unavoidable_reuse"] >= 1


def test_provider_constrained_inventory_recalculates_and_config_validation_is_clear():
    profiles = [_profile("lead", role="protagonist", prominence="major recurring", speaking_frequency=10, dialogue_count=8, scene_count=6, likely_recurrence=True)]
    scores = score_voice_candidates(
        [
            _voice("alpha", "one", supported_languages=["en-GB"]),
            _voice("beta", "two", supported_languages=["en-US"]),
        ],
        ScoreContext(role="character", character_profile=profiles[0]),
    )
    budget = calculate_voice_budget(BudgetContext(character_profiles=tuple(profiles), candidate_scores=tuple(scores), narrator_required=False))
    assert budget.total_eligible_voice_inventory == 2
    assert budget.voices_available_for_new_planning >= 1

    with pytest.raises(ValueError):
        BudgetConfig.from_mapping({"narrator_reserve": -1})
