from __future__ import annotations

from dataclasses import dataclass, field, MISSING
from typing import Any, Mapping, Sequence

from .bindings import binding_precedence, binding_registry_status
from .models import CharacterProfile, SeriesBindings, SeriesVoiceBinding, dataclass_to_dict
from .registry import voice_registry_key
from .schema import canonical_json_dumps
from .scoring import CandidateScore

BUDGET_SCHEMA_VERSION = 1

_TIER_ORDER = (
    "narrator",
    "primary or lead characters",
    "major recurring characters",
    "supporting recurring characters",
    "minor speaking characters",
    "one-scene or one-off speakers",
    "unresolved speakers",
)

_TIER_PRIORITY = {
    "narrator": 0,
    "primary or lead characters": 1,
    "major recurring characters": 2,
    "supporting recurring characters": 3,
    "minor speaking characters": 4,
    "one-scene or one-off speakers": 5,
    "unresolved speakers": 6,
}

_ROLE_HINTS = {
    "primary or lead characters": {
        "protagonist",
        "main",
        "lead",
        "principal",
        "hero",
        "central",
    },
    "major recurring characters": {
        "major recurring",
        "recurring major",
        "major",
        "deuteragonist",
        "antagonist",
        "important recurring",
    },
    "supporting recurring characters": {
        "supporting recurring",
        "recurring supporting",
        "supporting",
        "ensemble",
        "companion",
        "ally",
    },
}

_PROMINENCE_HINTS = {
    "primary or lead characters": {"primary", "lead", "principal", "main"},
    "major recurring characters": {"major recurring", "major", "recurring major"},
    "supporting recurring characters": {"supporting recurring", "supporting", "recurring supporting"},
}


@dataclass(frozen=True)
class BudgetConfig:
    schema_version: int = BUDGET_SCHEMA_VERSION
    narrator_reserve: int = 1
    narrator_sharing_policy: str = "prefer_distinct"
    tier_weights: dict[str, int] = field(
        default_factory=lambda: {
            "narrator": 1,
            "primary or lead characters": 2,
            "major recurring characters": 2,
            "supporting recurring characters": 1,
            "minor speaking characters": 1,
            "one-scene or one-off speakers": 0,
            "unresolved speakers": 0,
        }
    )
    minimum_distinct_voices_by_tier: dict[str, int] = field(
        default_factory=lambda: {
            "narrator": 1,
            "primary or lead characters": 1,
            "major recurring characters": 1,
            "supporting recurring characters": 1,
            "minor speaking characters": 0,
            "one-scene or one-off speakers": 0,
            "unresolved speakers": 0,
        }
    )
    maximum_reuse_by_tier: dict[str, int] = field(
        default_factory=lambda: {
            "narrator": 0,
            "primary or lead characters": 0,
            "major recurring characters": 1,
            "supporting recurring characters": 2,
            "minor speaking characters": 4,
            "one-scene or one-off speakers": 6,
            "unresolved speakers": 2,
        }
    )
    protected_tier_thresholds: dict[str, int] = field(
        default_factory=lambda: {
            "narrator": 1,
            "primary or lead characters": 1,
            "major recurring characters": 1,
            "supporting recurring characters": 0,
        }
    )
    scarcity_thresholds: dict[str, float] = field(
        default_factory=lambda: {
            "low": 1.0,
            "moderate": 1.2,
            "high": 1.5,
            "critical": 2.0,
        }
    )
    reuse_penalty_scale: int = 1
    locked_binding_capacity_behavior: str = "reserve"
    inherited_binding_reserve_behavior: str = "reserve_when_available"
    unknown_prominence_behavior: str = "conservative"
    one_off_sharing_policy: str = "preferred_due_to_scarcity"
    dialogue_weight: int = 1
    scene_weight: int = 1
    recurrence_bonus: int = 1
    relationship_density_bonus: int = 1
    first_appearance_bonus: int = 1
    first_appearance_threshold: int = 3
    locked_binding_bonus: int = 1
    manual_override_bonus: int = 1
    inherited_binding_bonus: int = 1
    narrator_locked_bonus: int = 1

    @classmethod
    def default(cls) -> "BudgetConfig":
        return cls()

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "BudgetConfig":
        if mapping is None:
            return cls.default()
        errors = validate_budget_config(mapping)
        if errors:
            raise BudgetError("; ".join(errors))
        payload = {}
        for field_def in cls.__dataclass_fields__.values():
            if field_def.default is not MISSING:
                default_value = field_def.default
            else:
                default_value = field_def.default_factory()  # type: ignore[misc]
            payload[field_def.name] = mapping.get(field_def.name, default_value)
        return cls(**payload)


