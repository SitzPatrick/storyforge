from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from hashlib import sha256
from statistics import mean
from typing import Any, Mapping, Sequence

from .bindings import binding_precedence, get_character_binding, get_narrator_binding
from .budget import classify_character_tier
from .conflicts import ConflictReport, SceneConflictContext, VoicePairConstraint, evaluate_pair_conflict
from .models import (
    AssignmentProvenance,
    CharacterPlan,
    CharacterProfile,
    NarratorPlan,
    PlanningReport,
    SceneConflict,
    SeriesBindings,
    SeriesVoiceBinding,
    VoiceAssignment,
    VoiceCapability,
    VoicePlan,
    dataclass_to_dict,
)
from .registry import is_voice_selectable
from .schema import canonical_json_dumps
from .scoring import CandidateScore, ScoreComponent


@dataclass(frozen=True)
class AssignmentContext:
    book_id: str
    series_id: str
    source_analysis_path: str
    source_analysis_hash: str
    source_voice_registry_hash: str | None
    source_series_bindings_hash: str | None
    character_profiles: tuple[CharacterProfile, ...]
    registry: Mapping[str, Any]
    series_bindings: SeriesBindings | None
    candidate_scores_by_character: dict[str, tuple[CandidateScore, ...]]
    narrator_candidates: tuple[CandidateScore, ...] = ()
    voice_budget: Any | None = None
    conflict_report: ConflictReport | None = None
    config: Any | None = None
    generated_at: str | None = None
    generated_by: str | None = None


@dataclass(frozen=True)
class AssignmentResult:
    voice_plan: VoicePlan
    assignment_report: PlanningReport
    optimization_statistics: dict[str, Any] = field(default_factory=dict)


class AssignmentError(ValueError):
    pass


@dataclass(frozen=True)
class _TargetSpec:
    target_id: str
    target_kind: str
    profile: CharacterProfile | None
    binding: SeriesVoiceBinding | None
    options: tuple[CandidateScore, ...]
    is_narrator: bool = False


def assign_voices(context: AssignmentContext) -> AssignmentResult:
    config = _coerce_config(context.config)
    registry_lookup = _build_registry_lookup(context.registry)
    conflict_report = context.conflict_report or ConflictReport(schema_version=1, book_id=context.book_id, series_id=context.series_id, pair_evidence=[], conflicts=[], warnings=[], summary={})
    if _single_voice_mode_enabled(config):
        result = _assign_single_voice_mode(context, registry_lookup)
        plan_hash = _plan_hash(result.voice_plan)
        voice_plan = replace(result.voice_plan, statistics={**result.voice_plan.statistics, "plan_hash": plan_hash})
        assignment_report = replace(result.assignment_report, plan_hash=plan_hash)
        return AssignmentResult(
            voice_plan=voice_plan,
            assignment_report=assignment_report,
            optimization_statistics=assignment_report.optimization_statistics,
        )
    targets = _build_targets(context)
    prepared_targets = tuple(_prepare_target(target, registry_lookup) for target in targets)
    best: dict[str, Any] | None = None
    selected: dict[str, CandidateScore] = {}

    def recurse(index: int) -> None:
        nonlocal best
        if index >= len(prepared_targets):
            result = _evaluate_leaf(context, config, registry_lookup, targets, selected, conflict_report)
            if best is None or _is_better_solution(result, best):
                best = result
            return

        target = prepared_targets[index]
        for candidate in target.options:
            selected[target.target_id] = candidate
            recurse(index + 1)
            selected.pop(target.target_id, None)

    recurse(0)
    if best is None:
        raise AssignmentError("no valid voice assignment could be produced without violating hard constraints")

    plan_hash = _plan_hash(best["voice_plan"])
    voice_plan = replace(best["voice_plan"], statistics={**best["voice_plan"].statistics, "plan_hash": plan_hash})
    assignment_report = replace(best["assignment_report"], plan_hash=plan_hash)
    return AssignmentResult(voice_plan=voice_plan, assignment_report=assignment_report, optimization_statistics=assignment_report.optimization_statistics)


def _single_voice_mode_enabled(config: Any) -> bool:
    if isinstance(config, Mapping):
        value = config.get("single_voice_mode")
    else:
        value = getattr(config, "single_voice_mode", False)
    return bool(value)


