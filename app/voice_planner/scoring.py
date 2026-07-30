from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Sequence

from .bindings import binding_precedence, get_character_binding, get_narrator_binding
from .models import CharacterProfile, SeriesBindings, SeriesVoiceBinding, VoiceCapability, dataclass_to_dict
from .registry import is_voice_selectable, voice_registry_key
from .schema import canonical_json_dumps

SCORING_SCHEMA_VERSION = 1

_NARRATOR_TAGS = {"narrator", "narration", "lead", "audiobook", "story"}
_ROLE_TAGS = {
    "protagonist": {"lead", "protagonist"},
    "supporting": {"supporting", "ensemble"},
    "mentor": {"mentor", "wise", "elder"},
    "antagonist": {"villain", "antagonist"},
    "companion": {"supporting", "companion"},
}
_AGE_ALIASES = {
    "teen": "teen",
    "teenager": "teen",
    "adolescent": "teen",
    "young adult": "adult",
    "young-adult": "adult",
    "adult": "adult",
    "child": "child",
    "kid": "child",
    "young": "child",
    "senior": "senior",
    "elderly": "senior",
}
_GENDER_ALIASES = {
    "male": "male",
    "man": "male",
    "female": "female",
    "woman": "female",
    "nonbinary": "nonbinary",
    "non-binary": "nonbinary",
    "neutral": "neutral",
    "androgynous": "neutral",
}


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    points: int
    reason: str | None = None
    category: str = "bonus"


@dataclass(frozen=True)
class CandidateScore:
    provider: str
    provider_voice_id: str
    voice_id: str
    registry_key: str
    total_score: int
    eligible: bool
    eligibility_status: str
    ineligibility_reasons: list[str] = field(default_factory=list)
    score_components: list[ScoreComponent] = field(default_factory=list)
    bonuses: list[ScoreComponent] = field(default_factory=list)
    penalties: list[ScoreComponent] = field(default_factory=list)
    tie_break_metadata: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    binding_precedence: str = "no binding"
    binding_precedence_rank: int = 4
    scene_separation_score: int = 0
    registry_base_priority: int = 0
    registry_quality_points: int = 0


@dataclass(frozen=True)
class ScoreContext:
    role: str
    character_profile: CharacterProfile | None = None
    series_bindings: SeriesBindings | None = None
    used_voices: tuple[VoiceCapability | Mapping[str, Any], ...] = ()
    required_languages: tuple[str, ...] = ()
    required_controls: tuple[str, ...] = ()
    required_provider: str | None = None
    excluded_registry_keys: tuple[str, ...] = ()
    scarcity_penalty_units: int = 0
    explicit_voice_key: str | None = None


@dataclass(frozen=True)
class ScoringConfig:
    schema_version: int = SCORING_SCHEMA_VERSION
    continuity_bonus: int = 1200
    manual_override_bonus: int = 1500
    locked_binding_bonus: int = 800
    inherited_binding_bonus: int = 500
    narrator_suitability_bonus: int = 700
    archetype_match_bonus: int = 450
    age_match_bonus: int = 300
    gender_match_bonus: int = 300
    species_match_bonus: int = 350
    prominence_match_bonus: int = 250
    quality_weight: int = 1000
    base_priority_weight: int = 10
    language_match_bonus: int = 600
    capability_match_bonus: int = 250
    scene_separation_bonus: int = 200
    similarity_cluster_penalty: int = 400
    reuse_penalty: int = 300
    scarcity_penalty_scale: int = 1
    unknown_metadata_behavior: str = "neutral"

    @classmethod
    def default(cls) -> "ScoringConfig":
        return cls()

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> "ScoringConfig":
        if mapping is None:
            return cls.default()
        errors = validate_scoring_config(mapping)
        if errors:
            raise ScoringError("; ".join(errors))
        payload = {field.name: mapping.get(field.name, getattr(cls, field.name)) for field in cls.__dataclass_fields__.values()}
        return cls(**payload)