@dataclass(frozen=True)
class BudgetContext:
    character_profiles: tuple[CharacterProfile, ...]
    candidate_scores: tuple[CandidateScore, ...]
    series_bindings: SeriesBindings | None = None
    narrator_required: bool = True
    config: BudgetConfig = field(default_factory=BudgetConfig.default)


@dataclass(frozen=True)
class TierDemand:
    tier: str
    character_ids: list[str] = field(default_factory=list)
    character_count: int = 0
    demand_units: int = 0
    distinct_voice_target: int = 0
    capacity_reserved: int = 0
    reuse_allowance: int = 0
    reasons: list[str] = field(default_factory=list)
    protected: bool = False
    shareable: bool = False


@dataclass(frozen=True)
class ReuseAllowance:
    tier: str
    policy: str
    max_reuse: int
    penalty_per_reuse: int
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScarcityDecision:
    level: str
    ratio: float
    triggers: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VoiceBudget:
    schema_version: int
    narrator_required: bool
    total_eligible_voice_inventory: int
    narrator_reserved_voice_count: int
    locked_binding_voice_count: int
    voices_available_for_new_planning: int
    weighted_demand_total: int
    role_tier_demand: dict[str, TierDemand] = field(default_factory=dict)
    role_tier_capacity: dict[str, int] = field(default_factory=dict)
    distinct_voice_targets: dict[str, int] = field(default_factory=dict)
    reuse_allowances: dict[str, ReuseAllowance] = field(default_factory=dict)
    scarcity_decision: ScarcityDecision = field(default_factory=lambda: ScarcityDecision(level="none", ratio=0.0))
    scarcity_level: str = "none"
    scarcity_penalties: dict[str, int] = field(default_factory=dict)
    candidate_scarcity_penalties: dict[str, int] = field(default_factory=dict)
    protected_characters: list[str] = field(default_factory=list)
    protected_tiers: list[str] = field(default_factory=list)
    shareable_characters: list[str] = field(default_factory=list)
    shareable_tiers: list[str] = field(default_factory=list)
    downgrade_decisions: list[str] = field(default_factory=list)
    unresolved_capacity_conflicts: list[str] = field(default_factory=list)
    summary_statistics: dict[str, Any] = field(default_factory=dict)


class BudgetError(ValueError):
    pass