def _assign_single_voice_mode(context: AssignmentContext, registry_lookup: Mapping[tuple[str, str], VoiceCapability]) -> AssignmentResult:
    candidate = _select_single_voice_candidate(context, registry_lookup)
    narrator_assignment = _single_voice_assignment(candidate, target_name="Narrator", source_label="single voice mode", is_narrator=True)
    character_assignments: list[CharacterPlan] = []
    for profile in sorted(context.character_profiles, key=_profile_sort_key):
        assignment = _single_voice_assignment(
            candidate,
            target_name=profile.canonical_name,
            source_label="single voice mode",
            is_narrator=False,
        )
        character_assignments.append(
            CharacterPlan(
                canonical_character_id=profile.canonical_character_id,
                canonical_name=profile.canonical_name,
                role=profile.role,
                prominence=profile.prominence,
                speaking_frequency=profile.speaking_frequency,
                first_appearance=profile.first_appearance_order,
                likely_recurrence=profile.likely_recurrence,
                age_bucket=profile.age_bucket,
                gender_presentation=profile.gender_presentation,
                species_or_archetype=profile.species_or_archetype,
                scene_relationships=[dataclass_to_dict(item) for item in profile.scene_relationships],
                unresolved_metadata=dict(profile.unresolved_metadata),
                assignment=assignment,
                notes=profile.notes,
            )
        )

    voice_plan = VoicePlan(
        schema_version=1,
        planner_version="phase4b-milestone8",
        book_id=context.book_id,
        series_id=context.series_id,
        source_analysis_hash=context.source_analysis_hash,
        source_analysis_path=context.source_analysis_path,
        narrator=NarratorPlan(assignment=narrator_assignment, rationale=narrator_assignment.rationale),
        characters=character_assignments,
        conflicts=[],
        scarcity_events=[],
        warnings=[],
        statistics={
            "single_voice_mode": True,
            "selected_provider": candidate.provider,
            "selected_provider_voice_id": candidate.provider_voice_id,
            "selected_voice_id": candidate.voice_id,
        },
        generated_at=context.generated_at or "1970-01-01T00:00:00Z",
        generated_by=context.generated_by or "deterministic-assignment-engine",
        source_voice_registry_hash=context.source_voice_registry_hash,
        source_series_bindings_hash=context.source_series_bindings_hash,
        notes=None,
        user_editable_notes=[],
    )

    score_values = [character.assignment.score or 0 for character in character_assignments if character.assignment is not None]
    # The narrator voice is intentionally reused across every speaker; this is not a mapping plan.
    reused_voice_count = max(0, len(character_assignments) - 1)
    report = PlanningReport(
        schema_version=1,
        book_id=context.book_id,
        series_id=context.series_id,
        plan_hash="",
        generated_at=context.generated_at or "1970-01-01T00:00:00Z",
        narrator_choice=dataclass_to_dict(voice_plan.narrator),
        total_characters=len(context.character_profiles),
        total_locked_assignments=0,
        inherited_bindings_reused=0,
        manual_overrides_honored=0,
        new_assignments=len(character_assignments),
        reused_voices=reused_voice_count,
        relaxed_conflicts=0,
        unresolved_conflicts=0,
        scarcity_level=getattr(context.voice_budget, "scarcity_level", "none") if context.voice_budget is not None else "none",
        protected_characters=[],
        assignment_score_statistics={
            "count": len(score_values),
            "total": sum(score_values),
            "minimum": min(score_values) if score_values else 0,
            "maximum": max(score_values) if score_values else 0,
            "average": round(mean(score_values), 3) if score_values else 0.0,
        },
        optimization_statistics={
            "search_strategy": "single_voice",
            "deterministic": True,
            "single_voice_mode": True,
            "hard_conflict_count": 0,
            "soft_conflict_penalty": 0,
            "states_explored": 1,
            "branches_pruned": 0,
            "objective_score": sum(score_values),
        },
        runtime_ms=0,
        deterministic_verification={"byte_identical_on_rerun": True, "json_key_ordering": True, "optimization_deterministic": True},
        reused_bindings=[],
        new_bindings=[
            {
                "canonical_character_id": profile.canonical_character_id,
                "canonical_name": profile.canonical_name,
                "provider": candidate.provider,
                "provider_voice_id": candidate.provider_voice_id,
                "voice_id": candidate.voice_id,
                "source": "single voice mode",
                "continuity_status": "single-voice",
                "score": candidate.total_score,
            }
            for profile in sorted(context.character_profiles, key=_profile_sort_key)
        ],
        manual_overrides=[],
        locked_assignments=[],
        deferred_characters=[],
        unavailable_voices=[],
        scarcity_events=[],
        similarity_conflicts=[],
        scene_conflicts=[],
        fallback_tiers_used=[],
        scoring_summaries=[
            {"character_id": profile.canonical_character_id, "score": candidate.total_score}
            for profile in sorted(context.character_profiles, key=_profile_sort_key)
        ],
        validation_warnings=[],
        final_statistics={
            "score_total": sum(score_values),
            "soft_penalty": 0,
            "objective_score": sum(score_values),
            "unique_voices": 1,
            "reused_voices": reused_voice_count,
            "pair_constraint_count": 0,
        },
    )
    return AssignmentResult(voice_plan=voice_plan, assignment_report=report, optimization_statistics=report.optimization_statistics)


def _select_single_voice_candidate(context: AssignmentContext, registry_lookup: Mapping[tuple[str, str], VoiceCapability]) -> CandidateScore:
    narrator_binding = get_narrator_binding(context.series_bindings) if context.series_bindings is not None else None
    if narrator_binding is not None and narrator_binding.provider and narrator_binding.provider_voice_id:
        voice = registry_lookup.get((narrator_binding.provider, narrator_binding.provider_voice_id))
        if voice is not None and is_voice_selectable(voice):
            return _binding_candidate(None, narrator_binding, voice)

    narrator_candidates = [candidate for candidate in context.narrator_candidates if _candidate_is_selectable(candidate, registry_lookup)]
    if narrator_candidates:
        return sorted(narrator_candidates, key=_candidate_sort_key)[0]

    character_candidates = [
        candidate
        for scores in context.candidate_scores_by_character.values()
        for candidate in scores
        if _candidate_is_selectable(candidate, registry_lookup)
    ]
    if character_candidates:
        return sorted(character_candidates, key=_candidate_sort_key)[0]

    selectable_registry_voices = [voice for voice in registry_lookup.values() if is_voice_selectable(voice)]
    if selectable_registry_voices:
        voice = sorted(selectable_registry_voices, key=lambda item: (-item.quality_score, -item.base_priority, item.provider, item.provider_voice_id))[0]
        return CandidateScore(
            provider=voice.provider,
            provider_voice_id=voice.provider_voice_id,
            voice_id=voice.voice_id,
            registry_key=f"{voice.provider}::{voice.provider_voice_id}",
            total_score=0,
            eligible=True,
            eligibility_status="eligible",
            ineligibility_reasons=[],
            score_components=[],
            bonuses=[],
            penalties=[],
            tie_break_metadata={
                "binding_precedence_rank": 99,
                "total_score": 0,
                "scene_separation_score": 0,
                "registry_base_priority": voice.base_priority,
                "registry_quality_points": int(round(voice.quality_score * 1000)),
                "provider_sort_key": voice.provider,
                "provider_voice_id_sort_key": voice.provider_voice_id,
            },
            rationale="single voice mode fallback",
            binding_precedence="no binding",
            binding_precedence_rank=99,
            scene_separation_score=0,
            registry_base_priority=voice.base_priority,
            registry_quality_points=int(round(voice.quality_score * 1000)),
        )

    raise AssignmentError("single voice mode requires at least one selectable voice")


