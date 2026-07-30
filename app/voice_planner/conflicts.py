from __future__ import annotations

from dataclasses import dataclass, field, MISSING
from itertools import combinations
from typing import Any, Mapping, Sequence

from .bindings import binding_precedence, binding_registry_status, get_character_binding, get_narrator_binding
from .budget import BudgetConfig, VoiceBudget, classify_character_tier
from .models import CharacterProfile, SeriesBindings, SeriesVoiceBinding, dataclass_to_dict
from .registry import voice_registry_key
from .schema import canonical_json_dumps
from .scoring import CandidateScore

SCENE_CONFLICT_SCHEMA_VERSION = 1

_TIER_PRIORITY = {
    "narrator": 0,
    "primary or lead characters": 1,
    "major recurring characters": 2,
    "supporting recurring characters": 3,
    "minor speaking characters": 4,
    "one-scene or one-off speakers": 5,
    "unresolved speakers": 6,
}

_ORDERED_CATEGORIES = (
    "same-scene speaking conflict",
    "frequent shared-scene conflict",
    "alternating-dialogue conflict",
    "dialogue-proximity conflict",
    "narrator-character conflict",
    "major-character distinction conflict",
    "locked-binding conflict",
    "same-voice conflict",
    "similarity-cluster conflict",
    "role-similarity conflict",
    "intentional reuse",
    "scarcity-relaxed conflict",
    "unresolved hard conflict",
)

_ALLOWED_NARRATOR_POLICIES = {"allow", "discourage", "prohibit"}
_ALLOWED_LOCKED_POLICIES = {"invalid", "warn", "allow_intentional_reuse"}
_ALLOWED_SIMILARITY_POLICIES = {"ignore", "soft_for_moderate", "hard_for_high_conflict"}
_ALLOWED_RELAXATION_POLICIES = {"preserve_primary_major", "lower_tier_first", "strict"}
_ALLOWED_UNRESOLVED_BEHAVIOR = {"report", "fail_loudly"}


@dataclass(frozen=True)
class ConflictConfig:
    schema_version: int = SCENE_CONFLICT_SCHEMA_VERSION
    adjacency_window: int = 1
    alternating_dialogue_threshold: int = 3
    shared_scene_thresholds: dict[str, int] = field(
        default_factory=lambda: {"low": 1, "moderate": 2, "high": 3, "critical": 4}
    )
    shared_speaking_scene_thresholds: dict[str, int] = field(
        default_factory=lambda: {"low": 1, "moderate": 2, "high": 3, "critical": 4}
    )
    relationship_density_thresholds: dict[str, float] = field(
        default_factory=lambda: {"low": 0.25, "moderate": 0.5, "high": 0.75, "critical": 1.0}
    )
    severity_thresholds: dict[str, int] = field(
        default_factory=lambda: {"low": 3, "moderate": 7, "high": 12, "critical": 18}
    )
    hard_separation_tier_pairs: tuple[tuple[str, str], ...] = field(
        default_factory=lambda: (
            ("primary or lead characters", "major recurring characters"),
            ("primary or lead characters", "narrator"),
            ("major recurring characters", "narrator"),
        )
    )
    similarity_cluster_policy: str = "hard_for_high_conflict"
    narrator_separation_policy: str = "prohibit"
    locked_conflict_policy: str = "invalid"
    scarcity_relaxation_policy: str = "preserve_primary_major"
    pairwise_penalty_weights: dict[str, int] = field(
        default_factory=lambda: {
            "shared_scene": 2,
            "shared_speaking_scene": 4,
            "adjacent_dialogue": 3,
            "near_dialogue": 2,
            "alternating_dialogue": 5,
            "relationship_density": 4,
            "same_voice": 12,
            "same_similarity_cluster": 6,
            "role_similarity": 4,
            "narrator": 8,
            "locked": 10,
        }
    )
    same_voice_prohibition_thresholds: dict[str, int] = field(
        default_factory=lambda: {
            "shared_speaking_scene_count": 2,
            "shared_scene_count": 3,
            "alternating_dialogue_transitions": 3,
        }
    )
    unresolved_conflict_behavior: str = "report"

    @classmethod
    def default(cls) -> "ConflictConfig":
        return cls()

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "ConflictConfig":
        if mapping is None:
            return cls.default()
        errors = validate_conflict_config(mapping)
        if errors:
            raise ConflictError("; ".join(errors))
        payload: dict[str, Any] = {}
        for field_def in cls.__dataclass_fields__.values():
            if field_def.default is not MISSING:
                default_value = field_def.default
            else:
                default_value = field_def.default_factory()  # type: ignore[misc]
            value = mapping.get(field_def.name, default_value)
            if field_def.name == "hard_separation_tier_pairs":
                payload[field_def.name] = _coerce_tier_pairs(value)
            else:
                payload[field_def.name] = value
        return cls(**payload)


@dataclass(frozen=True)
class SceneConflictContext:
    character_profiles: tuple[CharacterProfile, ...]
    scene_records: tuple[Mapping[str, Any], ...]
    dialogue_records: tuple[Mapping[str, Any], ...]
    candidate_scores_by_character: dict[str, tuple[CandidateScore, ...]] = field(default_factory=dict)
    voice_budget: VoiceBudget | None = None
    series_bindings: SeriesBindings | None = None
    voice_registry: Mapping[str, Any] | None = None
    config: ConflictConfig = field(default_factory=ConflictConfig.default)