def validate_budget_config(mapping: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(mapping, Mapping):
        return ["budget config must be a mapping"]
    int_fields = {
        "schema_version",
        "narrator_reserve",
        "reuse_penalty_scale",
        "dialogue_weight",
        "scene_weight",
        "recurrence_bonus",
        "relationship_density_bonus",
        "first_appearance_bonus",
        "first_appearance_threshold",
        "locked_binding_bonus",
        "manual_override_bonus",
        "inherited_binding_bonus",
        "narrator_locked_bonus",
    }
    for key in int_fields:
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, int):
            errors.append(f"budget config {key} must be an integer")
        elif value < 0:
            errors.append(f"budget config {key} must be greater than or equal to zero")
    for key in ("tier_weights", "minimum_distinct_voices_by_tier", "maximum_reuse_by_tier", "protected_tier_thresholds"):
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, Mapping):
            errors.append(f"budget config {key} must be a mapping")
            continue
        for sub_key, sub_value in value.items():
            if key == "protected_tier_thresholds":
                if not isinstance(sub_value, int) or sub_value < 0:
                    errors.append(f"budget config {key}.{sub_key} must be a non-negative integer")
            else:
                if not isinstance(sub_value, int) or sub_value < 0:
                    errors.append(f"budget config {key}.{sub_key} must be a non-negative integer")
    scarcity_thresholds = mapping.get("scarcity_thresholds")
    if scarcity_thresholds is not None:
        if not isinstance(scarcity_thresholds, Mapping):
            errors.append("budget config scarcity_thresholds must be a mapping")
        else:
            for sub_key, sub_value in scarcity_thresholds.items():
                if not isinstance(sub_value, (int, float)) or sub_value < 0:
                    errors.append(f"budget config scarcity_thresholds.{sub_key} must be a non-negative number")
    for key in (
        "narrator_sharing_policy",
        "locked_binding_capacity_behavior",
        "inherited_binding_reserve_behavior",
        "unknown_prominence_behavior",
        "one_off_sharing_policy",
    ):
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"budget config {key} must be a non-empty string")
    if mapping.get("narrator_sharing_policy") not in {None, "prefer_distinct", "allow", "prohibit"}:
        errors.append("budget config narrator_sharing_policy must be one of prefer_distinct, allow, prohibit")
    if mapping.get("unknown_prominence_behavior") not in {None, "conservative", "neutral"}:
        errors.append("budget config unknown_prominence_behavior must be one of conservative, neutral")
    if mapping.get("locked_binding_capacity_behavior") not in {None, "reserve", "soft_preference"}:
        errors.append("budget config locked_binding_capacity_behavior must be one of reserve, soft_preference")
    if mapping.get("inherited_binding_reserve_behavior") not in {None, "reserve_when_available", "soft_preference", "ignore"}:
        errors.append("budget config inherited_binding_reserve_behavior must be one of reserve_when_available, soft_preference, ignore")
    if mapping.get("one_off_sharing_policy") not in {None, "preferred_due_to_scarcity", "permitted", "prohibited"}:
        errors.append("budget config one_off_sharing_policy must be one of preferred_due_to_scarcity, permitted, prohibited")
    return errors


def classify_character_tier(profile: CharacterProfile, config: BudgetConfig | Mapping[str, Any] | None = None) -> str:
    _ = _coerce_config(config)
    role = _normalized_text(profile.role)
    prominence = _normalized_text(profile.prominence)
    speaking_frequency = profile.speaking_frequency or 0
    scene_count = profile.scene_count or 0
    dialogue_count = profile.dialogue_count or 0
    likely_recurrence = profile.likely_recurrence

    if "narrator" in role or "narrator" in prominence:
        return "narrator"
    if _matches_hint(role, _ROLE_HINTS["primary or lead characters"]) or _matches_hint(prominence, _PROMINENCE_HINTS["primary or lead characters"]):
        return "primary or lead characters"
    if _matches_hint(role, _ROLE_HINTS["major recurring characters"]) or _matches_hint(prominence, _PROMINENCE_HINTS["major recurring characters"]):
        return "major recurring characters"
    if speaking_frequency >= 10 or scene_count >= 6 or dialogue_count >= 8:
        return "primary or lead characters"
    if speaking_frequency >= 6 or scene_count >= 4 or dialogue_count >= 5 or likely_recurrence is True:
        return "major recurring characters"
    if speaking_frequency >= 3 or scene_count >= 3 or dialogue_count >= 3:
        return "supporting recurring characters"
    if not role and not prominence and speaking_frequency == 0 and scene_count == 0 and dialogue_count == 0:
        return "unresolved speakers"
    if speaking_frequency <= 1 and scene_count <= 1 and dialogue_count <= 1:
        return "one-scene or one-off speakers"
    if speaking_frequency <= 2 or scene_count <= 2 or dialogue_count <= 2:
        return "minor speaking characters"
    if likely_recurrence is True:
        return "supporting recurring characters"
    return "unresolved speakers"