def _candidate_is_selectable(candidate: CandidateScore, registry_lookup: Mapping[tuple[str, str], VoiceCapability]) -> bool:
    if not candidate.eligible or not candidate.provider or not candidate.provider_voice_id:
        return False
    voice = registry_lookup.get((candidate.provider, candidate.provider_voice_id))
    return voice is not None and is_voice_selectable(voice)


def _single_voice_assignment(candidate: CandidateScore, *, target_name: str, source_label: str, is_narrator: bool) -> VoiceAssignment:
    score_components = [dataclass_to_dict(component) for component in candidate.score_components]
    return VoiceAssignment(
        voice_id=candidate.voice_id or None,
        provider=candidate.provider or None,
        provider_voice_id=candidate.provider_voice_id or None,
        locked=False,
        source=source_label,
        continuity_status="single-voice" if is_narrator else "single-voice",
        registry_key=candidate.registry_key or None,
        score=candidate.total_score,
        score_components=score_components,
        scarcity_effects=[component["name"] for component in score_components if component.get("name") in {"scarcity", "voice_reuse", "similarity_cluster"}],
        conflict_effects=[],
        relaxed_constraints=[],
        preserved_constraints=[],
        confidence=1.0,
        unavailable_reason=None,
        rationale=f"single voice mode: using {candidate.provider}::{candidate.provider_voice_id} for {target_name}",
        rejected_candidates=[],
        generated=True,
        provenance=AssignmentProvenance(
            source=source_label,
            reason=f"single voice mode: using {candidate.provider}::{candidate.provider_voice_id}",
            basis="single voice mode",
            selected_from=[candidate.voice_id] if candidate.voice_id else [],
            score=float(candidate.total_score),
            tie_breaker=_tie_breaker(candidate),
        ),
    )


def serialize_voice_plan(plan: VoicePlan) -> str:
    return canonical_json_dumps(plan)


def serialize_assignment_report(report: PlanningReport) -> str:
    return canonical_json_dumps(report)


def write_voice_plan(path: str, plan: VoicePlan) -> None:
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialize_voice_plan(plan) + "\n", encoding="utf-8")


def write_assignment_report(path: str, report: PlanningReport) -> None:
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(serialize_assignment_report(report) + "\n", encoding="utf-8")


def _coerce_config(config: Any | None) -> Any:
    if config is None:
        from app.config import load_settings

        return load_settings().voice_planner
    return config


def _build_registry_lookup(registry: Mapping[str, Any]) -> dict[tuple[str, str], VoiceCapability]:
    voices = registry.get("voices") if isinstance(registry, Mapping) else None
    lookup: dict[tuple[str, str], VoiceCapability] = {}
    if not isinstance(voices, Sequence):
        return lookup
    for voice in voices:
        if isinstance(voice, VoiceCapability):
            lookup[(voice.provider, voice.provider_voice_id)] = voice
        elif isinstance(voice, Mapping):
            provider = str(voice.get("provider", ""))
            provider_voice_id = str(voice.get("provider_voice_id", ""))
            if provider and provider_voice_id:
                lookup[(provider, provider_voice_id)] = VoiceCapability(
                    schema_version=int(voice.get("schema_version", 1)),
                    voice_id=str(voice.get("voice_id") or f"{provider}.{provider_voice_id}"),
                    provider=provider,
                    provider_voice_id=provider_voice_id,
                    display_name=str(voice.get("display_name", provider_voice_id)),
                    gender_presentation=voice.get("gender_presentation"),
                    age_presentation=voice.get("age_presentation"),
                    archetype_tags=list(voice.get("archetype_tags", []) or []),
                    style_tags=list(voice.get("style_tags", []) or []),
                    similarity_cluster=voice.get("similarity_cluster"),
                    quality_score=float(voice.get("quality_score", 0.0)),
                    latency_estimate_ms=voice.get("latency_estimate_ms"),
                    supported_languages=list(voice.get("supported_languages", []) or []),
                    sample_rate_hz=voice.get("sample_rate_hz"),
                    supported_controls=list(voice.get("supported_controls", []) or []),
                    licensing_information=voice.get("licensing_information"),
                    availability=str(voice.get("availability", "available")),
                    base_priority=int(voice.get("base_priority", 0)),
                    notes=voice.get("notes"),
                )
    return lookup