class ScoringError(ValueError):
    pass


def validate_scoring_config(mapping: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(mapping, Mapping):
        return ["scoring config must be a mapping"]
    int_fields = {
        "schema_version",
        "continuity_bonus",
        "manual_override_bonus",
        "locked_binding_bonus",
        "inherited_binding_bonus",
        "narrator_suitability_bonus",
        "archetype_match_bonus",
        "age_match_bonus",
        "gender_match_bonus",
        "species_match_bonus",
        "prominence_match_bonus",
        "quality_weight",
        "base_priority_weight",
        "language_match_bonus",
        "capability_match_bonus",
        "scene_separation_bonus",
        "similarity_cluster_penalty",
        "reuse_penalty",
        "scarcity_penalty_scale",
    }
    for key in int_fields:
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, int):
            errors.append(f"scoring config {key} must be an integer")
        elif value < 0:
            errors.append(f"scoring config {key} must be greater than or equal to zero")
    behavior = mapping.get("unknown_metadata_behavior", "neutral")
    if not isinstance(behavior, str) or behavior not in {"neutral", "reduced_confidence"}:
        errors.append("scoring config unknown_metadata_behavior must be 'neutral' or 'reduced_confidence'")
    return errors


def score_voice_candidate(candidate: VoiceCapability | Mapping[str, Any], context: ScoreContext, config: ScoringConfig | Mapping[str, Any] | None = None) -> CandidateScore:
    scoring_config = _coerce_config(config)
    voice = _coerce_voice(candidate)
    registry_key_tuple = voice_registry_key(voice)
    registry_key = _registry_key_string(registry_key_tuple)
    used_voices = _normalize_used_voices(context.used_voices)
    binding = _binding_for_context(context)
    binding_key = _binding_registry_key(binding)
    binding_precedence_label = _candidate_binding_precedence(voice, binding)
    binding_rank = _binding_precedence_rank(binding_precedence_label)

    hard_reasons = _hard_ineligibility_reasons(voice, context, used_voices, binding, binding_key, registry_key_tuple)

    components: list[ScoreComponent] = []
    if not hard_reasons:
        components.extend(_binding_components(voice, binding, scoring_config))
        components.extend(_role_components(voice, context, scoring_config))
        components.extend(_registry_value_components(voice, scoring_config))
        components.extend(_compatibility_components(voice, context, scoring_config))
        components.extend(_local_penalty_components(voice, used_voices, context, scoring_config))
        components.extend(_scarcity_component(context, scoring_config))

    components = _sorted_components(components)
    total_score = sum(component.points for component in components)
    bonuses = [component for component in components if component.points > 0]
    penalties = [component for component in components if component.points < 0]
    eligible = not hard_reasons
    rationale = _build_rationale(binding_precedence_label, components, hard_reasons)
    tie_break_metadata = {
        "binding_precedence_rank": binding_rank,
        "total_score": total_score,
        "scene_separation_score": sum(component.points for component in components if component.name == "scene_separation"),
        "registry_base_priority": voice.base_priority,
        "registry_quality_points": _quality_points(voice, scoring_config),
        "provider_sort_key": voice.provider,
        "provider_voice_id_sort_key": voice.provider_voice_id,
    }
    return CandidateScore(
        provider=voice.provider,
        provider_voice_id=voice.provider_voice_id,
        voice_id=voice.voice_id,
        registry_key=registry_key,
        total_score=total_score,
        eligible=eligible,
        eligibility_status="eligible" if eligible else "ineligible",
        ineligibility_reasons=hard_reasons,
        score_components=components,
        bonuses=bonuses,
        penalties=penalties,
        tie_break_metadata=tie_break_metadata,
        rationale=rationale,
        binding_precedence=binding_precedence_label,
        binding_precedence_rank=binding_rank,
        scene_separation_score=tie_break_metadata["scene_separation_score"],
        registry_base_priority=voice.base_priority,
        registry_quality_points=tie_break_metadata["registry_quality_points"],
    )