def calculate_weighted_demand(profiles: Sequence[CharacterProfile], config: BudgetConfig | Mapping[str, Any] | None = None) -> dict[str, TierDemand]:
    budget_config = _coerce_config(config)
    grouped: dict[str, list[CharacterProfile]] = {tier: [] for tier in _TIER_ORDER}
    for profile in profiles:
        tier = classify_character_tier(profile, budget_config)
        grouped.setdefault(tier, []).append(profile)

    demands: dict[str, TierDemand] = {}
    for tier in _TIER_ORDER:
        tier_profiles = grouped.get(tier, [])
        if not tier_profiles:
            demands[tier] = TierDemand(tier=tier)
            continue
        demand_units = 0
        reasons: list[str] = []
        character_ids: list[str] = []
        for profile in tier_profiles:
            character_ids.append(profile.canonical_character_id)
            base = budget_config.tier_weights.get(tier, 0)
            demand = max(1, base)
            if profile.dialogue_count >= 8:
                demand += budget_config.dialogue_weight
                reasons.append(f"{profile.canonical_character_id}: dialogue count supports stronger reservation")
            if profile.scene_count >= 5:
                demand += budget_config.scene_weight
                reasons.append(f"{profile.canonical_character_id}: scene count supports stronger reservation")
            if profile.likely_recurrence is True:
                demand += budget_config.recurrence_bonus
                reasons.append(f"{profile.canonical_character_id}: recurrence evidence supports stronger reservation")
            if len(profile.scene_relationships) >= 2:
                demand += budget_config.relationship_density_bonus
                reasons.append(f"{profile.canonical_character_id}: relationship density supports stronger reservation")
            if profile.first_appearance_order is not None and profile.first_appearance_order <= budget_config.first_appearance_threshold and tier in {
                "primary or lead characters",
                "major recurring characters",
                "supporting recurring characters",
            }:
                demand += budget_config.first_appearance_bonus
                reasons.append(f"{profile.canonical_character_id}: early first appearance supports continuity")
            demand_units += demand
        demands[tier] = TierDemand(
            tier=tier,
            character_ids=character_ids,
            character_count=len(tier_profiles),
            demand_units=demand_units,
            distinct_voice_target=max(budget_config.minimum_distinct_voices_by_tier.get(tier, 0), min(demand_units, len(tier_profiles) if tier not in {"narrator", "unresolved speakers"} else max(1, len(tier_profiles)))),
            reasons=_dedupe_preserve_order(reasons),
            protected=tier in {"narrator", "primary or lead characters", "major recurring characters"},
            shareable=tier in {"supporting recurring characters", "minor speaking characters", "one-scene or one-off speakers", "unresolved speakers"},
        )
    return demands