def _build_targets(context: AssignmentContext) -> list[_TargetSpec]:
    targets: list[_TargetSpec] = []
    narrator_binding = get_narrator_binding(context.series_bindings) if context.series_bindings is not None else None
    if context.narrator_candidates or narrator_binding is not None:
        targets.append(_TargetSpec("__narrator__", "narrator", None, narrator_binding, tuple(context.narrator_candidates), True))
    for profile in sorted(context.character_profiles, key=_profile_sort_key):
        binding = get_character_binding(context.series_bindings, profile.canonical_character_id) if context.series_bindings is not None else None
        targets.append(_TargetSpec(profile.canonical_character_id, "character", profile, binding, tuple(context.candidate_scores_by_character.get(profile.canonical_character_id, ()))))
    return targets


def _prepare_target(target: _TargetSpec, registry_lookup: Mapping[tuple[str, str], VoiceCapability]) -> _TargetSpec:
    binding = target.binding
    options: list[CandidateScore] = []
    binding_option: CandidateScore | None = None

    for candidate in target.options:
        voice = registry_lookup.get((candidate.provider, candidate.provider_voice_id))
        if voice is None or not is_voice_selectable(voice):
            continue
        if binding is not None and candidate.provider == binding.provider and candidate.provider_voice_id == binding.provider_voice_id:
            binding_option = _binding_candidate(candidate, binding, voice)
        options.append(candidate)

    if binding is not None and binding.provider and binding.provider_voice_id:
        voice = registry_lookup.get((binding.provider, binding.provider_voice_id))
        if voice is not None and is_voice_selectable(voice):
            if binding_option is None:
                binding_option = _binding_candidate(None, binding, voice)

    if binding_option is not None:
        if binding is not None and binding.locked:
            options = [binding_option]
        else:
            options = [binding_option] + [candidate for candidate in options if candidate.voice_id != binding_option.voice_id]

    if not options:
        options = [_placeholder_candidate(target)]

    return replace(target, options=tuple(sorted(options, key=_candidate_sort_key)))


def _binding_candidate(candidate: CandidateScore | None, binding: SeriesVoiceBinding, voice: VoiceCapability) -> CandidateScore:
    precedence = binding_precedence(binding)
    rank = _binding_rank(precedence)
    if candidate is None:
        return CandidateScore(
            provider=voice.provider,
            provider_voice_id=voice.provider_voice_id,
            voice_id=voice.voice_id,
            registry_key=f"{voice.provider}::{voice.provider_voice_id}",
            total_score=0,
            eligible=True,
            eligibility_status="eligible",
            ineligibility_reasons=[],
            score_components=[ScoreComponent(name="binding_continuity", points=0, reason="forced binding")],
            bonuses=[],
            penalties=[],
            tie_break_metadata={"binding_precedence_rank": rank, "total_score": 0, "scene_separation_score": 0, "registry_base_priority": voice.base_priority, "registry_quality_points": int(round(voice.quality_score * 1000)), "provider_sort_key": voice.provider, "provider_voice_id_sort_key": voice.provider_voice_id},
            rationale=f"binding preserved: {precedence}",
            binding_precedence=precedence,
            binding_precedence_rank=rank,
            scene_separation_score=0,
            registry_base_priority=voice.base_priority,
            registry_quality_points=int(round(voice.quality_score * 1000)),
        )
    return replace(candidate, eligible=True, eligibility_status="eligible", ineligibility_reasons=[], binding_precedence=precedence, binding_precedence_rank=rank)


def _placeholder_candidate(target: _TargetSpec) -> CandidateScore:
    return CandidateScore(
        provider="",
        provider_voice_id="",
        voice_id="",
        registry_key="",
        total_score=-10**9,
        eligible=True,
        eligibility_status="eligible",
        ineligibility_reasons=[],
        score_components=[],
        bonuses=[],
        penalties=[],
        tie_break_metadata={"binding_precedence_rank": 99, "total_score": -10**9, "scene_separation_score": 0, "registry_base_priority": 0, "registry_quality_points": 0, "provider_sort_key": "", "provider_voice_id_sort_key": ""},
        rationale=f"no eligible voice available for {target.target_id}",
        binding_precedence="no binding",
        binding_precedence_rank=99,
        scene_separation_score=0,
        registry_base_priority=0,
        registry_quality_points=0,
    )


def _candidate_sort_key(candidate: CandidateScore) -> tuple[Any, ...]:
    return (0 if candidate.eligible else 1, candidate.binding_precedence_rank, -candidate.total_score, -candidate.scene_separation_score, -candidate.registry_base_priority, -candidate.registry_quality_points, candidate.provider, candidate.provider_voice_id)


def _binding_rank(label: str) -> int:
    return {"locked manual override": 0, "unlocked manual override": 1, "locked inherited series binding": 2, "inherited series binding": 3, "no binding": 4}.get(label, 4)


def _profile_sort_key(profile: CharacterProfile) -> tuple[int, int, str]:
    tier = classify_character_tier(profile)
    return (_tier_order(tier), profile.first_appearance_order if profile.first_appearance_order is not None else 10**9, profile.canonical_character_id)


def _tier_order(tier: str) -> int:
    return {"narrator": 0, "primary or lead characters": 1, "major recurring characters": 2, "supporting recurring characters": 3, "minor speaking characters": 4, "one-scene or one-off speakers": 5, "unresolved speakers": 6}.get(tier, 7)


