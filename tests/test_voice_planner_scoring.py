from __future__ import annotations

from pathlib import Path

import pytest

from app.voice_planner import (
    ScoringConfig,
    ScoreContext,
    ScoringError,
    load_character_profiles,
    load_series_bindings,
    load_voice_registry,
    rank_voice_candidates,
    score_voice_candidate,
    score_voice_candidates,
    serialize_candidate_scores,
    validate_scoring_config,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
REGISTRY_FIXTURE = FIXTURE_DIR / "voice_registry.sample.json"
BINDINGS_FIXTURE = FIXTURE_DIR / "series_bindings.sample.json"
PROFILE_FIXTURE_DIR = FIXTURE_DIR / "normalized_analysis_sample"


def _voice(
    *,
    provider: str,
    provider_voice_id: str,
    display_name: str,
    quality_score: float,
    base_priority: int,
    voice_id: str | None = None,
    availability: str = "available",
    supported_languages: list[str] | None = None,
    supported_controls: list[str] | None = None,
    archetype_tags: list[str] | None = None,
    style_tags: list[str] | None = None,
    age_presentation: str | None = None,
    gender_presentation: str | None = None,
    similarity_cluster: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "voice_id": voice_id or f"{provider}.{provider_voice_id}",
        "provider": provider,
        "provider_voice_id": provider_voice_id,
        "display_name": display_name,
        "quality_score": quality_score,
        "base_priority": base_priority,
        "availability": availability,
        "supported_languages": supported_languages or ["en-US"],
        "supported_controls": supported_controls or ["rate", "pitch"],
        "archetype_tags": archetype_tags or [],
        "style_tags": style_tags or [],
        "age_presentation": age_presentation,
        "gender_presentation": gender_presentation,
        "similarity_cluster": similarity_cluster,
    }


def _bobby_profile():
    bundle = load_character_profiles(PROFILE_FIXTURE_DIR)
    return next(profile for profile in bundle.profiles if profile.canonical_character_id == "bobby-pendragon")


def test_scoring_ranks_bindings_and_serializes_deterministically():
    registry = load_voice_registry(REGISTRY_FIXTURE)
    bindings = load_series_bindings(BINDINGS_FIXTURE)
    profile = _bobby_profile()
    context = ScoreContext(role="character", character_profile=profile, series_bindings=bindings, used_voices=(registry["voices"][1],))

    ranked = score_voice_candidates(registry["voices"], context)
    assert [candidate.provider_voice_id for candidate in ranked] == ["nova", "rachel", "af_bella"]
    assert ranked[0].binding_precedence == "unlocked manual override"
    assert ranked[0].eligible is True
    assert ranked[2].eligible is False
    assert "registry entry is unavailable" in ranked[2].ineligibility_reasons[0]
    assert sum(component.points for component in ranked[0].score_components) == ranked[0].total_score
    assert sum(component.points for component in ranked[1].score_components) == ranked[1].total_score
    assert sum(component.points for component in ranked[2].score_components) == ranked[2].total_score
    assert any(component.name == "binding_continuity" for component in ranked[0].score_components)
    assert any(component.name == "manual_override" for component in ranked[0].score_components)

    first = serialize_candidate_scores(ranked)
    second = serialize_candidate_scores(score_voice_candidates(registry["voices"], context))
    assert first == second


def test_narrator_suitability_and_deterministic_tie_breaks():
    candidates = [
        _voice(
            provider="beta",
            provider_voice_id="plain",
            display_name="Plain",
            quality_score=0.80,
            base_priority=40,
            archetype_tags=["supporting"],
            style_tags=["clear"],
        ),
        _voice(
            provider="alpha",
            provider_voice_id="narrator",
            display_name="Narrator",
            quality_score=0.80,
            base_priority=40,
            archetype_tags=["narrator"],
            style_tags=["steady"],
        ),
    ]
    context = ScoreContext(role="narrator", required_languages=("en-US",))
    ranked = rank_voice_candidates(list(reversed(candidates)), context)

    assert [candidate.provider for candidate in ranked] == ["alpha", "beta"]
    assert ranked[0].eligible is True
    assert any(component.name == "narrator_suitability" for component in ranked[0].score_components)
    assert ranked[0].tie_break_metadata["provider_sort_key"] == "alpha"
    assert ranked[1].tie_break_metadata["provider_sort_key"] == "beta"


def test_exact_tie_break_order_is_deterministic():
    candidates = [
        _voice(
            provider="beta",
            provider_voice_id="same",
            display_name="Same Beta",
            quality_score=0.80,
            base_priority=40,
        ),
        _voice(
            provider="alpha",
            provider_voice_id="same",
            display_name="Same Alpha",
            quality_score=0.80,
            base_priority=40,
        ),
    ]
    ranked = score_voice_candidates(list(reversed(candidates)), ScoreContext(role="narrator"))
    assert [candidate.provider for candidate in ranked] == ["alpha", "beta"]
    assert ranked[0].total_score == ranked[1].total_score
    assert ranked[0].binding_precedence_rank == ranked[1].binding_precedence_rank
    assert ranked[0].registry_base_priority == ranked[1].registry_base_priority
    assert ranked[0].registry_quality_points == ranked[1].registry_quality_points


def test_character_metadata_fits_age_gender_species_and_prominence():
    profile = _bobby_profile()
    candidates = [
        _voice(
            provider="match",
            provider_voice_id="teen-hero",
            display_name="Teen Hero",
            quality_score=0.70,
            base_priority=30,
            archetype_tags=["lead", "human"],
            age_presentation="teen",
            gender_presentation="male",
        ),
        _voice(
            provider="high",
            provider_voice_id="generic",
            display_name="Generic",
            quality_score=0.99,
            base_priority=90,
            archetype_tags=["supporting"],
            age_presentation="adult",
            gender_presentation="female",
        ),
    ]
    context = ScoreContext(role="character", character_profile=profile)
    ranked = score_voice_candidates(candidates, context)

    assert ranked[0].provider == "match"
    assert any(component.name == "age_fit" for component in ranked[0].score_components)
    assert any(component.name == "gender_fit" for component in ranked[0].score_components)
    assert any(component.name == "species_fit" for component in ranked[0].score_components)
    assert any(component.name == "archetype_fit" for component in ranked[0].score_components)
    assert ranked[0].total_score > ranked[1].total_score


def test_scene_reuse_and_similarity_penalties_are_applied():
    used = _voice(
        provider="alpha",
        provider_voice_id="reuse",
        display_name="Reuse",
        quality_score=0.80,
        base_priority=20,
        similarity_cluster="shared",
    )
    reused_candidate = _voice(
        provider="alpha",
        provider_voice_id="reuse",
        display_name="Reuse",
        quality_score=0.80,
        base_priority=20,
        similarity_cluster="shared",
    )
    clustered_candidate = _voice(
        provider="beta",
        provider_voice_id="clustered",
        display_name="Clustered",
        quality_score=0.80,
        base_priority=20,
        similarity_cluster="shared",
    )
    context = ScoreContext(role="narrator", used_voices=(used,))

    reused_score = score_voice_candidate(reused_candidate, context)
    clustered_score = score_voice_candidate(clustered_candidate, context)

    assert any(component.name == "voice_reuse" for component in reused_score.penalties)
    assert any(component.name == "scene_separation" for component in clustered_score.score_components)
    assert any(component.name == "similarity_cluster" for component in clustered_score.penalties)


def test_duplicate_candidates_are_rejected_deterministically():
    candidate = _voice(provider="alpha", provider_voice_id="same", display_name="Same", quality_score=0.80, base_priority=20)
    with pytest.raises(ScoringError, match="duplicate candidate registry key"):
        score_voice_candidates([candidate, candidate], ScoreContext(role="narrator"))


def test_language_and_capability_rules_are_hard_eligibility_constraints():
    context = ScoreContext(role="character", character_profile=_bobby_profile(), required_languages=("en-US",), required_controls=("pitch",))
    candidate = _voice(
        provider="alpha",
        provider_voice_id="wrong-language",
        display_name="Wrong Language",
        quality_score=0.80,
        base_priority=10,
        supported_languages=["en-GB"],
        supported_controls=["rate", "pitch"],
    )
    scored = score_voice_candidate(candidate, context)
    assert scored.eligible is False
    assert any("required language unsupported" in reason for reason in scored.ineligibility_reasons)

    candidate_controls = _voice(
        provider="alpha",
        provider_voice_id="missing-controls",
        display_name="Missing Controls",
        quality_score=0.80,
        base_priority=10,
        supported_languages=["en-US"],
        supported_controls=["rate"],
    )
    scored_controls = score_voice_candidate(candidate_controls, context)
    assert scored_controls.eligible is False
    assert any("required capability unsupported" in reason for reason in scored_controls.ineligibility_reasons)


def test_locked_binding_missing_from_candidates_fails_clearly():
    bindings = load_series_bindings(BINDINGS_FIXTURE)
    context = ScoreContext(role="narrator", series_bindings=bindings)
    with pytest.raises(ScoringError, match="locked binding references unavailable or missing voice"):
        score_voice_candidates(
            [
                _voice(provider="alpha", provider_voice_id="one", display_name="One", quality_score=0.8, base_priority=10),
                _voice(provider="beta", provider_voice_id="two", display_name="Two", quality_score=0.8, base_priority=10),
            ],
            context,
        )


def test_required_provider_explicit_exclusion_and_malformed_reference():
    candidate = _voice(provider="alpha", provider_voice_id="excluded", display_name="Excluded", quality_score=0.80, base_priority=10)
    provider_context = ScoreContext(role="narrator", required_provider="beta")
    provider_scored = score_voice_candidate(candidate, provider_context)
    assert provider_scored.eligible is False
    assert any("required provider" in reason for reason in provider_scored.ineligibility_reasons)

    excluded_context = ScoreContext(role="narrator", excluded_registry_keys=("alpha::excluded",))
    excluded_scored = score_voice_candidate(candidate, excluded_context)
    assert excluded_scored.eligible is False
    assert any("voice is explicitly excluded" in reason for reason in excluded_scored.ineligibility_reasons)

    with pytest.raises(ScoringError, match="malformed voice reference"):
        score_voice_candidate({"provider": "alpha"}, ScoreContext(role="narrator"))


def test_invalid_scoring_config_is_rejected():
    cfg = ScoringConfig.from_mapping({"continuity_bonus": 99, "unknown_metadata_behavior": "neutral"})
    assert cfg.continuity_bonus == 99
    assert cfg.manual_override_bonus == 1500

    errors = validate_scoring_config({"quality_weight": -1, "unknown_metadata_behavior": "mystery"})
    assert any("quality_weight" in message for message in errors)
    assert any("unknown_metadata_behavior" in message for message in errors)
    with pytest.raises(ScoringError):
        ScoringConfig.from_mapping({"quality_weight": -1})