def build_reuse_policy(
    tier: str,
    demand: TierDemand,
    *,
    scarcity_level: str,
    config: BudgetConfig,
) -> ReuseAllowance:
    if tier == "narrator":
        if config.narrator_sharing_policy == "prohibit":
            policy = "prohibited"
            max_reuse = 0
        elif config.narrator_sharing_policy == "allow":
            policy = "permitted_with_penalty" if scarcity_level in {"high", "critical"} else "strongly_discouraged"
            max_reuse = 1 if scarcity_level in {"high", "critical"} else 0
        else:
            policy = "strongly_discouraged"
            max_reuse = 0
    elif tier == "primary or lead characters":
        policy = "strongly_discouraged" if scarcity_level in {"none", "low", "moderate"} else "permitted_with_penalty"
        max_reuse = 0 if scarcity_level in {"none", "low"} else 1
    elif tier == "major recurring characters":
        policy = "strongly_discouraged" if scarcity_level in {"none", "low"} else "permitted_with_penalty"
        max_reuse = 1 if scarcity_level in {"high", "critical"} else 0
    elif tier == "supporting recurring characters":
        policy = "permitted_with_penalty" if scarcity_level in {"none", "low"} else "preferred_due_to_scarcity"
        max_reuse = config.maximum_reuse_by_tier.get(tier, 0)
    elif tier in {"minor speaking characters", "one-scene or one-off speakers"}:
        policy = config.one_off_sharing_policy
        max_reuse = config.maximum_reuse_by_tier.get(tier, 0)
    else:
        policy = "permitted_with_penalty" if scarcity_level in {"high", "critical"} else "strongly_discouraged"
        max_reuse = config.maximum_reuse_by_tier.get(tier, 0)
    penalty_per_reuse = config.reuse_penalty_scale * _reuse_penalty_factor(tier, scarcity_level)
    reasons = [
        f"tier={tier}",
        f"scarcity={scarcity_level}",
        f"policy={policy}",
    ]
    if demand.reuse_allowance > 0:
        reasons.append(f"demand exceeds distinct capacity by {demand.reuse_allowance}")
    return ReuseAllowance(tier=tier, policy=policy, max_reuse=max_reuse, penalty_per_reuse=penalty_per_reuse, reasons=reasons)