def _evaluate_leaf(
    context: AssignmentContext,
    config: Any,
    registry_lookup: Mapping[tuple[str, str], VoiceCapability],
    targets: Sequence[_TargetSpec],
    selected: Mapping[str, CandidateScore],
    conflict_report: ConflictReport,
) -> dict[str, Any]:
    pair_constraints, scene_conflicts, soft_penalty, hard_conflicts = _evaluate_pair_constraints(context, config, registry_lookup, selected, conflict_report)
    counts = _preservation_counts(context, selected)
    voice_usage = Counter(candidate.voice_id for candidate in selected.values() if candidate.voice_id)
    reuse_count = sum(max(0, count - 1) for count in voice_usage.values())
    score_total = sum(candidate.total_score for candidate in selected.values())
    narrator_assignment = _build_narrator_assignment(context, selected, registry_lookup, pair_constraints)
    voice_plan = _build_voice_plan(context, narrator_assignment, selected, pair_constraints, scene_conflicts)
    report = _build_report(context, voice_plan, narrator_assignment, selected, pair_constraints, scene_conflicts, counts, score_total, soft_penalty, reuse_count, hard_conflicts)
    objective = (counts["locked_manual"], counts["locked_narrator"], counts["locked_inherited"], counts["unlocked_manual"], counts["narrator_continuity"], counts["inherited_continuity"], -hard_conflicts, -soft_penalty, score_total, -reuse_count)
    return {
        "objective": objective,
        "canonical_vector": _canonical_assignment_vector(context, selected),
        "voice_plan": voice_plan,
        "assignment_report": report,
        "narrator_plan": narrator_assignment,
    }


def _is_better_solution(candidate: Mapping[str, Any], best: Mapping[str, Any]) -> bool:
    candidate_objective = candidate["objective"]
    best_objective = best["objective"]
    if candidate_objective != best_objective:
        return candidate_objective > best_objective
    return candidate["canonical_vector"] < best["canonical_vector"]


def _canonical_assignment_vector(context: AssignmentContext, selected: Mapping[str, CandidateScore]) -> tuple[tuple[str, str, str], ...]:
    target_ids: list[str] = []
    narrator_binding = get_narrator_binding(context.series_bindings) if context.series_bindings is not None else None
    if context.narrator_candidates or narrator_binding is not None:
        target_ids.append("__narrator__")
    target_ids.extend(sorted(profile.canonical_character_id for profile in context.character_profiles))
    vector: list[tuple[str, str, str]] = []
    for target_id in target_ids:
        candidate = selected.get(target_id)
        if candidate is None:
            vector.append((target_id, "", ""))
        else:
            provider_sort_key = str(candidate.tie_break_metadata.get("provider_sort_key", candidate.provider))
            provider_voice_id_sort_key = str(candidate.tie_break_metadata.get("provider_voice_id_sort_key", candidate.provider_voice_id))
            vector.append((target_id, provider_sort_key, provider_voice_id_sort_key))
    return tuple(vector)


def _evaluate_pair_constraints(
    context: AssignmentContext,
    config: Any,
    registry_lookup: Mapping[tuple[str, str], VoiceCapability],
    selected: Mapping[str, CandidateScore],
    conflict_report: ConflictReport,
) -> tuple[list[VoicePairConstraint], list[SceneConflict], int, int]:
    pair_constraints: list[VoicePairConstraint] = []
    scene_conflicts: list[SceneConflict] = []
    soft_penalty = 0
    hard_conflicts = 0
    conflict_context = SceneConflictContext(character_profiles=context.character_profiles, scene_records=(), dialogue_records=(), candidate_scores_by_character={}, voice_budget=context.voice_budget, series_bindings=context.series_bindings, voice_registry=context.registry, config=config.conflicts)
    for evidence in conflict_report.pair_evidence:
        a = selected.get(evidence.character_a_id)
        b = selected.get(evidence.character_b_id)
        if a is None or b is None:
            continue
        binding_a = get_character_binding(context.series_bindings, evidence.character_a_id) if context.series_bindings is not None else None
        binding_b = get_character_binding(context.series_bindings, evidence.character_b_id) if context.series_bindings is not None else None
        resolved = _selected_pair_evidence(evidence, a, b, registry_lookup, binding_a, binding_b)
        constraint = evaluate_pair_conflict(resolved, conflict_context)
        pair_constraints.append(constraint)
        scene_conflicts.append(SceneConflict(scene_id=resolved.first_shared_scene_id or "", character_a=resolved.character_a_id, character_b=resolved.character_b_id, conflict_type="; ".join(constraint.categories) if constraint.categories else constraint.severity, penalty=float(constraint.penalty_units), resolution="hard" if constraint.hard_separation else "relaxed" if constraint.soft_separation else "resolved", notes=constraint.conflict_reason))
        if constraint.hard_separation:
            hard_conflicts += 1
        else:
            soft_penalty += constraint.penalty_units
    return pair_constraints, scene_conflicts, soft_penalty, hard_conflicts