@dataclass(frozen=True)
class DialogueProximityEvidence:
    adjacency_window: int
    adjacent_dialogue_pairs: int = 0
    near_adjacency_pairs: int = 0
    alternating_dialogue_transitions: int = 0
    total_dialogue_records_in_shared_scenes: int = 0
    max_dialogue_gap: int | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CharacterPairEvidence:
    character_a_id: str
    character_b_id: str
    character_a_name: str
    character_b_name: str
    character_a_tier: str
    character_b_tier: str
    character_a_prominence: str | None
    character_b_prominence: str | None
    character_a_recurrence: bool | None
    character_b_recurrence: bool | None
    character_a_locked: bool
    character_b_locked: bool
    locked_binding_conflict: bool
    shared_scene_count: int
    shared_speaking_scene_count: int
    first_shared_scene_id: str | None
    first_shared_scene_order: int | None
    total_dialogue_records_in_shared_scenes: int
    relationship_density: float
    dialogue_proximity: DialogueProximityEvidence
    voice_a_id: str | None
    voice_b_id: str | None
    voice_a_similarity_cluster: str | None
    voice_b_similarity_cluster: str | None
    shares_persisted_voice: bool
    same_voice: bool
    same_similarity_cluster: bool
    same_provider_family: bool
    narrator_involved: bool
    both_primary_or_major: bool
    roles_similar: bool
    pair_ordering_key: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class VoicePairConstraint:
    character_a_id: str
    character_b_id: str
    categories: tuple[str, ...]
    severity: str
    severity_score: int
    distinct_voice_requirement: bool
    same_voice_prohibition: bool
    similarity_cluster_prohibition: bool
    similarity_cluster_penalty: int
    reuse_eligibility: bool
    conflict_reason: str
    applicable_scarcity_relaxation: str
    unresolved_conflict_status: str
    locked_binding_conflict: bool
    narrator_conflict: bool
    same_voice_conflict: bool
    same_similarity_cluster: bool
    role_similarity_conflict: bool
    shared_scene_count: int
    shared_speaking_scene_count: int
    first_shared_scene_id: str | None
    first_shared_scene_order: int | None
    total_dialogue_records_in_shared_scenes: int
    relationship_density: float
    dialogue_proximity: DialogueProximityEvidence
    voice_a_id: str | None
    voice_b_id: str | None
    voice_a_similarity_cluster: str | None
    voice_b_similarity_cluster: str | None
    same_provider_family: bool
    intentional_reuse: bool
    scarcity_relaxed_conflict: bool
    hard_separation: bool
    soft_separation: bool
    penalty_units: int
    deterministic_ordering: tuple[Any, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ConflictReport:
    schema_version: int
    book_id: str
    series_id: str
    pair_evidence: list[CharacterPairEvidence] = field(default_factory=list)
    conflicts: list[VoicePairConstraint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


class ConflictError(ValueError):
    pass


def validate_conflict_config(mapping: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(mapping, Mapping):
        return ["conflict config must be a mapping"]
    int_fields = {"schema_version", "adjacency_window", "alternating_dialogue_threshold"}
    for key in int_fields:
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, int):
            errors.append(f"conflict config {key} must be an integer")
        elif value < 0:
            errors.append(f"conflict config {key} must be greater than or equal to zero")
    for key in ("shared_scene_thresholds", "shared_speaking_scene_thresholds", "severity_thresholds", "same_voice_prohibition_thresholds", "pairwise_penalty_weights"):
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            errors.append(f"conflict config {key} must be a mapping")
            continue
        for sub_key, sub_value in value.items():
            if key == "shared_scene_thresholds" or key == "shared_speaking_scene_thresholds" or key == "severity_thresholds" or key == "same_voice_prohibition_thresholds" or key == "pairwise_penalty_weights":
                if not isinstance(sub_value, int) or sub_value < 0:
                    errors.append(f"conflict config {key}.{sub_key} must be a non-negative integer")
    rel = mapping.get("relationship_density_thresholds")
    if rel is not None:
        if not isinstance(rel, Mapping):
            errors.append("conflict config relationship_density_thresholds must be a mapping")
        else:
            for sub_key, sub_value in rel.items():
                if not isinstance(sub_value, (int, float)) or sub_value < 0:
                    errors.append(f"conflict config relationship_density_thresholds.{sub_key} must be a non-negative number")
    for key, allowed in (
        ("similarity_cluster_policy", _ALLOWED_SIMILARITY_POLICIES),
        ("narrator_separation_policy", _ALLOWED_NARRATOR_POLICIES),
        ("locked_conflict_policy", _ALLOWED_LOCKED_POLICIES),
        ("scarcity_relaxation_policy", _ALLOWED_RELAXATION_POLICIES),
        ("unresolved_conflict_behavior", _ALLOWED_UNRESOLVED_BEHAVIOR),
    ):
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or value not in allowed:
            errors.append(f"conflict config {key} must be one of {', '.join(sorted(allowed))}")
    pairs = mapping.get("hard_separation_tier_pairs")
    if pairs is not None:
        try:
            coerced = _coerce_tier_pairs(pairs)
        except ConflictError as exc:
            errors.append(str(exc))
        else:
            normalized = {tuple(sorted(pair)) for pair in coerced}
            if len(normalized) != len(coerced):
                errors.append("conflict config hard_separation_tier_pairs must not contain duplicate pairs")
    return errors


def build_character_pairs(profiles: Sequence[CharacterProfile]) -> list[tuple[str, str]]:
    ids = sorted(profile.canonical_character_id for profile in profiles)
    return [(left, right) for left, right in combinations(ids, 2)]


def calculate_dialogue_proximity(
    dialogues: Sequence[Mapping[str, Any]],
    character_a_id: str,
    character_b_id: str,
    *,
    adjacency_window: int,
) -> DialogueProximityEvidence:
    ordered = sorted(
        [dialogue for dialogue in dialogues if _dialogue_speaker(dialogue) in {character_a_id, character_b_id}],
        key=_dialogue_sort_key,
    )
    adjacent = 0
    near_adjacent = 0
    alternating = 0
    max_gap: int | None = None
    last_ab_speaker: str | None = None
    ab_sequence: list[str] = []

    for index, current in enumerate(ordered):
        current_speaker = _dialogue_speaker(current)
        if current_speaker is None:
            continue
        ab_sequence.append(current_speaker)
        if index == 0:
            continue
        previous = ordered[index - 1]
        previous_speaker = _dialogue_speaker(previous)
        if previous_speaker is None:
            continue
        if {previous_speaker, current_speaker} == {character_a_id, character_b_id}:
            gap = max(0, _dialogue_sort_index(current) - _dialogue_sort_index(previous) - 1)
            if max_gap is None or gap > max_gap:
                max_gap = gap
            if gap == 0:
                adjacent += 1
            elif gap <= adjacency_window:
                near_adjacent += 1

    for speaker in ab_sequence:
        if last_ab_speaker is not None and speaker != last_ab_speaker:
            alternating += 1
        last_ab_speaker = speaker

    return DialogueProximityEvidence(
        adjacency_window=adjacency_window,
        adjacent_dialogue_pairs=adjacent,
        near_adjacency_pairs=near_adjacent,
        alternating_dialogue_transitions=alternating,
        total_dialogue_records_in_shared_scenes=len(ordered),
        max_dialogue_gap=max_gap,
        notes=[],
    )


def evaluate_pair_conflict(evidence: CharacterPairEvidence, context: SceneConflictContext) -> VoicePairConstraint:
    config = context.config or ConflictConfig.default()
    categories: list[str] = []
    severity_score = 0
    penalty_units = 0

    if evidence.shared_speaking_scene_count > 0:
        categories.append("same-scene speaking conflict")
        severity_score += evidence.shared_speaking_scene_count * config.pairwise_penalty_weights["shared_speaking_scene"]
        penalty_units += evidence.shared_speaking_scene_count * config.pairwise_penalty_weights["shared_speaking_scene"]
    if evidence.shared_scene_count >= config.shared_scene_thresholds.get("high", 3) or evidence.relationship_density >= config.relationship_density_thresholds.get("high", 0.75):
        categories.append("frequent shared-scene conflict")
        severity_score += evidence.shared_scene_count * config.pairwise_penalty_weights["shared_scene"]
        penalty_units += evidence.shared_scene_count * config.pairwise_penalty_weights["shared_scene"]
    if evidence.dialogue_proximity.alternating_dialogue_transitions >= config.alternating_dialogue_threshold:
        categories.append("alternating-dialogue conflict")
        severity_score += evidence.dialogue_proximity.alternating_dialogue_transitions * config.pairwise_penalty_weights["alternating_dialogue"]
        penalty_units += evidence.dialogue_proximity.alternating_dialogue_transitions * config.pairwise_penalty_weights["alternating_dialogue"]
    if evidence.dialogue_proximity.adjacent_dialogue_pairs > 0 or evidence.dialogue_proximity.near_adjacency_pairs > 0:
        categories.append("dialogue-proximity conflict")
        proximity_count = evidence.dialogue_proximity.adjacent_dialogue_pairs + evidence.dialogue_proximity.near_adjacency_pairs
        severity_score += proximity_count * config.pairwise_penalty_weights["adjacent_dialogue"]
        penalty_units += proximity_count * config.pairwise_penalty_weights["near_dialogue"]
    if evidence.narrator_involved and evidence.shared_scene_count > 0:
        categories.append("narrator-character conflict")
        narrator_policy = config.narrator_separation_policy
        narrator_weight = config.pairwise_penalty_weights["narrator"]
        if narrator_policy == "discourage":
            narrator_weight = max(1, narrator_weight // 2)
        elif narrator_policy == "allow":
            narrator_weight = 0
        severity_score += narrator_weight
        penalty_units += narrator_weight
    if evidence.both_primary_or_major:
        categories.append("major-character distinction conflict")
        severity_score += config.pairwise_penalty_weights["role_similarity"]
        penalty_units += config.pairwise_penalty_weights["role_similarity"]
    if evidence.roles_similar and evidence.shared_scene_count > 0:
        categories.append("role-similarity conflict")
        severity_score += config.pairwise_penalty_weights["role_similarity"]
        penalty_units += config.pairwise_penalty_weights["role_similarity"]
    if evidence.locked_binding_conflict:
        categories.append("locked-binding conflict")
        severity_score += config.pairwise_penalty_weights["locked"]
        penalty_units += config.pairwise_penalty_weights["locked"]
    if evidence.same_voice and evidence.shared_scene_count > 0:
        categories.append("same-voice conflict")
        severity_score += config.pairwise_penalty_weights["same_voice"]
        penalty_units += config.pairwise_penalty_weights["same_voice"]
    if evidence.same_similarity_cluster and evidence.shared_scene_count > 0:
        categories.append("similarity-cluster conflict")
        severity_score += config.pairwise_penalty_weights["same_similarity_cluster"]
        penalty_units += config.pairwise_penalty_weights["same_similarity_cluster"]

    severity_score += _relationship_density_penalty(evidence.relationship_density)
    if evidence.shared_scene_count == 0 and severity_score == 0:
        severity = "none"
    else:
        severity = _classify_severity(severity_score, config)

    same_voice_prohibition = _same_voice_prohibition(evidence, config, severity)
    similarity_cluster_penalty = 0
    similarity_cluster_prohibition = False
    if evidence.same_similarity_cluster and evidence.shared_scene_count > 0:
        if config.similarity_cluster_policy == "hard_for_high_conflict" and severity in {"high", "critical"}:
            similarity_cluster_prohibition = True
            similarity_cluster_penalty = config.pairwise_penalty_weights["same_similarity_cluster"]
        elif config.similarity_cluster_policy == "soft_for_moderate" and severity in {"moderate", "high", "critical"}:
            similarity_cluster_penalty = config.pairwise_penalty_weights["same_similarity_cluster"]
        elif config.similarity_cluster_policy == "hard_for_high_conflict" and severity == "moderate":
            similarity_cluster_penalty = config.pairwise_penalty_weights["same_similarity_cluster"]
        elif config.similarity_cluster_policy == "ignore":
            similarity_cluster_penalty = 0

    intentional_reuse = False
    if evidence.same_voice and config.locked_conflict_policy == "allow_intentional_reuse" and evidence.shares_persisted_voice:
        intentional_reuse = True
        categories.append("intentional reuse")

    soft_separation = not same_voice_prohibition and (severity in {"low", "moderate"} or similarity_cluster_penalty > 0 or intentional_reuse)
    hard_separation = same_voice_prohibition or similarity_cluster_prohibition or evidence.locked_binding_conflict
    distinct_voice_requirement = evidence.shared_scene_count > 0 and (
        evidence.narrator_involved or evidence.same_voice or evidence.same_similarity_cluster or evidence.roles_similar or evidence.both_primary_or_major
    )
    reuse_eligibility = not hard_separation and (severity == "none" or soft_separation or intentional_reuse)

    scarcity_relaxed_conflict = False
    applicable_scarcity_relaxation = "none"
    if context.voice_budget is not None:
        scarcity_relaxed_conflict, applicable_scarcity_relaxation = _apply_scarcity_relaxation(evidence, severity, same_voice_prohibition, context.voice_budget, config)
        if scarcity_relaxed_conflict:
            categories.append("scarcity-relaxed conflict")
            if soft_separation and not same_voice_prohibition:
                reuse_eligibility = True
            elif severity in {"high", "critical"} and not evidence.both_primary_or_major:
                reuse_eligibility = True
    if evidence.narrator_involved and config.narrator_separation_policy != "prohibit":
        reuse_eligibility = True

    unresolved_conflict_status = _unresolved_status(evidence, severity, same_voice_prohibition, similarity_cluster_prohibition, intentional_reuse, scarcity_relaxed_conflict)
    if unresolved_conflict_status == "hard":
        categories.append("unresolved hard conflict")

    categories = _dedupe_preserve_order([category for category in categories if category in _ORDERED_CATEGORIES])
    conflict_reason = _build_conflict_reason(evidence, categories, severity, applicable_scarcity_relaxation)
    if evidence.narrator_involved and evidence.shared_scene_count > 0:
        narrator_phrase = {
            "allow": "allowed",
            "discourage": "discouraged",
            "prohibit": "prohibited",
        }.get(config.narrator_separation_policy, config.narrator_separation_policy)
        conflict_reason += f" | narrator sharing {narrator_phrase}"
    deterministic_ordering = _pair_ordering_key(evidence, severity_score)

    return VoicePairConstraint(
        character_a_id=evidence.character_a_id,
        character_b_id=evidence.character_b_id,
        categories=tuple(categories),
        severity=severity,
        severity_score=severity_score,
        distinct_voice_requirement=distinct_voice_requirement,
        same_voice_prohibition=same_voice_prohibition,
        similarity_cluster_prohibition=similarity_cluster_prohibition,
        similarity_cluster_penalty=similarity_cluster_penalty,
        reuse_eligibility=reuse_eligibility,
        conflict_reason=conflict_reason,
        applicable_scarcity_relaxation=applicable_scarcity_relaxation,
        unresolved_conflict_status=unresolved_conflict_status,
        locked_binding_conflict=evidence.locked_binding_conflict,
        narrator_conflict=evidence.narrator_involved,
        same_voice_conflict=evidence.same_voice,
        same_similarity_cluster=evidence.same_similarity_cluster,
        role_similarity_conflict=evidence.roles_similar,
        shared_scene_count=evidence.shared_scene_count,
        shared_speaking_scene_count=evidence.shared_speaking_scene_count,
        first_shared_scene_id=evidence.first_shared_scene_id,
        first_shared_scene_order=evidence.first_shared_scene_order,
        total_dialogue_records_in_shared_scenes=evidence.total_dialogue_records_in_shared_scenes,
        relationship_density=evidence.relationship_density,
        dialogue_proximity=evidence.dialogue_proximity,
        voice_a_id=evidence.voice_a_id,
        voice_b_id=evidence.voice_b_id,
        voice_a_similarity_cluster=evidence.voice_a_similarity_cluster,
        voice_b_similarity_cluster=evidence.voice_b_similarity_cluster,
        same_provider_family=evidence.same_provider_family,
        intentional_reuse=intentional_reuse,
        scarcity_relaxed_conflict=scarcity_relaxed_conflict,
        hard_separation=hard_separation,
        soft_separation=soft_separation,
        penalty_units=penalty_units + similarity_cluster_penalty,
        deterministic_ordering=deterministic_ordering,
    )


def evaluate_voice_pair_constraint(evidence: CharacterPairEvidence, context: SceneConflictContext) -> VoicePairConstraint:
    return evaluate_pair_conflict(evidence, context)


def apply_scarcity_relaxation(
    evidence: CharacterPairEvidence,
    severity: str,
    same_voice_prohibition: bool,
    voice_budget: VoiceBudget,
    config: ConflictConfig,
) -> tuple[bool, str]:
    level = getattr(voice_budget, "scarcity_level", "none")
    if level not in {"high", "critical"}:
        return False, "none"
    a_tier = evidence.character_a_tier
    b_tier = evidence.character_b_tier
    low_priority_tiers = {"supporting recurring characters", "minor speaking characters", "one-scene or one-off speakers", "unresolved speakers"}
    if evidence.narrator_involved:
        if config.narrator_separation_policy == "prohibit":
            return False, "narrator-distinctness"
        if level == "critical" and severity in {"high", "critical"}:
            return True, "scarcity-relaxed"
        return severity in {"low", "moderate"}, "narrator-sharing-discouraged"
    if a_tier in low_priority_tiers or b_tier in low_priority_tiers:
        if same_voice_prohibition and severity in {"high", "critical"}:
            return True, "scarcity-relaxed"
        return severity in {"low", "moderate", "high", "critical"}, "scarcity-relaxed"
    if evidence.both_primary_or_major:
        return False, "preserve_primary_major"
    if config.scarcity_relaxation_policy == "strict":
        return False, "preserve_primary_major"
    if severity in {"low", "moderate"}:
        return True, "same-cluster-softened" if evidence.same_similarity_cluster else "lower-tier-reuse"
    return False, "preserve_primary_major"


def analyze_scene_conflicts(context: SceneConflictContext) -> ConflictReport:
    config = context.config or ConflictConfig.default()
    if not isinstance(config, ConflictConfig):
        config = ConflictConfig.from_mapping(config)

    profiles = sorted(context.character_profiles, key=lambda profile: profile.canonical_character_id)
    profile_map = {profile.canonical_character_id: profile for profile in profiles}
    scene_index = _build_scene_index(context.scene_records)
    dialogue_index = _build_dialogue_index(context.dialogue_records)
    registry = context.voice_registry or {}
    if registry and not isinstance(registry, Mapping):
        raise ConflictError("voice registry must be a mapping")
    binding_state = context.series_bindings
    pair_evidence: list[CharacterPairEvidence] = []
    constraints: list[VoicePairConstraint] = []

    for character_a_id, character_b_id in build_character_pairs(profiles):
        evidence = _build_pair_evidence(
            character_a_id,
            character_b_id,
            profile_map,
            scene_index,
            dialogue_index,
            context,
            registry,
            binding_state,
        )
        pair_evidence.append(evidence)
        constraint = evaluate_pair_conflict(evidence, context)
        constraints.append(constraint)

    pair_evidence.sort(key=lambda item: item.pair_ordering_key)
    constraints.sort(key=lambda item: item.deterministic_ordering)
    report = ConflictReport(
        schema_version=SCENE_CONFLICT_SCHEMA_VERSION,
        book_id=_report_book_id(scene_index, context),
        series_id=_report_series_id(scene_index, context),
        pair_evidence=pair_evidence,
        conflicts=[constraint for constraint in constraints if constraint.severity != "none" or constraint.categories],
        warnings=_build_warnings(pair_evidence, constraints, context),
        summary=_build_summary(pair_evidence, constraints),
    )
    errors = validate_scene_conflict_report(dataclass_to_dict(report))
    if errors:
        raise ConflictError("; ".join(errors))
    return report


def serialize_conflict_report(report: ConflictReport | Mapping[str, Any]) -> str:
    return canonical_json_dumps(report)


def validate_scene_conflict_report(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, Mapping):
        return ["scene conflict report must be a mapping"]
    for key in ("schema_version", "book_id", "series_id", "pair_evidence", "conflicts", "summary"):
        if key not in data:
            errors.append(f"missing scene conflict report field: {key}")
    if not isinstance(data.get("schema_version"), int):
        errors.append("scene conflict report schema_version must be an integer")
    if not isinstance(data.get("book_id"), str) or not data.get("book_id"):
        errors.append("scene conflict report book_id must be a non-empty string")
    if not isinstance(data.get("series_id"), str) or not data.get("series_id"):
        errors.append("scene conflict report series_id must be a non-empty string")
    for key in ("pair_evidence", "conflicts"):
        value = data.get(key)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            errors.append(f"scene conflict report {key} must be a sequence")
    if not isinstance(data.get("summary"), Mapping):
        errors.append("scene conflict report summary must be a mapping")
    return errors


def _build_pair_evidence(
    character_a_id: str,
    character_b_id: str,
    profile_map: Mapping[str, CharacterProfile],
    scene_index: dict[str, dict[str, Any]],
    dialogue_index: dict[str, list[dict[str, Any]]],
    context: SceneConflictContext,
    registry: Mapping[str, Any],
    bindings: SeriesBindings | None,
) -> CharacterPairEvidence:
    profile_a = profile_map[character_a_id]
    profile_b = profile_map[character_b_id]
    tier_a = classify_character_tier(profile_a, BudgetConfig.default())
    tier_b = classify_character_tier(profile_b, BudgetConfig.default())
    scenes_a = {scene_id for scene_id, scene in scene_index.items() if character_a_id in scene["character_ids"]}
    scenes_b = {scene_id for scene_id, scene in scene_index.items() if character_b_id in scene["character_ids"]}
    shared_scenes = sorted(scenes_a & scenes_b, key=lambda scene_id: _scene_sort_key(scene_index[scene_id]))
    shared_speaking_scenes = [scene_id for scene_id in shared_scenes if character_a_id in scene_index[scene_id]["speaking_character_ids"] and character_b_id in scene_index[scene_id]["speaking_character_ids"]]
    first_shared_scene_id = shared_scenes[0] if shared_scenes else None
    first_shared_scene_order = scene_index[first_shared_scene_id]["scene_order"] if first_shared_scene_id is not None else None
    shared_dialogues = [dialogue for scene_id in shared_scenes for dialogue in dialogue_index.get(scene_id, [])]
    proximity = calculate_dialogue_proximity(shared_dialogues, character_a_id, character_b_id, adjacency_window=context.config.adjacency_window)
    total_shared_dialogue_records = len(shared_dialogues)
    relationship_density = _relationship_density(profile_a, profile_b, len(shared_speaking_scenes), len(shared_scenes))

    voice_a = _resolve_voice(character_a_id, profile_a, context, registry, bindings)
    voice_b = _resolve_voice(character_b_id, profile_b, context, registry, bindings)
    same_voice = bool(voice_a.voice_id and voice_a.voice_id == voice_b.voice_id)
    same_similarity_cluster = bool(voice_a.similarity_cluster and voice_a.similarity_cluster == voice_b.similarity_cluster)
    same_provider_family = bool(voice_a.provider and voice_a.provider == voice_b.provider)
    shares_persisted_voice = same_voice and (voice_a.source != "none" or voice_b.source != "none")
    narrator_involved = tier_a == "narrator" or tier_b == "narrator"
    hard_pairs = {tuple(sorted(pair)) for pair in context.config.hard_separation_tier_pairs}
    both_primary_or_major = tuple(sorted((tier_a, tier_b))) in hard_pairs or (
        tier_a in {"primary or lead characters", "major recurring characters"}
        and tier_b in {"primary or lead characters", "major recurring characters"}
    )
    roles_similar = tier_a == tier_b or {tier_a, tier_b}.issubset({"primary or lead characters", "major recurring characters", "supporting recurring characters"})
    pair_ordering_key = (
        0 if shared_scenes else 1,
        -len(shared_speaking_scenes),
        -len(shared_scenes),
        character_a_id,
        character_b_id,
    )
    return CharacterPairEvidence(
        character_a_id=character_a_id,
        character_b_id=character_b_id,
        character_a_name=profile_a.canonical_name,
        character_b_name=profile_b.canonical_name,
        character_a_tier=tier_a,
        character_b_tier=tier_b,
        character_a_prominence=profile_a.prominence,
        character_b_prominence=profile_b.prominence,
        character_a_recurrence=profile_a.likely_recurrence,
        character_b_recurrence=profile_b.likely_recurrence,
        character_a_locked=voice_a.locked,
        character_b_locked=voice_b.locked,
        locked_binding_conflict=bool(
            (voice_a.locked or voice_b.locked)
            and (
                same_voice
                or (same_similarity_cluster and len(shared_speaking_scenes) > 0)
                or narrator_involved
                or both_primary_or_major
            )
        ),
        shared_scene_count=len(shared_scenes),
        shared_speaking_scene_count=len(shared_speaking_scenes),
        first_shared_scene_id=first_shared_scene_id,
        first_shared_scene_order=first_shared_scene_order,
        total_dialogue_records_in_shared_scenes=total_shared_dialogue_records,
        relationship_density=relationship_density,
        dialogue_proximity=proximity,
        voice_a_id=voice_a.voice_id,
        voice_b_id=voice_b.voice_id,
        voice_a_similarity_cluster=voice_a.similarity_cluster,
        voice_b_similarity_cluster=voice_b.similarity_cluster,
        shares_persisted_voice=shares_persisted_voice,
        same_voice=same_voice,
        same_similarity_cluster=same_similarity_cluster,
        same_provider_family=same_provider_family,
        narrator_involved=narrator_involved,
        both_primary_or_major=both_primary_or_major,
        roles_similar=roles_similar,
    )


def _resolve_voice(
    character_id: str,
    profile: CharacterProfile,
    context: SceneConflictContext,
    registry: Mapping[str, Any],
    bindings: SeriesBindings | None,
) -> _ResolvedVoice:
    binding = get_character_binding(bindings, character_id) if bindings is not None else None
    narrator_binding = get_narrator_binding(bindings) if bindings is not None else None
    if profile.role == "narrator" or (profile.prominence and "narrator" in profile.prominence.lower()):
        binding = narrator_binding or binding
    if binding is not None and binding.provider and binding.provider_voice_id:
        cluster = _registry_cluster(registry, binding.provider, binding.provider_voice_id)
        return _ResolvedVoice(
            voice_id=_binding_voice_id(binding),
            provider=binding.provider,
            provider_voice_id=binding.provider_voice_id,
            similarity_cluster=cluster,
            locked=binding.locked,
            source="binding",
        )
    candidate_scores = context.candidate_scores_by_character.get(character_id, ())
    for score in candidate_scores:
        cluster = _registry_cluster(registry, score.provider, score.provider_voice_id)
        return _ResolvedVoice(
            voice_id=score.voice_id,
            provider=score.provider,
            provider_voice_id=score.provider_voice_id,
            similarity_cluster=cluster,
            locked=False,
            source="candidate",
        )
    return _ResolvedVoice(voice_id=None, provider=None, provider_voice_id=None, similarity_cluster=None, locked=False, source="none")


@dataclass(frozen=True)
class _ResolvedVoice:
    voice_id: str | None
    provider: str | None
    provider_voice_id: str | None
    similarity_cluster: str | None
    locked: bool
    source: str


def _registry_cluster(registry: Mapping[str, Any], provider: str | None, provider_voice_id: str | None) -> str | None:
    if not provider or not provider_voice_id:
        return None
    voices = registry.get("voices") if isinstance(registry, Mapping) else None
    if not isinstance(voices, Sequence):
        return None
    for voice in voices:
        if not isinstance(voice, Mapping):
            continue
        if voice.get("provider") == provider and voice.get("provider_voice_id") == provider_voice_id:
            cluster = voice.get("similarity_cluster")
            return str(cluster) if isinstance(cluster, str) and cluster else None
    return None


def _build_warnings(
    pair_evidence: Sequence[CharacterPairEvidence],
    constraints: Sequence[VoicePairConstraint],
    context: SceneConflictContext,
) -> list[str]:
    warnings: list[str] = []
    if context.voice_budget is not None and getattr(context.voice_budget, "scarcity_level", "none") in {"high", "critical"}:
        warnings.append(f"scarcity level {context.voice_budget.scarcity_level} may require lower-tier reuse")
    if any(constraint.unresolved_conflict_status == "hard" for constraint in constraints):
        warnings.append("one or more hard conflicts remain unresolved")
    if not pair_evidence:
        warnings.append("no character pairs were available for scene conflict analysis")
    return warnings


def _build_summary(
    pair_evidence: Sequence[CharacterPairEvidence],
    constraints: Sequence[VoicePairConstraint],
) -> dict[str, Any]:
    severity_counts = {severity: 0 for severity in ("none", "low", "moderate", "high", "critical")}
    for constraint in constraints:
        severity_counts[constraint.severity] = severity_counts.get(constraint.severity, 0) + 1
    return {
        "pair_count": len(pair_evidence),
        "conflict_count": len([constraint for constraint in constraints if constraint.severity != "none" or constraint.categories]),
        "hard_conflict_count": len([constraint for constraint in constraints if constraint.hard_separation]),
        "scarcity_relaxed_count": len([constraint for constraint in constraints if constraint.scarcity_relaxed_conflict]),
        "narrator_conflict_count": len([constraint for constraint in constraints if constraint.narrator_conflict]),
        "locked_conflict_count": len([constraint for constraint in constraints if constraint.locked_binding_conflict]),
        "severity_counts": severity_counts,
    }


def _build_conflict_reason(evidence: CharacterPairEvidence, categories: Sequence[str], severity: str, scarcity_relaxation: str) -> str:
    if not categories:
        return "no conflict"
    parts = [
        f"{evidence.character_a_id} vs {evidence.character_b_id}",
        f"severity={severity}",
        f"shared_scenes={evidence.shared_scene_count}",
        f"shared_speaking_scenes={evidence.shared_speaking_scene_count}",
    ]
    if evidence.dialogue_proximity.alternating_dialogue_transitions:
        parts.append(f"alternating={evidence.dialogue_proximity.alternating_dialogue_transitions}")
    if scarcity_relaxation != "none":
        parts.append(f"scarcity={scarcity_relaxation}")
    parts.append("categories=" + ",".join(categories))
    return " | ".join(parts)


def _pair_ordering_key(evidence: CharacterPairEvidence, severity_score: int) -> tuple[Any, ...]:
    return (
        -severity_score,
        -evidence.shared_speaking_scene_count,
        -evidence.shared_scene_count,
        evidence.character_a_id,
        evidence.character_b_id,
    )


def _classify_severity(score: int, config: ConflictConfig) -> str:
    if score <= 0:
        return "none"
    low = config.severity_thresholds.get("low", 3)
    moderate = config.severity_thresholds.get("moderate", 7)
    high = config.severity_thresholds.get("high", 12)
    if score < low:
        return "low"
    if score < moderate:
        return "moderate"
    if score < high:
        return "high"
    return "critical"


def _same_voice_prohibition(evidence: CharacterPairEvidence, config: ConflictConfig, severity: str) -> bool:
    thresholds = config.same_voice_prohibition_thresholds
    if evidence.narrator_involved and config.narrator_separation_policy == "prohibit":
        return True
    if evidence.locked_binding_conflict:
        if config.locked_conflict_policy == "invalid":
            return True
        if config.locked_conflict_policy == "warn":
            return severity in {"high", "critical"}
        return evidence.same_voice and severity in {"high", "critical"}
    if evidence.same_voice and evidence.shared_speaking_scene_count >= thresholds.get("shared_speaking_scene_count", 2):
        return True
    if evidence.same_voice and evidence.dialogue_proximity.alternating_dialogue_transitions >= thresholds.get("alternating_dialogue_transitions", 3):
        return True
    if evidence.same_voice and evidence.shared_scene_count >= thresholds.get("shared_scene_count", 3) and severity in {"high", "critical"}:
        return True
    return False


def _relationship_density_penalty(density: float) -> int:
    return int(round(density * 10))


def _relationship_density(profile_a: CharacterProfile, profile_b: CharacterProfile, shared_speaking_scene_count: int, shared_scene_count: int) -> float:
    denominator = max(1, min(profile_a.scene_count or 0, profile_b.scene_count or 0, shared_scene_count or 1))
    return shared_speaking_scene_count / denominator


def _apply_scarcity_relaxation(
    evidence: CharacterPairEvidence,
    severity: str,
    same_voice_prohibition: bool,
    voice_budget: VoiceBudget,
    config: ConflictConfig,
) -> tuple[bool, str]:
    level = getattr(voice_budget, "scarcity_level", "none")
    if level not in {"high", "critical"}:
        return False, "none"
    low_priority_tiers = {"supporting recurring characters", "minor speaking characters", "one-scene or one-off speakers", "unresolved speakers"}
    if evidence.narrator_involved:
        if config.narrator_separation_policy == "prohibit":
            return False, "narrator-distinctness"
        return True, "narrator-sharing-discouraged" if severity in {"low", "moderate", "high", "critical"} else "none"
    if evidence.both_primary_or_major:
        return False, "preserve_primary_major"
    if evidence.character_a_tier in low_priority_tiers or evidence.character_b_tier in low_priority_tiers:
        return True, "lower-tier-reuse"
    if evidence.same_similarity_cluster and severity in {"moderate", "high", "critical"}:
        return True, "scarcity-relaxed"
    if same_voice_prohibition and severity in {"high", "critical"}:
        return True, "lower-tier-reuse"
    return False, "preserve_primary_major"


def _unresolved_status(
    evidence: CharacterPairEvidence,
    severity: str,
    same_voice_prohibition: bool,
    similarity_cluster_prohibition: bool,
    intentional_reuse: bool,
    scarcity_relaxed: bool,
) -> str:
    if severity == "none":
        return "none"
    if intentional_reuse:
        return "resolved"
    if same_voice_prohibition or similarity_cluster_prohibition:
        return "relaxable" if scarcity_relaxed else "hard"
    if severity in {"high", "critical"} and evidence.shared_scene_count > 0 and (
        evidence.shared_speaking_scene_count > 0 or evidence.both_primary_or_major or evidence.narrator_involved or evidence.roles_similar
    ):
        return "relaxable" if scarcity_relaxed else "hard"
    return "resolved"


def _build_scene_index(scene_records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in scene_records:
        scene_id = _scene_id(record)
        index[scene_id] = {
            "scene_id": scene_id,
            "scene_order": _scene_order(record),
            "character_ids": tuple(sorted({str(value) for value in record.get("character_ids", []) if value is not None})),
            "speaking_character_ids": tuple(sorted({str(value) for value in record.get("speaking_character_ids", []) if value is not None})),
        }
    return dict(sorted(index.items(), key=lambda item: _scene_sort_key(item[1])))


def _build_dialogue_index(dialogue_records: Sequence[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for record in dialogue_records:
        scene_id = _dialogue_scene_id(record)
        index.setdefault(scene_id, []).append(dict(record))
    for scene_id in list(index.keys()):
        index[scene_id] = sorted(index[scene_id], key=_dialogue_sort_key)
    return dict(sorted(index.items(), key=lambda item: item[0]))


def _scene_id(record: Mapping[str, Any]) -> str:
    value = record.get("scene_id")
    if not isinstance(value, str) or not value:
        raise ConflictError("scene record missing scene_id")
    return value


def _scene_order(record: Mapping[str, Any]) -> int:
    value = record.get("scene_order")
    if not isinstance(value, int):
        raise ConflictError(f"scene {record.get('scene_id', '<unknown>')} missing integer scene_order")
    return value


def _scene_sort_key(scene: Mapping[str, Any]) -> tuple[int, str]:
    return (int(scene["scene_order"]), str(scene["scene_id"]))


def _dialogue_scene_id(record: Mapping[str, Any]) -> str:
    value = record.get("scene_id")
    if not isinstance(value, str) or not value:
        raise ConflictError("dialogue record missing scene_id")
    return value


def _dialogue_sort_index(record: Mapping[str, Any]) -> int:
    value = record.get("order")
    if not isinstance(value, int):
        raise ConflictError(f"dialogue {record.get('dialogue_id', '<unknown>')} missing integer order")
    return value


def _dialogue_sort_key(record: Mapping[str, Any]) -> tuple[int, str]:
    return (_dialogue_sort_index(record), str(record.get("dialogue_id", "")))


def _dialogue_speaker(record: Mapping[str, Any]) -> str | None:
    value = record.get("speaker_character_id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConflictError("dialogue speaker_character_id must be a string or null")
    return value or None


def _binding_voice_id(binding: SeriesVoiceBinding) -> str | None:
    if binding.voice_id:
        return binding.voice_id
    if binding.provider and binding.provider_voice_id:
        return f"{binding.provider}.{binding.provider_voice_id}"
    return None


def _coerce_tier_pairs(value: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(value, tuple) and all(isinstance(item, tuple) and len(item) == 2 for item in value):
        return tuple((str(a), str(b)) for a, b in value)
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ConflictError("conflict config hard_separation_tier_pairs must be a sequence of two-item sequences")
    pairs: list[tuple[str, str]] = []
    for idx, pair in enumerate(value):
        if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)) or len(pair) != 2:
            raise ConflictError(f"conflict config hard_separation_tier_pairs[{idx}] must be a two-item sequence")
        left, right = pair
        if not isinstance(left, str) or not left:
            raise ConflictError(f"conflict config hard_separation_tier_pairs[{idx}][0] must be a non-empty string")
        if not isinstance(right, str) or not right:
            raise ConflictError(f"conflict config hard_separation_tier_pairs[{idx}][1] must be a non-empty string")
        pairs.append((left, right))
    return tuple(pairs)


def _report_book_id(scene_index: Mapping[str, Any], context: SceneConflictContext) -> str:
    for record in context.scene_records:
        value = record.get("book_id")
        if isinstance(value, str) and value:
            return value
    if scene_index:
        first_scene_id = next(iter(scene_index.values()))
        if isinstance(first_scene_id, Mapping):
            value = first_scene_id.get("book_id")
            if isinstance(value, str) and value:
                return value
    return "unknown"


def _report_series_id(scene_index: Mapping[str, Any], context: SceneConflictContext) -> str:
    for record in context.scene_records:
        value = record.get("series_id")
        if isinstance(value, str) and value:
            return value
    if scene_index:
        first_scene_id = next(iter(scene_index.values()))
        if isinstance(first_scene_id, Mapping):
            value = first_scene_id.get("series_id")
            if isinstance(value, str) and value:
                return value
    return "unknown"


def _dedupe_preserve_order(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