def calculate_voice_budget(context: BudgetContext) -> VoiceBudget:
    config = context.config or BudgetConfig.default()
    eligible_scores = [score for score in context.candidate_scores if score.eligible]
    eligible_voice_keys = {score.registry_key for score in eligible_scores}
    eligible_count = len(eligible_voice_keys)
    narrator_binding = _binding_for_narrator(context.series_bindings)
    character_bindings = _binding_for_characters(context.series_bindings)
    candidate_lookup = {score.registry_key: score for score in eligible_scores}
    locked_binding_reasons: list[str] = []
    valid_locked_voice_keys: list[str] = []
    invalid_locked_voice_keys: list[str] = []
    duplicate_locked_voice_keys: list[str] = []
    narrator_locked_consumed = False

    if narrator_binding is not None and narrator_binding.locked:
        narrator_key = _binding_key(narrator_binding)
        narrator_score = candidate_lookup.get(narrator_key)
        if narrator_score is not None:
            valid_locked_voice_keys.append(narrator_key)
            narrator_locked_consumed = True
        else:
            invalid_locked_voice_keys.append(narrator_key)
            locked_binding_reasons.append(f"unavailable locked narrator voice: {narrator_binding.voice_id or narrator_key}")

    seen_locked_keys: dict[str, str] = {}
    locked_character_ids: list[str] = []
    protected_characters: list[str] = []
    shareable_characters: list[str] = []
    for binding in character_bindings:
        if not binding.locked:
            continue
        key = _binding_key(binding)
        if key in candidate_lookup:
            if key not in seen_locked_keys:
                valid_locked_voice_keys.append(key)
                seen_locked_keys[key] = binding.canonical_character_id or key
            else:
                duplicate_locked_voice_keys.append(key)
            if binding.canonical_character_id:
                locked_character_ids.append(binding.canonical_character_id)
        else:
            invalid_locked_voice_keys.append(key)
            locked_binding_reasons.append(f"unavailable locked voice: {binding.voice_id or key}")
    if duplicate_locked_voice_keys:
        locked_binding_reasons.append(f"duplicate locked voice reuse: {', '.join(sorted(set(duplicate_locked_voice_keys)))}")

    locked_binding_voice_count = len(set(valid_locked_voice_keys))
    narrator_reserved_voice_count = 0 if narrator_locked_consumed else (min(config.narrator_reserve, eligible_count) if context.narrator_required else 0)
    voices_available_for_new_planning = max(0, eligible_count - narrator_reserved_voice_count - locked_binding_voice_count)

    role_tier_demand = calculate_weighted_demand(context.character_profiles, config)
    role_tier_capacity: dict[str, int] = {tier: 0 for tier in _TIER_ORDER}
    distinct_voice_targets: dict[str, int] = {tier: role_tier_demand[tier].distinct_voice_target for tier in _TIER_ORDER}
    reuse_allowances: dict[str, ReuseAllowance] = {}
    scarcity_penalties: dict[str, int] = {}
    candidate_scarcity_penalties: dict[str, int] = {}
    downgrade_decisions: list[str] = []
    unresolved_capacity_conflicts: list[str] = list(locked_binding_reasons)

    weighted_demand_total = sum(demand.distinct_voice_target for tier, demand in role_tier_demand.items() if tier != "narrator")
    remaining_capacity = voices_available_for_new_planning
    for tier in _TIER_ORDER:
        if tier == "narrator":
            continue
        demand = role_tier_demand[tier]
        desired = max(config.minimum_distinct_voices_by_tier.get(tier, 0), demand.distinct_voice_target)
        capacity = min(desired, remaining_capacity)
        role_tier_capacity[tier] = capacity
        remaining_capacity -= capacity
        shortage = max(0, demand.distinct_voice_target - capacity)
        demand = _replace_tier_demand(demand, distinct_voice_target=desired, capacity_reserved=capacity, reuse_allowance=shortage)
        role_tier_demand[tier] = demand
        reuse_allowances[tier] = build_reuse_policy(tier, demand, scarcity_level="none", config=config)
        scarcity_penalties[tier] = shortage * config.reuse_penalty_scale
        if shortage > 0:
            downgrade_decisions.append(f"{tier}: lower tiers absorb reuse first before higher-priority tiers are displaced")
            if tier in {"primary or lead characters", "major recurring characters"}:
                unresolved_capacity_conflicts.append(f"protected tier shortage for {tier}: need {demand.demand_units}, capacity {capacity}")
            else:
                shareable_characters.extend(demand.character_ids)
        else:
            if tier in {"primary or lead characters", "major recurring characters"}:
                protected_characters.extend(demand.character_ids)
            else:
                shareable_characters.extend(demand.character_ids)

    scarcity_decision = _evaluate_scarcity(
        weighted_demand_total=weighted_demand_total,
        voices_available_for_new_planning=voices_available_for_new_planning,
        narrator_required=context.narrator_required,
        locked_binding_voice_count=locked_binding_voice_count,
        narrator_reserved_voice_count=narrator_reserved_voice_count,
        conflicts=unresolved_capacity_conflicts,
        config=config,
    )

    for tier, demand in role_tier_demand.items():
        reuse_allowances[tier] = build_reuse_policy(tier, demand, scarcity_level=scarcity_decision.level, config=config)
        if demand.reuse_allowance > 0:
            scarcity_penalties[tier] = scarcity_penalties.get(tier, 0) + demand.reuse_allowance * config.reuse_penalty_scale

    for score in eligible_scores:
        candidate_scarcity_penalties[score.registry_key] = _candidate_penalty(score, scarcity_decision.level, config, score.registry_key in valid_locked_voice_keys)

    if narrator_reserved_voice_count:
        protected_tiers = ["narrator"]
    else:
        protected_tiers = []
    protected_tiers.extend([tier for tier in ("primary or lead characters", "major recurring characters") if role_tier_capacity.get(tier, 0) > 0])
    protected_tiers = _dedupe_preserve_order(protected_tiers)

    for tier in _TIER_ORDER:
        if tier in {"supporting recurring characters", "minor speaking characters", "one-scene or one-off speakers", "unresolved speakers"} and role_tier_capacity.get(tier, 0) > 0:
            shareable_characters.extend(role_tier_demand[tier].character_ids)
    protected_characters = _dedupe_preserve_order(protected_characters + locked_character_ids)
    shareable_characters = _dedupe_preserve_order(shareable_characters)

    summary_statistics = {
        "weighted_demand_total": weighted_demand_total,
        "raw_weighted_demand_total": sum(demand.demand_units for demand in role_tier_demand.values()),
        "eligible_candidate_count": eligible_count,
        "locked_binding_voice_keys": len(set(valid_locked_voice_keys)),
        "locked_binding_reuse_count": max(0, len(valid_locked_voice_keys) - len(set(valid_locked_voice_keys))),
        "invalid_locked_voice_count": len(set(invalid_locked_voice_keys)),
        "unavoidable_reuse": sum(max(0, demand.reuse_allowance) for demand in role_tier_demand.values()),
        "scarcity_ratio": scarcity_decision.ratio,
        "scarcity_level": scarcity_decision.level,
        "protected_character_count": len(protected_characters),
        "shareable_character_count": len(shareable_characters),
    }

    return VoiceBudget(
        schema_version=BUDGET_SCHEMA_VERSION,
        narrator_required=context.narrator_required,
        total_eligible_voice_inventory=eligible_count,
        narrator_reserved_voice_count=narrator_reserved_voice_count,
        locked_binding_voice_count=locked_binding_voice_count,
        voices_available_for_new_planning=voices_available_for_new_planning,
        weighted_demand_total=weighted_demand_total,
        role_tier_demand=role_tier_demand,
        role_tier_capacity=role_tier_capacity,
        distinct_voice_targets=distinct_voice_targets,
        reuse_allowances=reuse_allowances,
        scarcity_decision=scarcity_decision,
        scarcity_level=scarcity_decision.level,
        scarcity_penalties=scarcity_penalties,
        candidate_scarcity_penalties=candidate_scarcity_penalties,
        protected_characters=protected_characters,
        protected_tiers=protected_tiers,
        shareable_characters=shareable_characters,
        shareable_tiers=[tier for tier in _TIER_ORDER if tier not in protected_tiers],
        downgrade_decisions=downgrade_decisions,
        unresolved_capacity_conflicts=unresolved_capacity_conflicts,
        summary_statistics=summary_statistics,
    )