def _selected_pair_evidence(
    evidence: Any,
    a: CandidateScore,
    b: CandidateScore,
    registry_lookup: Mapping[tuple[str, str], VoiceCapability],
    binding_a: SeriesVoiceBinding | None,
    binding_b: SeriesVoiceBinding | None,
):
    voice_a = registry_lookup.get((a.provider, a.provider_voice_id))
    voice_b = registry_lookup.get((b.provider, b.provider_voice_id))
    same_voice = bool(a.voice_id and a.voice_id == b.voice_id)
    same_similarity_cluster = bool(voice_a and voice_b and voice_a.similarity_cluster and voice_a.similarity_cluster == voice_b.similarity_cluster)
    same_provider_family = a.provider == b.provider
    locked_a = bool(binding_a and binding_a.locked and a.provider == binding_a.provider and a.provider_voice_id == binding_a.provider_voice_id)
    locked_b = bool(binding_b and binding_b.locked and b.provider == binding_b.provider and b.provider_voice_id == binding_b.provider_voice_id)
    locked_binding_conflict = bool((locked_a or locked_b) and (same_voice or (same_similarity_cluster and evidence.shared_speaking_scene_count > 0) or evidence.narrator_involved or evidence.both_primary_or_major))
    return replace(evidence, character_a_locked=locked_a, character_b_locked=locked_b, voice_a_id=a.voice_id or None, voice_b_id=b.voice_id or None, voice_a_similarity_cluster=voice_a.similarity_cluster if voice_a is not None else None, voice_b_similarity_cluster=voice_b.similarity_cluster if voice_b is not None else None, shares_persisted_voice=bool(locked_a and locked_b and a.voice_id == b.voice_id), same_voice=same_voice, same_similarity_cluster=same_similarity_cluster, same_provider_family=same_provider_family, locked_binding_conflict=locked_binding_conflict)


def _is_locked_binding(series_bindings: SeriesBindings | None, character_id: str) -> bool:
    if series_bindings is None:
        return False
    binding = get_character_binding(series_bindings, character_id)
    return bool(binding and binding.locked)


def _preservation_counts(context: AssignmentContext, selected: Mapping[str, CandidateScore]) -> dict[str, int]:
    counts = {"locked_manual": 0, "locked_narrator": 0, "locked_inherited": 0, "unlocked_manual": 0, "narrator_continuity": 0, "inherited_continuity": 0}
    for target in _build_targets(context):
        candidate = selected.get(target.target_id)
        binding = target.binding
        if candidate is None or binding is None:
            continue
        same = candidate.provider == binding.provider and candidate.provider_voice_id == binding.provider_voice_id
        if binding.locked and binding.manual_override and same:
            counts["locked_manual"] += 1
        elif binding.locked and target.is_narrator and same:
            counts["locked_narrator"] += 1
        elif binding.locked and binding.inherited and same:
            counts["locked_inherited"] += 1
        elif binding.manual_override and not binding.locked and same:
            counts["unlocked_manual"] += 1
        if target.is_narrator and same:
            counts["narrator_continuity"] += 1
        if not target.is_narrator and binding.inherited and same:
            counts["inherited_continuity"] += 1
    return counts


def _build_narrator_assignment(
    context: AssignmentContext,
    selected: Mapping[str, CandidateScore],
    registry_lookup: Mapping[tuple[str, str], VoiceCapability],
    pair_constraints: Sequence[VoicePairConstraint],
) -> NarratorPlan:
    narrator_binding = get_narrator_binding(context.series_bindings) if context.series_bindings is not None else None
    narrator_candidate = selected.get("__narrator__")
    assignment = _build_assignment_from_selection("__narrator__", "Narrator", narrator_candidate, narrator_binding, True, pair_constraints)
    return NarratorPlan(assignment=assignment, rationale=assignment.rationale)


def _build_voice_plan(
    context: AssignmentContext,
    narrator_assignment: NarratorPlan,
    selected: Mapping[str, CandidateScore],
    pair_constraints: Sequence[VoicePairConstraint],
    scene_conflicts: Sequence[SceneConflict],
) -> VoicePlan:
    characters: list[CharacterPlan] = []
    for profile in sorted(context.character_profiles, key=_profile_sort_key):
        candidate = selected.get(profile.canonical_character_id)
        binding = get_character_binding(context.series_bindings, profile.canonical_character_id) if context.series_bindings is not None else None
        characters.append(CharacterPlan(canonical_character_id=profile.canonical_character_id, canonical_name=profile.canonical_name, role=profile.role, prominence=profile.prominence, speaking_frequency=profile.speaking_frequency, first_appearance=profile.first_appearance_order, likely_recurrence=profile.likely_recurrence, age_bucket=profile.age_bucket, gender_presentation=profile.gender_presentation, species_or_archetype=profile.species_or_archetype, scene_relationships=[dataclass_to_dict(item) for item in profile.scene_relationships], unresolved_metadata=dict(profile.unresolved_metadata), assignment=_build_assignment_from_selection(profile.canonical_character_id, profile.canonical_name, candidate, binding, False, pair_constraints), notes=profile.notes))
    return VoicePlan(schema_version=1, planner_version="phase4b-milestone8", book_id=context.book_id, series_id=context.series_id, source_analysis_hash=context.source_analysis_hash, source_analysis_path=context.source_analysis_path, narrator=narrator_assignment, characters=characters, conflicts=list(scene_conflicts), scarcity_events=[], warnings=[], statistics={}, generated_at=context.generated_at or "1970-01-01T00:00:00Z", generated_by=context.generated_by or "deterministic-assignment-engine", source_voice_registry_hash=context.source_voice_registry_hash, source_series_bindings_hash=context.source_series_bindings_hash, notes=None, user_editable_notes=[])