def score_voice_candidates(candidates: Sequence[VoiceCapability | Mapping[str, Any]], context: ScoreContext, config: ScoringConfig | Mapping[str, Any] | None = None) -> list[CandidateScore]:
    scoring_config = _coerce_config(config)
    normalized = [_coerce_voice(candidate) for candidate in candidates]
    deduped = _dedupe_candidates(normalized)
    _assert_locked_binding_present(deduped, context)
    scored = [score_voice_candidate(candidate, context, scoring_config) for candidate in deduped]
    return sorted(scored, key=_ranking_key)


def rank_voice_candidates(candidates: Sequence[VoiceCapability | Mapping[str, Any]], context: ScoreContext, config: ScoringConfig | Mapping[str, Any] | None = None) -> list[CandidateScore]:
    return score_voice_candidates(candidates, context, config)


def serialize_candidate_scores(scores: Sequence[CandidateScore | Mapping[str, Any]]) -> str:
    return canonical_json_dumps(list(scores))


def _coerce_config(config: ScoringConfig | Mapping[str, Any] | None) -> ScoringConfig:
    if config is None:
        return ScoringConfig.default()
    if isinstance(config, ScoringConfig):
        return config
    return ScoringConfig.from_mapping(config)


def _coerce_voice(candidate: VoiceCapability | Mapping[str, Any]) -> VoiceCapability:
    if isinstance(candidate, VoiceCapability):
        return candidate
    if not isinstance(candidate, Mapping):
        raise ScoringError("candidate must be a voice capability mapping or VoiceCapability")
    required = {"voice_id", "provider", "provider_voice_id", "display_name", "availability", "quality_score", "base_priority"}
    missing = sorted(field for field in required if field not in candidate)
    if missing:
        raise ScoringError(f"malformed voice reference missing required fields: {', '.join(missing)}")
    return VoiceCapability(
        schema_version=int(candidate.get("schema_version", 1)),
        voice_id=str(candidate["voice_id"]),
        provider=str(candidate["provider"]),
        provider_voice_id=str(candidate["provider_voice_id"]),
        display_name=str(candidate["display_name"]),
        gender_presentation=_optional_str(candidate.get("gender_presentation")),
        age_presentation=_optional_str(candidate.get("age_presentation")),
        archetype_tags=_string_list(candidate.get("archetype_tags")),
        style_tags=_string_list(candidate.get("style_tags")),
        similarity_cluster=_optional_str(candidate.get("similarity_cluster")),
        quality_score=float(candidate.get("quality_score", 0.0)),
        latency_estimate_ms=_optional_int(candidate.get("latency_estimate_ms")),
        supported_languages=_string_list(candidate.get("supported_languages")),
        sample_rate_hz=_optional_int(candidate.get("sample_rate_hz")),
        supported_controls=_string_list(candidate.get("supported_controls")),
        licensing_information=_optional_str(candidate.get("licensing_information")),
        availability=str(candidate.get("availability", "available")),
        base_priority=int(candidate.get("base_priority", 0)),
        notes=_optional_str(candidate.get("notes")),
    )


def _binding_for_context(context: ScoreContext) -> SeriesVoiceBinding | None:
    if context.series_bindings is None:
        return None
    if context.role == "narrator":
        return get_narrator_binding(context.series_bindings)
    if context.character_profile is None:
        return None
    return get_character_binding(context.series_bindings, context.character_profile.canonical_character_id)


def _binding_registry_key(binding: SeriesVoiceBinding | None) -> tuple[str, str] | None:
    if binding is None or not binding.provider or not binding.provider_voice_id:
        return None
    return str(binding.provider), str(binding.provider_voice_id)