def evaluate_scarcity(
    *,
    weighted_demand_total: int,
    voices_available_for_new_planning: int,
    narrator_required: bool,
    locked_binding_voice_count: int,
    narrator_reserved_voice_count: int,
    conflicts: Sequence[str],
    config: BudgetConfig,
) -> ScarcityDecision:
    capacity = max(1, voices_available_for_new_planning)
    ratio = weighted_demand_total / capacity
    triggers: list[str] = []
    effects: list[str] = []
    if narrator_required and narrator_reserved_voice_count == 0 and locked_binding_voice_count == 0 and weighted_demand_total > 0:
        triggers.append("narrator reserve required")
    if locked_binding_voice_count > 0:
        triggers.append(f"{locked_binding_voice_count} locked voice(s) consume capacity")
    if conflicts:
        triggers.extend(conflicts)

    low = config.scarcity_thresholds.get("low", 1.0)
    moderate = config.scarcity_thresholds.get("moderate", 1.2)
    high = config.scarcity_thresholds.get("high", 1.5)
    critical = config.scarcity_thresholds.get("critical", 2.0)

    level = "none"
    if voices_available_for_new_planning == 0 and weighted_demand_total > 0:
        level = "critical"
    elif ratio <= low:
        level = "none"
    elif ratio <= moderate:
        level = "low"
    elif ratio <= high:
        level = "moderate"
    elif ratio <= critical:
        level = "high"
    else:
        level = "critical"

    if any("protected tier shortage" in conflict for conflict in conflicts):
        level = _raise_scarcity(level, "high")
        effects.append("protected tiers retain capacity before lower tiers")
    if any("narrator" in conflict for conflict in conflicts):
        level = _raise_scarcity(level, "critical")
        effects.append("narrator distinctness is preserved when possible")
    if locked_binding_voice_count > 0:
        effects.append("locked bindings consume capacity before new planning")
    if level in {"high", "critical"}:
        effects.append("minor and one-off speakers absorb reuse first")
    elif level == "moderate":
        effects.append("supporting and minor tiers absorb reuse if needed")
    elif level in {"low", "none"}:
        effects.append("distinct voices are preserved for protected tiers")
    return ScarcityDecision(level=level, ratio=ratio, triggers=_dedupe_preserve_order(triggers), effects=_dedupe_preserve_order(effects))