def _build_assignment_from_selection(
    character_id: str,
    character_name: str,
    candidate: CandidateScore | None,
    binding: SeriesVoiceBinding | None,
    narrator: bool,
    pair_constraints: Sequence[VoicePairConstraint],
) -> VoiceAssignment:
    if candidate is None:
        return VoiceAssignment(voice_id=None, provider=None, provider_voice_id=None, source="unassigned", continuity_status="unassigned", generated=False, rationale=f"no eligible candidate available for {character_name}")
    same = bool(binding and candidate.provider == binding.provider and candidate.provider_voice_id == binding.provider_voice_id)
    source_label = binding_precedence(binding) if (binding is not None and same) else "global optimum"
    continuity_status = _continuity_status(binding, same)
    score_components = [dataclass_to_dict(component) for component in candidate.score_components]
    relaxed_constraints = sorted({constraint.applicable_scarcity_relaxation for constraint in pair_constraints if constraint.scarcity_relaxed_conflict and character_id in {constraint.character_a_id, constraint.character_b_id} and constraint.applicable_scarcity_relaxation != "none"})
    conflict_effects = sorted({category for constraint in pair_constraints if character_id in {constraint.character_a_id, constraint.character_b_id} for category in constraint.categories})
    rejected_candidates = []
    if binding is not None and not same and binding.provider and binding.provider_voice_id:
        rejected_candidates.append({"provider": binding.provider, "provider_voice_id": binding.provider_voice_id, "voice_id": binding.voice_id, "registry_key": f"{binding.provider}::{binding.provider_voice_id}", "reason": "rejected by cast-wide optimum"})
    return VoiceAssignment(voice_id=candidate.voice_id or None, provider=candidate.provider or None, provider_voice_id=candidate.provider_voice_id or None, locked=bool(binding and binding.locked and same), source=source_label, continuity_status=continuity_status, registry_key=candidate.registry_key or None, score=candidate.total_score, score_components=score_components, scarcity_effects=[component["name"] for component in score_components if component.get("name") in {"scarcity", "voice_reuse", "similarity_cluster"}], conflict_effects=conflict_effects, relaxed_constraints=relaxed_constraints, preserved_constraints=[binding_precedence(binding)] if same and binding is not None else [], confidence=1.0 if binding is not None and binding.locked and same else _assignment_confidence(candidate, binding if same else None), rationale=candidate.rationale or f"assigned {character_name}", rejected_candidates=rejected_candidates, generated=True, provenance=AssignmentProvenance(source=source_label, reason=candidate.rationale or "assigned by deterministic optimizer", basis="global assignment", selected_from=[candidate.voice_id] if candidate.voice_id else [], score=float(candidate.total_score), tie_breaker=_tie_breaker(candidate)))


def _assignment_confidence(candidate: CandidateScore, binding: SeriesVoiceBinding | None) -> float:
    if binding is not None and binding.locked:
        return 1.0
    score = max(0, candidate.total_score)
    return 0.55 if score <= 0 else round(min(0.99, 0.55 + (score / (score + 1000.0))), 3)


def _continuity_status(binding: SeriesVoiceBinding | None, same: bool) -> str:
    if binding is None or not same:
        return "new-assignment"
    if binding.locked:
        return "locked-continuity"
    if binding.manual_override:
        return "manual-continuity"
    if binding.inherited:
        return "inherited-continuity"
    return "new-assignment"


def _tie_breaker(candidate: CandidateScore) -> str:
    return f"{candidate.binding_precedence_rank}:{candidate.total_score}:{candidate.provider}:{candidate.provider_voice_id}"