def _candidate_binding_precedence(voice: VoiceCapability, binding: SeriesVoiceBinding | None) -> str:
    if binding is None:
        return "no binding"
    binding_key = _binding_registry_key(binding)
    if binding_key is None or voice_registry_key(voice) != binding_key:
        return "no binding"
    return binding_precedence(binding)


def _registry_key_string(registry_key: tuple[str, str]) -> str:
    return f"{registry_key[0]}::{registry_key[1]}"


def _hard_ineligibility_reasons(
    voice: VoiceCapability,
    context: ScoreContext,
    used_voices: dict[str, VoiceCapability],
    binding: SeriesVoiceBinding | None,
    binding_key: tuple[str, str] | None,
    registry_key: tuple[str, str],
) -> list[str]:
    reasons: list[str] = []
    registry_key_string = _registry_key_string(registry_key)
    if not is_voice_selectable(voice):
        reasons.append("registry entry is unavailable")
    if context.required_provider is not None and voice.provider != context.required_provider:
        reasons.append(f"required provider {context.required_provider!r} not met")
    if context.required_languages:
        supported = {lang.lower() for lang in voice.supported_languages}
        required = {lang.lower() for lang in context.required_languages}
        if not required.issubset(supported):
            reasons.append(f"required language unsupported: {sorted(required - supported)}")
    if context.required_controls:
        supported_controls = {control.lower() for control in voice.supported_controls}
        required_controls = {control.lower() for control in context.required_controls}
        if not required_controls.issubset(supported_controls):
            reasons.append(f"required capability unsupported: {sorted(required_controls - supported_controls)}")
    if registry_key_string in set(context.excluded_registry_keys):
        reasons.append("voice is explicitly excluded")
    if binding is not None and binding.locked:
        if binding_key is None:
            reasons.append("locked binding references malformed voice reference")
        elif registry_key != binding_key:
            reasons.append(f"hard lock requires {_registry_key_string(binding_key)}")
    return reasons


def _binding_components(voice: VoiceCapability, binding: SeriesVoiceBinding | None, config: ScoringConfig) -> list[ScoreComponent]:
    if binding is None:
        return []
    binding_key = _binding_registry_key(binding)
    if binding_key is None or voice_registry_key(voice) != binding_key:
        return []
    precedence = binding_precedence(binding)
    components: list[ScoreComponent] = [
        ScoreComponent("binding_continuity", config.continuity_bonus, f"matches {precedence}", "bonus"),
    ]
    if binding.manual_override:
        components.append(ScoreComponent("manual_override", config.manual_override_bonus, "manual override preserved", "bonus"))
    if binding.locked:
        components.append(ScoreComponent("lock_state", config.locked_binding_bonus, "locked binding preserved", "bonus"))
    elif binding.inherited:
        components.append(ScoreComponent("inheritance", config.inherited_binding_bonus, "inherited series continuity", "bonus"))
    return components


def _role_components(voice: VoiceCapability, context: ScoreContext, config: ScoringConfig) -> list[ScoreComponent]:
    components: list[ScoreComponent] = []
    tags = _normalized_tokens([*voice.archetype_tags, *voice.style_tags])
    if context.role == "narrator":
        if tags & _NARRATOR_TAGS:
            components.append(ScoreComponent("narrator_suitability", config.narrator_suitability_bonus, "narrator-suitable voice metadata", "bonus"))
        return components
    profile = context.character_profile
    if profile is None:
        return components
    if profile.role:
        role_tags = _ROLE_TAGS.get(profile.role.lower(), set())
        if role_tags & tags:
            components.append(ScoreComponent("archetype_fit", config.archetype_match_bonus, f"role {profile.role} matches {sorted(role_tags & tags)}", "bonus"))
    if profile.species_or_archetype:
        normalized_species = _normalized_tokens([profile.species_or_archetype])
        if normalized_species & tags:
            components.append(ScoreComponent("species_fit", config.species_match_bonus, f"species/archetype {profile.species_or_archetype} matches", "bonus"))
    if profile.age_bucket:
        normalized_age = _normalize_age(profile.age_bucket)
        if normalized_age and _normalize_age(voice.age_presentation) == normalized_age:
            components.append(ScoreComponent("age_fit", config.age_match_bonus, f"age bucket {profile.age_bucket} matches", "bonus"))
    if profile.gender_presentation:
        normalized_gender = _normalize_gender(profile.gender_presentation)
        if normalized_gender and _normalize_gender(voice.gender_presentation) == normalized_gender:
            components.append(ScoreComponent("gender_fit", config.gender_match_bonus, f"gender {profile.gender_presentation} matches", "bonus"))
    if profile.prominence:
        prominence_tags = _prominence_tags(profile.prominence)
        if prominence_tags & tags:
            components.append(ScoreComponent("prominence_suitability", config.prominence_match_bonus, f"prominence {profile.prominence} matches", "bonus"))
    return components