def serialize_voice_budget(budget: VoiceBudget | Mapping[str, Any]) -> str:
    return canonical_json_dumps(budget)


def _candidate_penalty(score: CandidateScore, scarcity_level: str, config: BudgetConfig, locked: bool) -> int:
    if locked:
        return 0
    if scarcity_level == "none":
        return 0
    if scarcity_level == "low":
        return config.reuse_penalty_scale * 25
    if scarcity_level == "moderate":
        return config.reuse_penalty_scale * 50
    if scarcity_level == "high":
        return config.reuse_penalty_scale * 100
    return config.reuse_penalty_scale * 150


def _evaluate_scarcity(
    *,
    weighted_demand_total: int,
    voices_available_for_new_planning: int,
    narrator_required: bool,
    locked_binding_voice_count: int,
    narrator_reserved_voice_count: int,
    conflicts: Sequence[str],
    config: BudgetConfig,
) -> ScarcityDecision:
    return evaluate_scarcity(
        weighted_demand_total=weighted_demand_total,
        voices_available_for_new_planning=voices_available_for_new_planning,
        narrator_required=narrator_required,
        locked_binding_voice_count=locked_binding_voice_count,
        narrator_reserved_voice_count=narrator_reserved_voice_count,
        conflicts=conflicts,
        config=config,
    )


def _raise_scarcity(current: str, minimum: str) -> str:
    order = ["none", "low", "moderate", "high", "critical"]
    return order[max(order.index(current), order.index(minimum))]


def _binding_for_narrator(series_bindings: SeriesBindings | None) -> SeriesVoiceBinding | None:
    if series_bindings is None:
        return None
    return series_bindings.narrator


def _binding_for_characters(series_bindings: SeriesBindings | None) -> list[SeriesVoiceBinding]:
    if series_bindings is None:
        return []
    return list(series_bindings.bindings)


def _binding_key(binding: SeriesVoiceBinding) -> str:
    if binding.provider and binding.provider_voice_id:
        return f"{binding.provider}::{binding.provider_voice_id}"
    if binding.voice_id:
        return binding.voice_id.replace(".", "::", 1)
    return binding.canonical_character_id or binding.target_kind


def _binding_for_context(series_bindings: SeriesBindings | None) -> SeriesVoiceBinding | None:
    return _binding_for_narrator(series_bindings)


def _normalized_text(value: str | None) -> str:
    return (value or "").strip().lower()


def _matches_hint(value: str, hints: set[str]) -> bool:
    if not value:
        return False
    return any(hint in value for hint in hints)


def _dedupe_preserve_order(items: Sequence[Any]) -> list[Any]:
    seen: set[Any] = set()
    result: list[Any] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _reuse_penalty_factor(tier: str, scarcity_level: str) -> int:
    base = {
        "narrator": 0,
        "primary or lead characters": 0,
        "major recurring characters": 1,
        "supporting recurring characters": 1,
        "minor speaking characters": 1,
        "one-scene or one-off speakers": 1,
        "unresolved speakers": 1,
    }.get(tier, 1)
    scarcity_bonus = {"none": 0, "low": 0, "moderate": 1, "high": 2, "critical": 3}.get(scarcity_level, 0)
    return base + scarcity_bonus


def _coerce_config(config: BudgetConfig | Mapping[str, Any] | None) -> BudgetConfig:
    if config is None:
        return BudgetConfig.default()
    if isinstance(config, BudgetConfig):
        return config
    return BudgetConfig.from_mapping(config)


def _replace_tier_demand(demand: TierDemand, **changes: Any) -> TierDemand:
    payload = dataclass_to_dict(demand)
    payload.update(changes)
    return TierDemand(**payload)