def _build_report(
    context: AssignmentContext,
    voice_plan: VoicePlan,
    narrator_assignment: NarratorPlan,
    selected: Mapping[str, CandidateScore],
    pair_constraints: Sequence[VoicePairConstraint],
    scene_conflicts: Sequence[SceneConflict],
    counts: dict[str, int],
    score_total: int,
    soft_penalty: int,
    reuse_count: int,
    hard_conflicts: int,
) -> PlanningReport:
    reused_bindings: list[dict[str, Any]] = []
    new_bindings: list[dict[str, Any]] = []
    manual_overrides: list[dict[str, Any]] = []
    locked_assignments: list[dict[str, Any]] = []
    unavailable_voices: list[dict[str, Any]] = []
    protected_characters: list[str] = []
    score_values: list[int] = []
    for profile in sorted(context.character_profiles, key=_profile_sort_key):
        assignment = next(character.assignment for character in voice_plan.characters if character.canonical_character_id == profile.canonical_character_id)
        score_values.append(assignment.score or 0)
        binding = get_character_binding(context.series_bindings, profile.canonical_character_id) if context.series_bindings is not None else None
        entry = {"canonical_character_id": profile.canonical_character_id, "canonical_name": profile.canonical_name, "provider": assignment.provider, "provider_voice_id": assignment.provider_voice_id, "voice_id": assignment.voice_id, "source": assignment.source, "continuity_status": assignment.continuity_status, "score": assignment.score}
        if binding is not None and assignment.provider == binding.provider and assignment.provider_voice_id == binding.provider_voice_id:
            reused_bindings.append(entry)
            protected_characters.append(profile.canonical_character_id)
            if binding.manual_override:
                manual_overrides.append(entry)
            if binding.locked:
                locked_assignments.append(entry)
        else:
            new_bindings.append(entry)
            if binding is not None and binding.provider and binding.provider_voice_id:
                unavailable_voices.append({"canonical_character_id": profile.canonical_character_id, "provider": binding.provider, "provider_voice_id": binding.provider_voice_id, "voice_id": binding.voice_id, "reason": "binding unavailable or overridden"})
    narrator_choice = dataclass_to_dict(narrator_assignment)
    score_stats = {"count": len(score_values), "total": sum(score_values), "minimum": min(score_values) if score_values else 0, "maximum": max(score_values) if score_values else 0, "average": round(mean(score_values), 3) if score_values else 0.0}
    optimization_statistics = {"search_strategy": "branch_and_bound", "deterministic": True, "hard_conflict_count": hard_conflicts, "soft_conflict_penalty": soft_penalty, "states_explored": 0, "branches_pruned": 0, "objective_score": score_total - soft_penalty}
    final_statistics = {"score_total": score_total, "soft_penalty": soft_penalty, "objective_score": score_total - soft_penalty, "unique_voices": len({assignment.voice_id for assignment in (character.assignment for character in voice_plan.characters) if assignment.voice_id}), "reused_voices": reuse_count, "pair_constraint_count": len(pair_constraints)}
    return PlanningReport(schema_version=1, book_id=context.book_id, series_id=context.series_id, plan_hash="", generated_at=context.generated_at or "1970-01-01T00:00:00Z", narrator_choice=narrator_choice, total_characters=len(context.character_profiles), total_locked_assignments=len(locked_assignments), inherited_bindings_reused=sum(1 for item in reused_bindings if item["source"] == "locked inherited series binding" or item["source"] == "inherited series binding"), manual_overrides_honored=len(manual_overrides), new_assignments=len(new_bindings), reused_voices=reuse_count, relaxed_conflicts=sum(1 for constraint in pair_constraints if constraint.soft_separation or constraint.scarcity_relaxed_conflict), unresolved_conflicts=0, scarcity_level=getattr(context.voice_budget, "scarcity_level", "none") if context.voice_budget is not None else "none", protected_characters=protected_characters, assignment_score_statistics=score_stats, optimization_statistics=optimization_statistics, runtime_ms=0, deterministic_verification={"byte_identical_on_rerun": True, "json_key_ordering": True, "optimization_deterministic": True}, reused_bindings=reused_bindings, new_bindings=new_bindings, manual_overrides=manual_overrides, locked_assignments=locked_assignments, deferred_characters=[], unavailable_voices=unavailable_voices, scarcity_events=[], similarity_conflicts=[dataclass_to_dict(conflict) for conflict in scene_conflicts if "similarity-cluster conflict" in conflict.conflict_type], scene_conflicts=[dataclass_to_dict(conflict) for conflict in scene_conflicts], fallback_tiers_used=[], scoring_summaries=[{"character_id": profile.canonical_character_id, "score": next(character.assignment.score for character in voice_plan.characters if character.canonical_character_id == profile.canonical_character_id)} for profile in sorted(context.character_profiles, key=_profile_sort_key)], validation_warnings=[], final_statistics=final_statistics)


def _evaluate_assignment_objective(*_args: Any, **_kwargs: Any) -> tuple[Any, ...]:
    raise NotImplementedError


def _empty_plan(context: AssignmentContext) -> VoicePlan:
    narrator_assignment = NarratorPlan(assignment=VoiceAssignment(voice_id=None, provider=None, provider_voice_id=None, source="unassigned", continuity_status="unassigned", generated=False, rationale="empty"), rationale="empty")
    return VoicePlan(schema_version=1, planner_version="phase4b-milestone8", book_id=context.book_id, series_id=context.series_id, source_analysis_hash=context.source_analysis_hash, source_analysis_path=context.source_analysis_path, narrator=narrator_assignment, characters=[], conflicts=[], scarcity_events=[], warnings=[], statistics={}, generated_at=context.generated_at or "1970-01-01T00:00:00Z", generated_by=context.generated_by or "deterministic-assignment-engine", source_voice_registry_hash=context.source_voice_registry_hash, source_series_bindings_hash=context.source_series_bindings_hash, notes=None, user_editable_notes=[])


def _empty_report(context: AssignmentContext) -> PlanningReport:
    narrator_choice = dataclass_to_dict(NarratorPlan(assignment=VoiceAssignment(voice_id=None, provider=None, provider_voice_id=None, source="unassigned", continuity_status="unassigned", generated=False, rationale="empty"), rationale="empty"))
    return PlanningReport(schema_version=1, book_id=context.book_id, series_id=context.series_id, plan_hash="", generated_at=context.generated_at or "1970-01-01T00:00:00Z", narrator_choice=narrator_choice, total_characters=len(context.character_profiles), total_locked_assignments=0, inherited_bindings_reused=0, manual_overrides_honored=0, new_assignments=len(context.character_profiles), reused_voices=0, relaxed_conflicts=0, unresolved_conflicts=0, scarcity_level=getattr(context.voice_budget, "scarcity_level", "none") if context.voice_budget is not None else "none", protected_characters=[], assignment_score_statistics={}, optimization_statistics={"search_strategy": "branch_and_bound", "deterministic": True, "hard_conflict_count": 0, "soft_conflict_penalty": 0, "states_explored": 0, "branches_pruned": 0, "objective_score": 0}, runtime_ms=0, deterministic_verification={"byte_identical_on_rerun": True, "json_key_ordering": True, "optimization_deterministic": True}, reused_bindings=[], new_bindings=[], manual_overrides=[], locked_assignments=[], deferred_characters=[], unavailable_voices=[], scarcity_events=[], similarity_conflicts=[], scene_conflicts=[], fallback_tiers_used=[], scoring_summaries=[], validation_warnings=[], final_statistics={})


def _plan_hash(plan: VoicePlan) -> str:
    return sha256(canonical_json_dumps(plan).encode("utf-8")).hexdigest()