def _registry_value_components(voice: VoiceCapability, config: ScoringConfig) -> list[ScoreComponent]:
    return [
        ScoreComponent("registry_quality", _quality_points(voice, config), f"quality_score {voice.quality_score} × {config.quality_weight}", "bonus"),
        ScoreComponent("registry_base_priority", voice.base_priority * config.base_priority_weight, f"base_priority {voice.base_priority} × {config.base_priority_weight}", "bonus"),
    ]


def _compatibility_components(voice: VoiceCapability, context: ScoreContext, config: ScoringConfig) -> list[ScoreComponent]:
    components: list[ScoreComponent] = []
    if context.required_languages:
        components.append(ScoreComponent("language_compatibility", len(context.required_languages) * config.language_match_bonus, f"supports required languages {list(context.required_languages)}", "bonus"))
    if context.required_controls:
        components.append(ScoreComponent("capability_compatibility", len(context.required_controls) * config.capability_match_bonus, f"supports required controls {list(context.required_controls)}", "bonus"))
    return components


def _local_penalty_components(voice: VoiceCapability, used_voices: Mapping[str, VoiceCapability], context: ScoreContext, config: ScoringConfig) -> list[ScoreComponent]:
    components: list[ScoreComponent] = []
    if not used_voices:
        return components
    used_voice_ids = set(used_voices)
    used_clusters = {candidate.similarity_cluster for candidate in used_voices.values() if candidate.similarity_cluster}
    if voice.voice_id in used_voice_ids:
        components.append(ScoreComponent("voice_reuse", -config.reuse_penalty, f"voice {voice.voice_id} already used", "penalty"))
    separation_count = len(used_voice_ids - {voice.voice_id})
    if separation_count:
        components.append(ScoreComponent("scene_separation", separation_count * config.scene_separation_bonus, f"separated from {separation_count} already-used voices", "bonus"))
    if voice.similarity_cluster and voice.similarity_cluster in used_clusters:
        overlap = sum(1 for candidate in used_voices.values() if candidate.similarity_cluster == voice.similarity_cluster)
        components.append(ScoreComponent("similarity_cluster", -(overlap * config.similarity_cluster_penalty), f"similarity cluster {voice.similarity_cluster} already used", "penalty"))
    return components


def _scarcity_component(context: ScoreContext, config: ScoringConfig) -> list[ScoreComponent]:
    if context.scarcity_penalty_units <= 0:
        return []
    return [ScoreComponent("scarcity", -(context.scarcity_penalty_units * config.scarcity_penalty_scale), f"scarcity penalty {context.scarcity_penalty_units}", "penalty")]


