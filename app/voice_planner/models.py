from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any


@dataclass(frozen=True)
class VoiceCapability:
    schema_version: int
    voice_id: str
    provider: str
    provider_voice_id: str
    display_name: str
    gender_presentation: str | None = None
    age_presentation: str | None = None
    archetype_tags: list[str] = field(default_factory=list)
    style_tags: list[str] = field(default_factory=list)
    similarity_cluster: str | None = None
    quality_score: float = 0.0
    latency_estimate_ms: int | None = None
    supported_languages: list[str] = field(default_factory=list)
    sample_rate_hz: int | None = None
    supported_controls: list[str] = field(default_factory=list)
    licensing_information: str | None = None
    availability: str = "available"
    base_priority: int = 0
    notes: str | None = None


@dataclass(frozen=True)
class AssignmentProvenance:
    source: str
    reason: str
    basis: str
    selected_from: list[str] = field(default_factory=list)
    score: float | None = None
    tie_breaker: str | None = None


@dataclass(frozen=True)
class VoiceAssignment:
    voice_id: str | None
    provider: str | None
    provider_voice_id: str | None
    locked: bool = False
    source: str = "automatic"
    confidence: float | None = None
    unavailable_reason: str | None = None
    edited_at: str | None = None
    edited_by: str | None = None
    notes: str | None = None
    generated: bool = True
    provenance: AssignmentProvenance | None = None


@dataclass(frozen=True)
class NarratorPlan:
    assignment: VoiceAssignment
    rationale: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class CharacterPlan:
    canonical_character_id: str
    canonical_name: str
    role: str | None = None
    prominence: str | None = None
    speaking_frequency: int | None = None
    first_appearance: int | None = None
    likely_recurrence: bool | None = None
    age_bucket: str | None = None
    gender_presentation: str | None = None
    species_or_archetype: str | None = None
    scene_relationships: list[dict[str, Any]] = field(default_factory=list)
    unresolved_metadata: dict[str, Any] = field(default_factory=dict)
    assignment: VoiceAssignment | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ScarcityEvent:
    priority_tier: str
    requested_role: str
    fallback_tier: str | None = None
    blocked_reason: str | None = None
    resolved_by: str | None = None
    impact: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class SceneConflict:
    scene_id: str
    character_a: str
    character_b: str
    conflict_type: str
    penalty: float = 0.0
    resolution: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class PlanningReport:
    schema_version: int
    book_id: str
    series_id: str
    plan_hash: str
    generated_at: str
    narrator_choice: dict[str, Any]
    reused_bindings: list[dict[str, Any]] = field(default_factory=list)
    new_bindings: list[dict[str, Any]] = field(default_factory=list)
    manual_overrides: list[dict[str, Any]] = field(default_factory=list)
    locked_assignments: list[dict[str, Any]] = field(default_factory=list)
    deferred_characters: list[dict[str, Any]] = field(default_factory=list)
    unavailable_voices: list[dict[str, Any]] = field(default_factory=list)
    scarcity_events: list[dict[str, Any]] = field(default_factory=list)
    similarity_conflicts: list[dict[str, Any]] = field(default_factory=list)
    scene_conflicts: list[dict[str, Any]] = field(default_factory=list)
    fallback_tiers_used: list[str] = field(default_factory=list)
    scoring_summaries: list[dict[str, Any]] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    final_statistics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoicePlan:
    schema_version: int
    planner_version: str
    book_id: str
    series_id: str
    source_analysis_hash: str
    source_analysis_path: str
    narrator: NarratorPlan
    characters: list[CharacterPlan] = field(default_factory=list)
    conflicts: list[SceneConflict] = field(default_factory=list)
    scarcity_events: list[ScarcityEvent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)
    generated_at: str | None = None
    generated_by: str | None = None
    source_voice_registry_hash: str | None = None
    source_series_bindings_hash: str | None = None
    notes: str | None = None
    user_editable_notes: list[str] = field(default_factory=list)


def dataclass_to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        result: dict[str, Any] = {}
        for f in fields(obj):
            value = getattr(obj, f.name)
            result[f.name] = dataclass_to_dict(value)
        return result
    if isinstance(obj, list):
        return [dataclass_to_dict(item) for item in obj]
    if isinstance(obj, tuple):
        return [dataclass_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {str(key): dataclass_to_dict(value) for key, value in obj.items()}
    return obj