def _sorted_components(components: Sequence[ScoreComponent]) -> list[ScoreComponent]:
    order = {"binding_continuity": 0, "manual_override": 1, "lock_state": 2, "inheritance": 3, "narrator_suitability": 4, "archetype_fit": 5, "species_fit": 6, "age_fit": 7, "gender_fit": 8, "prominence_suitability": 9, "registry_quality": 10, "registry_base_priority": 11, "language_compatibility": 12, "capability_compatibility": 13, "scene_separation": 14, "similarity_cluster": 15, "voice_reuse": 16, "scarcity": 17}
    return sorted(components, key=lambda component: (order.get(component.name, 99), -component.points if component.points >= 0 else abs(component.points), component.reason or ""))


def _build_rationale(binding_precedence_label: str, components: Sequence[ScoreComponent], hard_reasons: Sequence[str]) -> str:
    if hard_reasons:
        return "; ".join([binding_precedence_label, *hard_reasons])
    parts = [binding_precedence_label]
    for component in components[:4]:
        sign = "+" if component.points >= 0 else ""
        parts.append(f"{sign}{component.points} {component.name}")
    return "; ".join(parts)


def _ranking_key(candidate: CandidateScore) -> tuple[Any, ...]:
    return (
        0 if candidate.eligible else 1,
        candidate.binding_precedence_rank,
        -candidate.total_score,
        -candidate.scene_separation_score,
        -candidate.registry_base_priority,
        -candidate.registry_quality_points,
        candidate.provider,
        candidate.provider_voice_id,
    )


def _dedupe_candidates(candidates: Sequence[VoiceCapability]) -> list[VoiceCapability]:
    seen: set[tuple[str, str]] = set()
    deduped: list[VoiceCapability] = []
    for candidate in sorted(candidates, key=lambda item: voice_registry_key(item)):
        key = (candidate.provider, candidate.provider_voice_id)
        if key in seen:
            raise ScoringError(f"duplicate candidate registry key: {candidate.provider}::{candidate.provider_voice_id}")
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _assert_locked_binding_present(candidates: Sequence[VoiceCapability], context: ScoreContext) -> None:
    binding = _binding_for_context(context)
    if binding is None or not binding.locked or not binding.provider or not binding.provider_voice_id:
        return
    binding_key = (binding.provider, binding.provider_voice_id)
    candidate_keys = {(candidate.provider, candidate.provider_voice_id) for candidate in candidates}
    if binding_key not in candidate_keys:
        raise ScoringError(f"locked binding references unavailable or missing voice {binding.provider}::{binding.provider_voice_id}")


def _binding_precedence_rank(label: str) -> int:
    return {
        "locked manual override": 0,
        "unlocked manual override": 1,
        "locked inherited series binding": 2,
        "inherited series binding": 3,
        "no binding": 4,
    }.get(label, 4)


def _normalize_used_voices(used_voices: Sequence[VoiceCapability | Mapping[str, Any]]) -> dict[str, VoiceCapability]:
    normalized: dict[str, VoiceCapability] = {}
    for voice in used_voices:
        coerced = _coerce_voice(voice)
        normalized[coerced.voice_id] = coerced
    return normalized


def _quality_points(voice: VoiceCapability, config: ScoringConfig) -> int:
    return int((Decimal(str(voice.quality_score)) * Decimal(config.quality_weight)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ScoringError("voice candidate sequence fields must be sequences")
    return [str(item) for item in value]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _normalized_tokens(values: Iterable[str]) -> set[str]:
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _normalize_age(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("_", "-")
    return _AGE_ALIASES.get(normalized, normalized)


def _normalize_gender(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("_", "-")
    return _GENDER_ALIASES.get(normalized, normalized)


def _prominence_tags(value: str) -> set[str]:
    normalized = value.strip().lower()
    tags: set[str] = set()
    if "major recurring" in normalized or "protagonist" in normalized:
        tags.update({"lead", "protagonist"})
    if "major supporting" in normalized or "supporting" in normalized:
        tags.update({"supporting", "ensemble"})
    if "mentor" in normalized:
        tags.update({"mentor", "wise"})
    return tags


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ScoringError("candidate sequence fields must be sequences")
    return list(value)
