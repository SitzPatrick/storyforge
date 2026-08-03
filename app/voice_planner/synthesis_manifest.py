from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field, replace
from enum import Enum
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Iterable, Mapping, Sequence

from .editable_plan import EditableAssignment, EditableVoicePlan, load_editable_voice_plan, resolve_effective_voice_plan
from .models import CharacterPlan, NarratorPlan, VoiceAssignment, VoiceCapability, VoicePlan, dataclass_to_dict
from .registry import is_voice_selectable, voice_registry_key
from .schema import SCHEMA_VERSIONS, SchemaValidationError, canonical_json_dumps, validate_voice_registry

SYNTHESIS_MANIFEST_SCHEMA_VERSION = 1


class SynthesisManifestError(ValueError):
    pass


class ManifestChangeReason(str, Enum):
    TEXT_CHANGED = "text_changed"
    VOICE_CHANGED = "voice_changed"
    CONTROL_CHANGED = "control_changed"
    PRONUNCIATION_CHANGED = "pronunciation_changed"
    SOURCE_IDENTITY_CHANGED = "source_identity_changed"
    SCHEMA_OR_RENDERER_CONTRACT_CHANGED = "schema_or_renderer_contract_changed"


@dataclass(frozen=True)
class ManifestValidationIssue:
    severity: str
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class ManifestValidationReport:
    total_source_segments: int
    total_render_units: int
    narration_units: int
    dialogue_units: int
    skipped_units: int
    blocked_units: int
    unresolved_speakers: int
    unavailable_voices: int
    unsupported_controls: int
    duplicate_ids: int
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    ready_state: str = "blocked"


@dataclass(frozen=True)
class RenderUnit:
    render_unit_id: str
    canonical_segment_id: str
    scene_id: str
    source_order: list[int]
    segment_type: str
    speaker_type: str
    canonical_speaker_id: str | None
    display_speaker_name: str | None
    raw_source_text: str
    synthesis_text: str
    assigned_provider: str | None
    assigned_provider_voice_id: str | None
    registry_key: str | None
    language: str | None
    requested_voice_controls: dict[str, Any] = field(default_factory=dict)
    effective_renderer_controls: dict[str, Any] = field(default_factory=dict)
    pronunciation_notes: str | None = None
    casting_notes: str | None = None
    performance_notes: str | None = None
    pace_intent: str | None = None
    pause_intent: str | None = None
    emphasis_intent: str | None = None
    requested_provider: str | None = None
    requested_provider_voice_id: str | None = None
    source_provenance: dict[str, Any] = field(default_factory=dict)
    source_text_hash: str = ""
    synthesis_input_hash: str = ""
    output_artifact_key: str = ""
    dependencies: list[str] = field(default_factory=list)
    validation_status: str = "ready"
    warnings: list[str] = field(default_factory=list)
    blocked_reason: str | None = None


@dataclass(frozen=True)
class SynthesisManifest:
    schema_version: int
    renderer_contract_version: int
    planner_version: str
    book_id: str
    series_id: str
    source_analysis_hash: str
    generated_plan_hash: str
    user_editable_hash: str
    effective_plan_hash: str
    voice_registry_hash: str
    planner_config_hash: str
    source_artifacts: dict[str, Any]
    render_units: list[RenderUnit] = field(default_factory=list)
    validation_report: ManifestValidationReport = field(default_factory=lambda: ManifestValidationReport(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, [], [], "blocked"))
    manifest_content_hash: str = ""
    created_by: str | None = "storyforge"
    created_at: str | None = None
    source_story_hash: str | None = None


@dataclass(frozen=True)
class ManifestBuildResult:
    manifest: SynthesisManifest
    validation_report: ManifestValidationReport
    manifest_hash: str
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ManifestDiff:
    unchanged_unit_ids: list[str] = field(default_factory=list)
    added_unit_ids: list[str] = field(default_factory=list)
    removed_unit_ids: list[str] = field(default_factory=list)
    changed_unit_ids: list[str] = field(default_factory=list)
    changed_unit_reasons: dict[str, list[ManifestChangeReason]] = field(default_factory=dict)


_ALLOWED_UNRESOLVED_POLICIES = {"reject", "block", "omit", "fallback"}


def build_synthesis_manifest(
    normalized_story: Mapping[str, Any],
    voice_plan: EditableVoicePlan | VoicePlan | Mapping[str, Any],
    voice_registry: Mapping[str, Any],
    planner_config: Mapping[str, Any] | Any,
    *,
    unresolved_speaker_policy: str | None = None,
    unresolved_fallback_voice: Mapping[str, Any] | None = None,
    source_artifacts: Mapping[str, Any] | None = None,
    bindings: Mapping[str, Any] | None = None,
    score_results: Mapping[str, Any] | None = None,
    budget_result: Mapping[str, Any] | None = None,
    conflict_result: Mapping[str, Any] | None = None,
    created_by: str | None = "storyforge",
    created_at: str | None = None,
) -> ManifestBuildResult:
    story = _deepcopy_mapping(normalized_story)
    registry = _deepcopy_mapping(voice_registry)
    config = _deepcopy_mapping(planner_config)
    bindings_copy = _deepcopy_mapping(bindings) if bindings is not None else None
    score_results_copy = _deepcopy_mapping(score_results) if score_results is not None else None
    budget_result_copy = _deepcopy_mapping(budget_result) if budget_result is not None else None
    conflict_result_copy = _deepcopy_mapping(conflict_result) if conflict_result is not None else None

    _validate_registry_immutability_inputs(registry)
    _validate_story_inputs(story)

    policy = _resolve_unresolved_policy(config, unresolved_speaker_policy)
    if policy not in _ALLOWED_UNRESOLVED_POLICIES:
        raise SynthesisManifestError(f"unsupported unresolved speaker policy: {policy!r}")

    if isinstance(voice_plan, EditableVoicePlan):
        editable = voice_plan
    else:
        editable = load_editable_voice_plan(voice_plan, registry=registry)
    effective_plan = resolve_effective_voice_plan(editable)
    voice_lookup = _voice_lookup(registry)
    settings = _coerce_config(config)
    source_artifacts_payload = _normalize_source_artifacts(source_artifacts or story.get("source_artifacts") or {})

    segment_records = _collect_segments(story)
    alias_map, canonical_name_map = _build_character_maps(story, editable)
    render_units: list[RenderUnit] = []
    validation_warnings: list[str] = []
    validation_errors: list[str] = []
    seen_ids: set[str] = set()
    unresolved_speakers = 0
    unavailable_voices = 0
    unsupported_controls = 0
    blocked_units = 0
    skipped_units = 0
    narration_units = 0
    dialogue_units = 0

    ordered_segments = sorted(segment_records, key=_segment_sort_key)
    previous_unit_id: str | None = None
    for segment_index, segment in enumerate(ordered_segments):
        unit = _build_render_unit(
            segment=segment,
            segment_index=segment_index,
            story=story,
            effective_voice_plan=editable,
            registry=registry,
            voice_lookup=voice_lookup,
            alias_map=alias_map,
            canonical_name_map=canonical_name_map,
            policy=policy,
            unresolved_fallback_voice=unresolved_fallback_voice,
            previous_unit_id=previous_unit_id,
            renderer_contract_version=settings["renderer_contract_version"],
        )
        if unit.validation_status == "skipped":
            skipped_units += 1
            validation_warnings.extend(unit.warnings)
            continue
        if unit.validation_status == "blocked":
            blocked_units += 1
        if unit.speaker_type == "narrator":
            narration_units += 1
        elif unit.segment_type == "dialogue":
            dialogue_units += 1
        else:
            dialogue_units += 0
        if unit.canonical_speaker_id is None and unit.segment_type != "narration":
            unresolved_speakers += 1
        if unit.blocked_reason and "voice" in unit.blocked_reason:
            unavailable_voices += 1
        if unit.warnings:
            validation_warnings.extend(unit.warnings)
            for warning in unit.warnings:
                if "control" in warning.lower():
                    unsupported_controls += 1
        if unit.canonical_segment_id in seen_ids:
            raise SynthesisManifestError(f"duplicate render-unit ID: {unit.canonical_segment_id}")
        seen_ids.add(unit.canonical_segment_id)
        render_units.append(unit)
        previous_unit_id = unit.render_unit_id

    if any(unit.validation_status == "blocked" for unit in render_units):
        ready_state = "blocked"
    elif validation_warnings:
        ready_state = "ready-with-warnings"
    else:
        ready_state = "ready"

    report = ManifestValidationReport(
        total_source_segments=len(ordered_segments),
        total_render_units=len(render_units),
        narration_units=narration_units,
        dialogue_units=dialogue_units,
        skipped_units=skipped_units,
        blocked_units=blocked_units,
        unresolved_speakers=unresolved_speakers,
        unavailable_voices=unavailable_voices,
        unsupported_controls=unsupported_controls,
        duplicate_ids=0,
        warnings=validation_warnings,
        errors=validation_errors,
        ready_state=ready_state,
    )

    manifest = SynthesisManifest(
        schema_version=SYNTHESIS_MANIFEST_SCHEMA_VERSION,
        renderer_contract_version=settings["renderer_contract_version"],
        planner_version=editable.generated_plan.planner_version,
        book_id=editable.book_id,
        series_id=editable.series_id,
        source_analysis_hash=editable.source_analysis_hash,
        generated_plan_hash=_hash_generated_plan(editable.generated_plan),
        user_editable_hash=editable.user_editable_hash,
        effective_plan_hash=editable.effective_plan_hash,
        voice_registry_hash=_hash_canonical(registry),
        planner_config_hash=_hash_canonical(config),
        source_artifacts=source_artifacts_payload,
        render_units=render_units,
        validation_report=report,
        created_by=created_by,
        created_at=created_at,
        source_story_hash=_hash_canonical(_canonicalize_story_for_hash(story)),
    )
    manifest = replace(manifest, manifest_content_hash=_hash_manifest_content(manifest))
    validation_report = validate_synthesis_manifest(manifest)
    manifest = replace(manifest, validation_report=validation_report)
    if policy == "reject" and (validation_report.blocked_units > 0 or validation_report.unresolved_speakers > 0):
        raise SynthesisManifestError("synthesis manifest contains unresolved or blocked render units")
    return ManifestBuildResult(manifest=manifest, validation_report=validation_report, manifest_hash=manifest.manifest_content_hash, warnings=list(validation_report.warnings), errors=list(validation_report.errors))


def validate_synthesis_manifest(data: SynthesisManifest | Mapping[str, Any]) -> ManifestValidationReport:
    payload = dataclass_to_dict(data) if isinstance(data, SynthesisManifest) else _deepcopy_mapping(data)
    _validate_manifest_payload(payload)
    manifest_hash = payload.get("manifest_content_hash")
    computed_hash = _hash_manifest_content(payload)
    if manifest_hash != computed_hash:
        raise SynthesisManifestError("manifest_content_hash does not match canonical payload")
    report = payload.get("validation_report")
    if not isinstance(report, Mapping):
        raise SynthesisManifestError("validation_report must be a mapping")
    return ManifestValidationReport(
        total_source_segments=int(report.get("total_source_segments", 0)),
        total_render_units=int(report.get("total_render_units", 0)),
        narration_units=int(report.get("narration_units", 0)),
        dialogue_units=int(report.get("dialogue_units", 0)),
        skipped_units=int(report.get("skipped_units", 0)),
        blocked_units=int(report.get("blocked_units", 0)),
        unresolved_speakers=int(report.get("unresolved_speakers", 0)),
        unavailable_voices=int(report.get("unavailable_voices", 0)),
        unsupported_controls=int(report.get("unsupported_controls", 0)),
        duplicate_ids=int(report.get("duplicate_ids", 0)),
        warnings=[str(item) for item in report.get("warnings", [])],
        errors=[str(item) for item in report.get("errors", [])],
        ready_state=str(report.get("ready_state", "blocked")),
    )


def serialize_synthesis_manifest(manifest: SynthesisManifest | Mapping[str, Any]) -> str:
    return canonical_json_dumps(manifest if isinstance(manifest, SynthesisManifest) else _coerce_manifest(manifest))


def load_synthesis_manifest(source: str | Path | Mapping[str, Any]) -> SynthesisManifest:
    if isinstance(source, (str, Path)):
        path = Path(source)
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = _deepcopy_mapping(source)
    manifest = _coerce_manifest(payload)
    validation_report = validate_synthesis_manifest(manifest)
    return replace(manifest, validation_report=validation_report)


def save_synthesis_manifest_atomic(path: str | Path, manifest: SynthesisManifest | Mapping[str, Any]) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_synthesis_manifest(manifest) + "\n"
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=out_path.parent, prefix=f".{out_path.name}.", suffix=".tmp", delete=False) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, out_path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def compare_synthesis_manifests(previous: SynthesisManifest | Mapping[str, Any], current: SynthesisManifest | Mapping[str, Any]) -> ManifestDiff:
    prev_manifest = _coerce_manifest(previous)
    current_manifest = _coerce_manifest(current)
    prev_units = {unit.render_unit_id: unit for unit in prev_manifest.render_units}
    current_units = {unit.render_unit_id: unit for unit in current_manifest.render_units}
    prev_ids = set(prev_units)
    current_ids = set(current_units)
    unchanged = sorted(prev_ids & current_ids)
    added = sorted(current_ids - prev_ids)
    removed = sorted(prev_ids - current_ids)
    changed: list[str] = []
    reasons: dict[str, list[ManifestChangeReason]] = {}
    for unit_id in sorted(prev_ids & current_ids):
        prev_unit = prev_units[unit_id]
        curr_unit = current_units[unit_id]
        unit_reasons = _compare_units(prev_unit, curr_unit, prev_manifest, current_manifest)
        if unit_reasons:
            changed.append(unit_id)
            reasons[unit_id] = unit_reasons
    return ManifestDiff(unchanged_unit_ids=unchanged, added_unit_ids=added, removed_unit_ids=removed, changed_unit_ids=changed, changed_unit_reasons=reasons)


def _build_render_unit(
    *,
    segment: Mapping[str, Any],
    segment_index: int,
    story: Mapping[str, Any],
    effective_voice_plan: EditableVoicePlan,
    registry: Mapping[str, Any],
    voice_lookup: dict[tuple[str, str], Mapping[str, Any]],
    alias_map: dict[str, str],
    canonical_name_map: dict[str, str],
    policy: str,
    unresolved_fallback_voice: Mapping[str, Any] | None,
    previous_unit_id: str | None,
    renderer_contract_version: int,
) -> RenderUnit:
    segment_type = str(segment.get("segment_type") or segment.get("type") or "").strip().lower()
    if segment_type not in {"narration", "dialogue", "other", "other_speech", "speech"}:
        segment_type = "other"
    scene_id = _safe_token(str(segment.get("scene_id") or segment.get("scene") or _infer_scene_id(story, segment)))
    chapter = int(segment.get("chapter") or segment.get("chapter_number") or 0)
    source_order = _segment_source_order(segment, segment_index)
    source_text = str(segment.get("source_text") or segment.get("text") or segment.get("quoted_text") or "")
    synthesis_text = _normalize_synthesis_text(str(segment.get("synthesis_text") or source_text))
    raw_source_text = source_text
    source_reference = _normalize_source_reference(segment.get("source_reference"), chapter, segment)
    source_identity = _source_identity(segment, story, scene_id)
    canonical_segment_id = _canonical_segment_id(segment, source_identity)
    source_text_hash = _hash_source_text(source_identity, source_text)
    language = _story_language(story)
    controls = _normalize_controls(segment.get("controls"))
    pronunciation_notes = _optional_str(segment.get("pronunciation_notes"))
    casting_notes = _optional_str(segment.get("casting_notes"))
    performance_notes = _optional_str(segment.get("performance_notes"))
    pace_intent = _optional_str(segment.get("pace_intent"))
    pause_intent = _optional_str(segment.get("pause_intent"))
    emphasis_intent = _optional_str(segment.get("emphasis_intent"))
    speaker_label = _optional_str(segment.get("speaker") or segment.get("speaker_name") or segment.get("display_speaker_name"))
    requested_provider = None
    requested_provider_voice_id = None
    canonical_speaker_id: str | None = None
    display_speaker_name: str | None = None
    speaker_type = "narrator" if segment_type == "narration" else str(segment.get("speaker_type") or ("dialogue" if segment_type == "dialogue" else "other")).lower()
    assignment: EditableAssignment | None = None
    if segment_type == "narration":
        assignment = effective_voice_plan.narrator
        canonical_speaker_id = "narrator"
        display_speaker_name = "Narrator"
        speaker_type = "narrator"
    else:
        resolved = _resolve_speaker(segment, alias_map, canonical_name_map, effective_voice_plan)
        canonical_speaker_id = resolved[0]
        display_speaker_name = resolved[1]
        assignment = _assignment_for_speaker(effective_voice_plan, canonical_speaker_id)
        speaker_type = "unresolved" if canonical_speaker_id is None else speaker_type
        requested_provider = assignment.requested_provider if assignment else None
        requested_provider_voice_id = assignment.requested_provider_voice_id if assignment else None
        if assignment is None and policy == "fallback":
            assignment = _fallback_assignment(unresolved_fallback_voice, registry)
        if assignment is None and policy == "block":
            return _blocked_unit(effective_voice_plan, scene_id, canonical_segment_id, segment_type, source_order, canonical_speaker_id, display_speaker_name, raw_source_text, synthesis_text, language, controls, pronunciation_notes, casting_notes, performance_notes, pace_intent, pause_intent, emphasis_intent, requested_provider, requested_provider_voice_id, source_reference, source_text_hash, previous_unit_id, "unresolved speaker blocked by policy")
        if assignment is None and policy == "omit":
            return RenderUnit(
                render_unit_id=_render_unit_id(effective_voice_plan.book_id, scene_id, canonical_segment_id, segment_type, canonical_speaker_id),
                canonical_segment_id=canonical_segment_id,
                scene_id=scene_id,
                source_order=source_order,
                segment_type=segment_type,
                speaker_type=speaker_type,
                canonical_speaker_id=canonical_speaker_id,
                display_speaker_name=display_speaker_name,
                raw_source_text=raw_source_text,
                synthesis_text=synthesis_text,
                assigned_provider=None,
                assigned_provider_voice_id=None,
                registry_key=None,
                language=language,
                requested_voice_controls=controls,
                effective_renderer_controls={},
                pronunciation_notes=pronunciation_notes,
                casting_notes=casting_notes,
                performance_notes=performance_notes,
                pace_intent=pace_intent,
                pause_intent=pause_intent,
                emphasis_intent=emphasis_intent,
                requested_provider=requested_provider,
                requested_provider_voice_id=requested_provider_voice_id,
                source_provenance=source_reference,
                source_text_hash=source_text_hash,
                synthesis_input_hash="",
                output_artifact_key="",
                dependencies=[previous_unit_id] if previous_unit_id else [],
                validation_status="skipped",
                warnings=["unresolved speaker omitted by policy"],
                blocked_reason=None,
            )
        if assignment is None and policy == "reject":
            raise SynthesisManifestError(f"unresolved speaker cannot be synthesized: {speaker_label or canonical_segment_id}")
    resolved_assignment = assignment or effective_voice_plan.narrator
    if resolved_assignment is None:
        raise SynthesisManifestError("no effective assignment available")
    effective_provider = resolved_assignment.effective_assignment.provider if resolved_assignment.effective_assignment else resolved_assignment.generated_assignment.provider
    effective_provider_voice_id = resolved_assignment.effective_assignment.provider_voice_id if resolved_assignment.effective_assignment else resolved_assignment.generated_assignment.provider_voice_id
    if effective_provider is None or effective_provider_voice_id is None:
        if policy == "block":
            return _blocked_unit(effective_voice_plan, scene_id, canonical_segment_id, segment_type, source_order, canonical_speaker_id, display_speaker_name, raw_source_text, synthesis_text, language, controls, pronunciation_notes, casting_notes, performance_notes, pace_intent, pause_intent, emphasis_intent, requested_provider, requested_provider_voice_id, source_reference, source_text_hash, previous_unit_id, "unresolved effective assignment")
        raise SynthesisManifestError("effective assignment lacks a voice")
    source_voice = _voice_entry(registry, effective_provider, effective_provider_voice_id)
    voice_entry = source_voice or voice_lookup.get((effective_provider, effective_provider_voice_id))
    if voice_entry is None:
        if policy == "block" or policy == "fallback":
            return _blocked_unit(effective_voice_plan, scene_id, canonical_segment_id, segment_type, source_order, canonical_speaker_id, display_speaker_name, raw_source_text, synthesis_text, language, controls, pronunciation_notes, casting_notes, performance_notes, pace_intent, pause_intent, emphasis_intent, requested_provider, requested_provider_voice_id, source_reference, source_text_hash, previous_unit_id, "voice missing from registry")
        raise SynthesisManifestError(f"effective voice not found in registry: {effective_provider}::{effective_provider_voice_id}")
    if not is_voice_selectable(voice_entry):
        return _blocked_unit(effective_voice_plan, scene_id, canonical_segment_id, segment_type, source_order, canonical_speaker_id, display_speaker_name, raw_source_text, synthesis_text, language, controls, pronunciation_notes, casting_notes, performance_notes, pace_intent, pause_intent, emphasis_intent, requested_provider, requested_provider_voice_id, source_reference, source_text_hash, previous_unit_id, "voice unavailable")
    available_controls = set(str(item) for item in voice_entry.get("supported_controls", []) if item is not None)
    effective_controls: dict[str, Any] = {}
    warnings: list[str] = []
    for key, value in controls.items():
        if key not in available_controls:
            warnings.append(f"unsupported control excluded: {key}")
            continue
        if not _control_value_valid(key, value):
            warnings.append(f"invalid control value excluded: {key}")
            continue
        effective_controls[key] = value
    if language and not _voice_supports_language(voice_entry, language):
        return _blocked_unit(effective_voice_plan, scene_id, canonical_segment_id, segment_type, source_order, canonical_speaker_id, display_speaker_name, raw_source_text, synthesis_text, language, controls, pronunciation_notes, casting_notes, performance_notes, pace_intent, pause_intent, emphasis_intent, requested_provider, requested_provider_voice_id, source_reference, source_text_hash, previous_unit_id, "language unsupported")
    if warnings and policy != "reject":
        pass
    synthesis_input_hash = _hash_synthesis_input(
        canonical_segment_id=canonical_segment_id,
        scene_id=scene_id,
        segment_type=segment_type,
        canonical_speaker_id=canonical_speaker_id,
        source_text=synthesis_text,
        provider=effective_provider,
        provider_voice_id=effective_provider_voice_id,
        language=language,
        renderer_controls=effective_controls,
        pronunciation_notes=pronunciation_notes,
        performance_notes=performance_notes,
        pace_intent=pace_intent,
        pause_intent=pause_intent,
        emphasis_intent=emphasis_intent,
        renderer_contract_version=renderer_contract_version,
    )
    return RenderUnit(
        render_unit_id=_render_unit_id(effective_voice_plan.book_id, scene_id, canonical_segment_id, segment_type, canonical_speaker_id),
        canonical_segment_id=canonical_segment_id,
        scene_id=scene_id,
        source_order=source_order,
        segment_type=segment_type,
        speaker_type=speaker_type,
        canonical_speaker_id=canonical_speaker_id,
        display_speaker_name=display_speaker_name,
        raw_source_text=raw_source_text,
        synthesis_text=synthesis_text,
        assigned_provider=effective_provider,
        assigned_provider_voice_id=effective_provider_voice_id,
        registry_key=f"{voice_entry['provider']}:{voice_entry['provider_voice_id']}",
        language=language,
        requested_voice_controls=dict(controls),
        effective_renderer_controls=effective_controls,
        pronunciation_notes=pronunciation_notes,
        casting_notes=casting_notes,
        performance_notes=performance_notes,
        pace_intent=pace_intent,
        pause_intent=pause_intent,
        emphasis_intent=emphasis_intent,
        requested_provider=requested_provider,
        requested_provider_voice_id=requested_provider_voice_id,
        source_provenance=source_reference,
        source_text_hash=source_text_hash,
        synthesis_input_hash=synthesis_input_hash,
        output_artifact_key=_artifact_key(scene_id, _render_unit_id(effective_voice_plan.book_id, scene_id, canonical_segment_id, segment_type, canonical_speaker_id)),
        dependencies=[previous_unit_id] if previous_unit_id else [],
        validation_status="ready",
        warnings=warnings,
        blocked_reason=None,
    )


def _blocked_unit(
    effective_voice_plan: EditableVoicePlan,
    scene_id: str,
    canonical_segment_id: str,
    segment_type: str,
    source_order: list[int],
    canonical_speaker_id: str | None,
    display_speaker_name: str | None,
    raw_source_text: str,
    synthesis_text: str,
    language: str | None,
    controls: dict[str, Any],
    pronunciation_notes: str | None,
    casting_notes: str | None,
    performance_notes: str | None,
    pace_intent: str | None,
    pause_intent: str | None,
    emphasis_intent: str | None,
    requested_provider: str | None,
    requested_provider_voice_id: str | None,
    source_reference: dict[str, Any],
    source_text_hash: str,
    previous_unit_id: str | None,
    reason: str,
) -> RenderUnit:
    render_unit_id = _render_unit_id(effective_voice_plan.book_id, scene_id, canonical_segment_id, segment_type, canonical_speaker_id)
    return RenderUnit(
        render_unit_id=render_unit_id,
        canonical_segment_id=canonical_segment_id,
        scene_id=scene_id,
        source_order=source_order,
        segment_type=segment_type,
        speaker_type="unresolved" if canonical_speaker_id is None and reason.startswith("unresolved") else "blocked",
        canonical_speaker_id=canonical_speaker_id,
        display_speaker_name=display_speaker_name,
        raw_source_text=raw_source_text,
        synthesis_text=synthesis_text,
        assigned_provider=requested_provider,
        assigned_provider_voice_id=requested_provider_voice_id,
        registry_key=None,
        language=language,
        requested_voice_controls=dict(controls),
        effective_renderer_controls={},
        pronunciation_notes=pronunciation_notes,
        casting_notes=casting_notes,
        performance_notes=performance_notes,
        pace_intent=pace_intent,
        pause_intent=pause_intent,
        emphasis_intent=emphasis_intent,
        requested_provider=requested_provider,
        requested_provider_voice_id=requested_provider_voice_id,
        source_provenance=source_reference,
        source_text_hash=source_text_hash,
        synthesis_input_hash="",
        output_artifact_key=_artifact_key(scene_id, render_unit_id),
        dependencies=[previous_unit_id] if previous_unit_id else [],
        validation_status="blocked",
        warnings=[reason],
        blocked_reason=reason,
    )


def _compare_units(previous: RenderUnit, current: RenderUnit, prev_manifest: SynthesisManifest, current_manifest: SynthesisManifest) -> list[ManifestChangeReason]:
    reasons: list[ManifestChangeReason] = []
    if prev_manifest.schema_version != current_manifest.schema_version or prev_manifest.renderer_contract_version != current_manifest.renderer_contract_version:
        reasons.append(ManifestChangeReason.SCHEMA_OR_RENDERER_CONTRACT_CHANGED)
        return reasons
    if previous.source_text_hash != current.source_text_hash:
        reasons.append(ManifestChangeReason.TEXT_CHANGED)
    if previous.assigned_provider != current.assigned_provider or previous.assigned_provider_voice_id != current.assigned_provider_voice_id:
        reasons.append(ManifestChangeReason.VOICE_CHANGED)
    if previous.effective_renderer_controls != current.effective_renderer_controls:
        reasons.append(ManifestChangeReason.CONTROL_CHANGED)
    if _pronunciation_payload(previous) != _pronunciation_payload(current):
        reasons.append(ManifestChangeReason.PRONUNCIATION_CHANGED)
    if previous.canonical_segment_id != current.canonical_segment_id or previous.scene_id != current.scene_id or previous.canonical_speaker_id != current.canonical_speaker_id or previous.segment_type != current.segment_type:
        reasons.append(ManifestChangeReason.SOURCE_IDENTITY_CHANGED)
    return reasons


def _coerce_manifest(data: SynthesisManifest | Mapping[str, Any]) -> SynthesisManifest:
    if isinstance(data, SynthesisManifest):
        return data
    payload = _deepcopy_mapping(data)
    _validate_manifest_payload(payload)
    render_units = [_coerce_render_unit(item) for item in payload.get("render_units", [])]
    validation_report = _coerce_validation_report(payload.get("validation_report") or {})
    return SynthesisManifest(
        schema_version=int(payload["schema_version"]),
        renderer_contract_version=int(payload["renderer_contract_version"]),
        planner_version=str(payload["planner_version"]),
        book_id=str(payload["book_id"]),
        series_id=str(payload["series_id"]),
        source_analysis_hash=str(payload["source_analysis_hash"]),
        generated_plan_hash=str(payload["generated_plan_hash"]),
        user_editable_hash=str(payload["user_editable_hash"]),
        effective_plan_hash=str(payload["effective_plan_hash"]),
        voice_registry_hash=str(payload["voice_registry_hash"]),
        planner_config_hash=str(payload["planner_config_hash"]),
        source_artifacts=_normalize_source_artifacts(payload.get("source_artifacts") or {}),
        render_units=render_units,
        validation_report=validation_report,
        manifest_content_hash=str(payload.get("manifest_content_hash", "")),
        created_by=_optional_str(payload.get("created_by")),
        created_at=_optional_str(payload.get("created_at")),
        source_story_hash=_optional_str(payload.get("source_story_hash")),
    )


def _validate_manifest_payload(payload: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "renderer_contract_version",
        "planner_version",
        "book_id",
        "series_id",
        "source_analysis_hash",
        "generated_plan_hash",
        "user_editable_hash",
        "effective_plan_hash",
        "voice_registry_hash",
        "planner_config_hash",
        "source_artifacts",
        "render_units",
        "validation_report",
        "manifest_content_hash",
    }
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise SynthesisManifestError(f"manifest missing required fields: {', '.join(missing)}")
    if int(payload["schema_version"]) != SYNTHESIS_MANIFEST_SCHEMA_VERSION:
        raise SynthesisManifestError(f"unsupported synthesis manifest schema version: {payload['schema_version']!r}")
    if not isinstance(payload.get("render_units"), Sequence) or isinstance(payload.get("render_units"), (str, bytes)):
        raise SynthesisManifestError("render_units must be a sequence")
    seen_ids = set()
    for idx, unit in enumerate(payload.get("render_units", [])):
        if not isinstance(unit, Mapping):
            raise SynthesisManifestError(f"render_units[{idx}] must be a mapping")
        unit_id = unit.get("render_unit_id")
        if not isinstance(unit_id, str) or not unit_id:
            raise SynthesisManifestError(f"render_units[{idx}] missing render_unit_id")
        if unit_id in seen_ids:
            raise SynthesisManifestError(f"duplicate render-unit ID: {unit_id}")
        seen_ids.add(unit_id)
    source_artifacts = payload.get("source_artifacts")
    if not isinstance(source_artifacts, Mapping):
        raise SynthesisManifestError("source_artifacts must be a mapping")
    _validate_safe_source_artifacts(source_artifacts)


def _coerce_render_unit(data: Mapping[str, Any]) -> RenderUnit:
    return RenderUnit(
        render_unit_id=str(data["render_unit_id"]),
        canonical_segment_id=str(data["canonical_segment_id"]),
        scene_id=str(data["scene_id"]),
        source_order=[int(v) for v in data.get("source_order", []) or []],
        segment_type=str(data["segment_type"]),
        speaker_type=str(data["speaker_type"]),
        canonical_speaker_id=_optional_str(data.get("canonical_speaker_id")),
        display_speaker_name=_optional_str(data.get("display_speaker_name")),
        raw_source_text=str(data.get("raw_source_text", "")),
        synthesis_text=str(data.get("synthesis_text", "")),
        assigned_provider=_optional_str(data.get("assigned_provider")),
        assigned_provider_voice_id=_optional_str(data.get("assigned_provider_voice_id")),
        registry_key=_optional_str(data.get("registry_key")),
        language=_optional_str(data.get("language")),
        requested_voice_controls=dict(data.get("requested_voice_controls") or {}),
        effective_renderer_controls=dict(data.get("effective_renderer_controls") or {}),
        pronunciation_notes=_optional_str(data.get("pronunciation_notes")),
        casting_notes=_optional_str(data.get("casting_notes")),
        performance_notes=_optional_str(data.get("performance_notes")),
        pace_intent=_optional_str(data.get("pace_intent")),
        pause_intent=_optional_str(data.get("pause_intent")),
        emphasis_intent=_optional_str(data.get("emphasis_intent")),
        requested_provider=_optional_str(data.get("requested_provider")),
        requested_provider_voice_id=_optional_str(data.get("requested_provider_voice_id")),
        source_provenance=dict(data.get("source_provenance") or {}),
        source_text_hash=str(data.get("source_text_hash", "")),
        synthesis_input_hash=str(data.get("synthesis_input_hash", "")),
        output_artifact_key=str(data.get("output_artifact_key", "")),
        dependencies=[str(item) for item in data.get("dependencies", []) or []],
        validation_status=str(data.get("validation_status", "ready")),
        warnings=[str(item) for item in data.get("warnings", []) or []],
        blocked_reason=_optional_str(data.get("blocked_reason")),
    )


def _coerce_validation_report(data: Mapping[str, Any]) -> ManifestValidationReport:
    return ManifestValidationReport(
        total_source_segments=int(data.get("total_source_segments", 0)),
        total_render_units=int(data.get("total_render_units", 0)),
        narration_units=int(data.get("narration_units", 0)),
        dialogue_units=int(data.get("dialogue_units", 0)),
        skipped_units=int(data.get("skipped_units", 0)),
        blocked_units=int(data.get("blocked_units", 0)),
        unresolved_speakers=int(data.get("unresolved_speakers", 0)),
        unavailable_voices=int(data.get("unavailable_voices", 0)),
        unsupported_controls=int(data.get("unsupported_controls", 0)),
        duplicate_ids=int(data.get("duplicate_ids", 0)),
        warnings=[str(item) for item in data.get("warnings", []) or []],
        errors=[str(item) for item in data.get("errors", []) or []],
        ready_state=str(data.get("ready_state", "blocked")),
    )


def _coerce_editable_voice_plan(data: Any) -> EditableVoicePlan | None:
    if data is None:
        return None
    if isinstance(data, EditableVoicePlan):
        return data
    if isinstance(data, Mapping) and "generated_plan" in data:
        from .editable_plan import _coerce_editable_plan  # type: ignore

        return _coerce_editable_plan(data)  # pragma: no cover - loaded data path
    return None


def _resolve_unresolved_policy(config: Mapping[str, Any], explicit: str | None) -> str:
    if explicit:
        return explicit
    voice_planner = config.get("voice_planner") if isinstance(config, Mapping) else None
    if isinstance(voice_planner, Mapping):
        policy = voice_planner.get("default_unresolved_speaker_policy")
        if isinstance(policy, str) and policy:
            return policy
    return "reject"


def _coerce_config(config: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(config, Mapping):
        voice_planner = dict(config.get("voice_planner") or {})
        return {
            "renderer_contract_version": int(voice_planner.get("renderer_contract_version", 1)),
            "default_unresolved_speaker_policy": str(voice_planner.get("default_unresolved_speaker_policy", "reject")),
            "manifest_filename": str(voice_planner.get("manifest_filename", "synthesis_manifest.json")),
            "raw": _deepcopy_mapping(config),
        }
    if hasattr(config, "voice_planner"):
        vp = getattr(config, "voice_planner")
        return {
            "renderer_contract_version": int(getattr(vp, "renderer_contract_version", 1)),
            "default_unresolved_speaker_policy": str(getattr(vp, "default_unresolved_speaker_policy", "reject")),
            "manifest_filename": str(getattr(vp, "manifest_filename", "synthesis_manifest.json")),
            "raw": dataclass_to_dict(config),
        }
    raise SynthesisManifestError("unsupported planner_config object")


def _collect_segments(story: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    segments = story.get("segments")
    if isinstance(segments, Sequence) and not isinstance(segments, (str, bytes)):
        return [segment for segment in segments if isinstance(segment, Mapping)]
    collected: list[Mapping[str, Any]] = []
    for item in story.get("narration_paragraphs", []) or []:
        if isinstance(item, Mapping):
            collected.append(dict(item, segment_type="narration", source_text=item.get("text", ""), synthesis_text=item.get("text", ""), source_order=[int(item.get("chapter", 0)), int(item.get("paragraph_index", 0)), 0]))
    for item in story.get("dialogue", []) or []:
        if isinstance(item, Mapping):
            collected.append(dict(item, segment_type="dialogue", source_text=item.get("quoted_text", ""), synthesis_text=item.get("quoted_text", ""), source_order=[int(item.get("chapter", 0)), int(item.get("paragraph_index", 0)), 1]))
    return collected


def _segment_source_order(segment: Mapping[str, Any], segment_index: int) -> list[int]:
    source_order = segment.get("source_order")
    if isinstance(source_order, Sequence) and not isinstance(source_order, (str, bytes)):
        values = [int(v) for v in source_order][:4]
        while len(values) < 4:
            values.append(0)
        return values
    if isinstance(source_order, int):
        return [int(source_order), 0, 0, 0]
    chapter = int(segment.get("chapter") or segment.get("chapter_number") or 0)
    scene_number = int(segment.get("scene_number") or 0)
    paragraph_index = int(segment.get("paragraph_index") or segment.get("paragraph") or 0)
    return [chapter, scene_number, paragraph_index, int(segment_index)]


def _segment_sort_key(segment: Mapping[str, Any]) -> tuple:
    source_order = segment.get("source_order")
    if isinstance(source_order, Sequence) and not isinstance(source_order, (str, bytes)):
        order_tuple = tuple(int(v) for v in source_order)
    elif isinstance(source_order, int):
        order_tuple = (int(source_order), 0, 0, 0)
    else:
        order_tuple = (
            int(segment.get("chapter") or segment.get("chapter_number") or 0),
            int(segment.get("scene_number") or 0),
            int(segment.get("paragraph_index") or segment.get("paragraph") or 0),
            int(segment.get("segment_index") or 0),
        )
    return (*order_tuple, _safe_token(str(segment.get("segment_id") or segment.get("id") or "")))


def _build_character_maps(story: Mapping[str, Any], effective_voice_plan: EditableVoicePlan) -> tuple[dict[str, str], dict[str, str]]:
    alias_map: dict[str, str] = {}
    canonical_name_map: dict[str, str] = {}
    for character in story.get("characters", []) or []:
        if not isinstance(character, Mapping):
            continue
        canonical_id = _optional_str(character.get("canonical_character_id") or character.get("id") or character.get("name"))
        canonical_name = _optional_str(character.get("canonical_name") or character.get("name") or canonical_id)
        if canonical_id:
            canonical_name_map[canonical_id.lower()] = canonical_id
        if canonical_id and canonical_name:
            canonical_name_map[canonical_name.lower()] = canonical_id
        for alias in _normalize_list(character.get("aliases")) + _normalize_list(character.get("source_aliases")):
            alias_map[alias.lower()] = canonical_id or alias
    for assignment in [effective_voice_plan.narrator, *effective_voice_plan.characters]:
        if assignment.canonical_character_id and assignment.canonical_name:
            canonical_name_map[assignment.canonical_character_id.lower()] = assignment.canonical_character_id
            canonical_name_map[assignment.canonical_name.lower()] = assignment.canonical_character_id
    return alias_map, canonical_name_map


def _resolve_speaker(segment: Mapping[str, Any], alias_map: Mapping[str, str], canonical_name_map: Mapping[str, str], effective_voice_plan: EditableVoicePlan) -> tuple[str | None, str | None]:
    canonical_id = _optional_str(segment.get("canonical_character_id") or segment.get("speaker_id"))
    speaker_label = _optional_str(segment.get("speaker") or segment.get("speaker_name") or segment.get("display_speaker_name"))
    if canonical_id:
        return canonical_id, speaker_label or canonical_id
    if speaker_label:
        key = speaker_label.lower()
        if key in canonical_name_map:
            return canonical_name_map[key], speaker_label
        if key in alias_map:
            return alias_map[key], speaker_label
        for assignment in effective_voice_plan.characters:
            if assignment.canonical_name and assignment.canonical_name.lower() == key:
                return assignment.canonical_character_id, assignment.canonical_name
    return None, speaker_label


def _assignment_for_speaker(effective_voice_plan: EditableVoicePlan, canonical_speaker_id: str | None) -> EditableAssignment | None:
    if canonical_speaker_id is None:
        return None
    for assignment in effective_voice_plan.characters:
        if assignment.canonical_character_id == canonical_speaker_id:
            return assignment
    return None


def _fallback_assignment(unresolved_fallback_voice: Mapping[str, Any] | None, registry: Mapping[str, Any]) -> EditableAssignment | None:
    if not unresolved_fallback_voice:
        return None
    provider = _optional_str(unresolved_fallback_voice.get("provider"))
    provider_voice_id = _optional_str(unresolved_fallback_voice.get("provider_voice_id"))
    if not provider or not provider_voice_id:
        raise SynthesisManifestError("fallback voice requires provider and provider_voice_id")
    record = _voice_entry(registry, provider, provider_voice_id)
    if record is None or not is_voice_selectable(record):
        raise SynthesisManifestError("fallback voice is unavailable")
    generated = VoiceAssignment(voice_id=f"{provider}.{provider_voice_id}", provider=provider, provider_voice_id=provider_voice_id, source="explicit fallback", continuity_status="fallback", rationale="explicit unresolved-speaker fallback", generated=True)
    return EditableAssignment(target_kind="narrator", canonical_character_id=None, canonical_name="Fallback", generated_assignment=generated, requested_provider=provider, requested_provider_voice_id=provider_voice_id, locked=True, manual_override=False, user_modified=False, assignment_origin="system", notes=None, effective_assignment=generated)


def _voice_lookup(registry: Mapping[str, Any]) -> dict[tuple[str, str], Mapping[str, Any]]:
    lookup: dict[tuple[str, str], Mapping[str, Any]] = {}
    for voice in registry.get("voices", []) or []:
        if isinstance(voice, Mapping):
            provider = _optional_str(voice.get("provider"))
            provider_voice_id = _optional_str(voice.get("provider_voice_id"))
            if provider and provider_voice_id:
                lookup[(provider, provider_voice_id)] = voice
    return lookup


def _voice_entry(registry: Mapping[str, Any], provider: str, provider_voice_id: str) -> Mapping[str, Any] | None:
    return _voice_lookup(registry).get((provider, provider_voice_id))


def _resolve_voice_entry(registry: Mapping[str, Any], assignment: EditableAssignment | None, unit: EditableAssignment) -> Mapping[str, Any] | None:
    if assignment is None:
        return None
    provider = assignment.effective_assignment.provider if assignment.effective_assignment else assignment.generated_assignment.provider
    provider_voice_id = assignment.effective_assignment.provider_voice_id if assignment.effective_assignment else assignment.generated_assignment.provider_voice_id
    if provider is None or provider_voice_id is None:
        return None
    return _voice_entry(registry, provider, provider_voice_id)


def _voice_supports_language(voice_entry: Mapping[str, Any], language: str) -> bool:
    languages = [str(item).lower() for item in voice_entry.get("supported_languages", []) or []]
    if not languages:
        return True
    normalized = language.lower()
    return normalized in languages or any(normalized.split("-")[0] == supported.split("-")[0] for supported in languages)


def _normalize_controls(controls: Any) -> dict[str, Any]:
    if not isinstance(controls, Mapping):
        return {}
    return {str(key): copy.deepcopy(value) for key, value in controls.items()}


def _control_value_valid(key: str, value: Any) -> bool:
    if key in {"rate", "pitch", "volume"}:
        return isinstance(value, (int, float))
    if key in {"pause_before", "pause_after", "emphasis"}:
        return isinstance(value, (int, float, str, bool))
    return True


def _normalized_control_payload(controls: Mapping[str, Any], pronunciation_notes: str | None, performance_notes: str | None, pace_intent: str | None, pause_intent: str | None, emphasis_intent: str | None) -> dict[str, Any]:
    return {
        "controls": {str(k): controls[k] for k in sorted(controls)},
        "pronunciation_notes": pronunciation_notes,
        "performance_notes": performance_notes,
        "pace_intent": pace_intent,
        "pause_intent": pause_intent,
        "emphasis_intent": emphasis_intent,
    }


def _pronunciation_payload(unit: RenderUnit) -> dict[str, Any]:
    return {
        "pronunciation_notes": unit.pronunciation_notes,
        "performance_notes": unit.performance_notes,
        "pace_intent": unit.pace_intent,
        "pause_intent": unit.pause_intent,
        "emphasis_intent": unit.emphasis_intent,
    }


def _story_language(story: Mapping[str, Any]) -> str | None:
    language = _optional_str(story.get("language"))
    return language or None


def _infer_scene_id(story: Mapping[str, Any], segment: Mapping[str, Any]) -> str:
    scenes = story.get("scenes", []) or []
    chapter = segment.get("chapter") or segment.get("chapter_number")
    paragraph_index = segment.get("paragraph_index") or segment.get("paragraph")
    for scene in scenes:
        if not isinstance(scene, Mapping):
            continue
        if chapter is not None and int(scene.get("chapter") or 0) != int(chapter):
            continue
        start_paragraph = int(scene.get("start_paragraph") or 0)
        end_paragraph = int(scene.get("end_paragraph") or 0)
        if paragraph_index is not None and start_paragraph <= int(paragraph_index) <= end_paragraph:
            return _safe_token(str(scene.get("scene_id") or f"chapter-{chapter}-scene-{scene.get('scene_number', 0)}"))
    return _safe_token(str(segment.get("scene_id") or f"chapter-{chapter or 0}-scene-{segment.get('scene_number') or 0}"))


def _normalize_source_reference(value: Any, chapter: int, segment: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return _deepcopy_mapping(value)
    paragraph_index = int(segment.get("paragraph_index") or 0)
    source_document_id = _optional_str(segment.get("source_document_id") or segment.get("source_document"))
    source_text = str(segment.get("source_text") or segment.get("synthesis_text") or segment.get("text") or "")
    return {
        "chapter": chapter,
        "paragraph_index": paragraph_index,
        "source_document_id": source_document_id,
        "source_text_hash": _hash_source_text(_source_identity(segment, {}, _safe_token(str(segment.get("scene_id") or ""))), source_text),
        "excerpt": source_text[:240],
    }


def _source_identity(segment: Mapping[str, Any], story: Mapping[str, Any], scene_id: str) -> str:
    source_document_id = _optional_str(segment.get("source_document_id") or story.get("source_document_id") or story.get("book_id") or story.get("series_id")) or "unknown"
    return _stable_join([story.get("book_id") or source_document_id, scene_id, _optional_str(segment.get("segment_id") or segment.get("id") or segment.get("source_reference", {}).get("paragraph_index") if isinstance(segment.get("source_reference"), Mapping) else None) or "", _optional_str(segment.get("segment_type") or segment.get("type") or "") or ""])


def _canonical_segment_id(segment: Mapping[str, Any], source_identity: str) -> str:
    explicit = _optional_str(segment.get("segment_id") or segment.get("canonical_segment_id") or segment.get("id"))
    if explicit:
        return explicit
    return f"seg-{_short_hash(source_identity)}"


def _hash_source_text(source_identity: str, source_text: str) -> str:
    return sha256(_stable_join([source_identity, _normalize_whitespace(source_text)]).encode("utf-8")).hexdigest()


def _hash_synthesis_input(
    *,
    canonical_segment_id: str,
    scene_id: str,
    segment_type: str,
    canonical_speaker_id: str | None,
    source_text: str,
    provider: str,
    provider_voice_id: str,
    language: str | None,
    renderer_controls: Mapping[str, Any],
    pronunciation_notes: str | None,
    performance_notes: str | None,
    pace_intent: str | None,
    pause_intent: str | None,
    emphasis_intent: str | None,
    renderer_contract_version: int,
) -> str:
    payload = {
        "canonical_segment_id": canonical_segment_id,
        "scene_id": scene_id,
        "segment_type": segment_type,
        "canonical_speaker_id": canonical_speaker_id,
        "source_text": source_text,
        "provider": provider,
        "provider_voice_id": provider_voice_id,
        "language": language,
        "renderer_controls": {str(k): renderer_controls[k] for k in sorted(renderer_controls)},
        "pronunciation_notes": pronunciation_notes,
        "performance_notes": performance_notes,
        "pace_intent": pace_intent,
        "pause_intent": pause_intent,
        "emphasis_intent": emphasis_intent,
        "renderer_contract_version": renderer_contract_version,
    }
    return _hash_canonical(payload)


def _render_unit_id(book_id: str, scene_id: str, canonical_segment_id: str, segment_type: str, canonical_speaker_id: str | None) -> str:
    raw = _stable_join([book_id, scene_id, canonical_segment_id, segment_type, canonical_speaker_id or ""])
    return f"ru_{_short_hash(raw)}"


def _artifact_key(scene_id: str, render_unit_id: str) -> str:
    return f"segments/{scene_id}/{render_unit_id}.audio"


def _hash_manifest_content(manifest: SynthesisManifest | Mapping[str, Any]) -> str:
    payload = dataclass_to_dict(manifest) if isinstance(manifest, SynthesisManifest) else _deepcopy_mapping(manifest)
    payload.pop("manifest_content_hash", None)
    payload.pop("created_at", None)
    return _hash_canonical(payload)


def _canonicalize_story_for_hash(story: Mapping[str, Any]) -> dict[str, Any]:
    payload = _deepcopy_mapping(story)
    for key in ("characters", "scenes", "dialogue", "narration_paragraphs", "segments"):
        value = payload.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            items = [item for item in value if isinstance(item, Mapping)]
            payload[key] = sorted(items, key=_story_collection_sort_key)
    return payload


def _story_collection_sort_key(item: Mapping[str, Any]) -> tuple:
    return (
        str(item.get("chapter") or item.get("chapter_number") or 0),
        str(item.get("scene_id") or item.get("canonical_character_id") or item.get("dialogue_id") or item.get("segment_id") or item.get("id") or ""),
        str(item.get("paragraph_index") or item.get("start_paragraph") or 0),
        str(item.get("source_text_hash") or ""),
        str(item.get("speaker") or item.get("canonical_name") or item.get("name") or ""),
    )


def _hash_generated_plan(plan: VoicePlan) -> str:
    return _hash_canonical(plan)


def _hash_canonical(value: Any) -> str:
    return sha256(canonical_json_dumps(value).encode("utf-8")).hexdigest()


def _stable_join(parts: Sequence[Any]) -> str:
    return "|".join("" if part is None else str(part) for part in parts)


def _short_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:24]


def _normalize_whitespace(text: str) -> str:
    return " ".join(str(text).split())


def _normalize_synthesis_text(text: str) -> str:
    return _normalize_whitespace(text)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalize_list(values: Any) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return [str(item) for item in values if str(item)]


def _deepcopy_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise SynthesisManifestError("expected a mapping")
    return copy.deepcopy(dict(value))


def _normalize_source_artifacts(source_artifacts: Mapping[str, Any]) -> dict[str, Any]:
    payload = _deepcopy_mapping(source_artifacts)
    _validate_safe_source_artifacts(payload)
    return payload


def _validate_safe_source_artifacts(source_artifacts: Mapping[str, Any]) -> None:
    for key, value in source_artifacts.items():
        if isinstance(value, Mapping) and "path" in value:
            path = str(value["path"])
        else:
            path = str(value)
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise SynthesisManifestError(f"unsafe source artifact path for {key}: {path}")


def _validate_registry_immutability_inputs(registry: Mapping[str, Any]) -> None:
    errors = validate_voice_registry(registry)
    if errors:
        raise SynthesisManifestError("invalid voice registry: " + "; ".join(errors))


def _validate_story_inputs(story: Mapping[str, Any]) -> None:
    required = ["book_id", "series_id", "source_analysis_hash", "source_analysis_path"]
    for key in required:
        if key not in story:
            raise SynthesisManifestError(f"missing required normalized story key: {key}")
    if not isinstance(story.get("segments"), Sequence) and not isinstance(story.get("dialogue"), Sequence) and not isinstance(story.get("narration_paragraphs"), Sequence):
        raise SynthesisManifestError("normalized story must include segments or dialogue/narration records")


# Public helpers for canonical data round-tripping -------------------------------------------------


def validate_synthesis_manifest_payload(payload: Mapping[str, Any]) -> ManifestValidationReport:
    manifest = _coerce_manifest(payload)
    return validate_synthesis_manifest(manifest)


def _normalize_segment_order(segment: Mapping[str, Any]) -> tuple[int, int, int, str]:
    source_order = segment.get("source_order")
    if isinstance(source_order, Sequence) and not isinstance(source_order, (str, bytes)):
        order = tuple(int(v) for v in source_order[:3])
        if len(order) == 3:
            return order[0], order[1], order[2], _safe_token(str(segment.get("segment_id") or ""))
    chapter = int(segment.get("chapter") or 0)
    scene_number = int(segment.get("scene_number") or 0)
    paragraph_index = int(segment.get("paragraph_index") or 0)
    return chapter, scene_number, paragraph_index, _safe_token(str(segment.get("segment_id") or ""))


# Internal accessors used by compare/normalization -------------------------------------------------


def _safe_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip().lower())
    while "--" in token:
        token = token.replace("--", "-")
    return token.strip("-._") or "segment"


# Compatibility aliases for planned import surface -------------------------------------------------

ManifestValidationReportType = ManifestValidationReport
ManifestValidationIssueType = ManifestValidationIssue


__all__ = [
    "ManifestBuildResult",
    "ManifestChangeReason",
    "ManifestDiff",
    "ManifestValidationIssue",
    "ManifestValidationReport",
    "RenderUnit",
    "SynthesisManifest",
    "SynthesisManifestError",
    "SYNTHESIS_MANIFEST_SCHEMA_VERSION",
    "build_synthesis_manifest",
    "compare_synthesis_manifests",
    "load_synthesis_manifest",
    "save_synthesis_manifest_atomic",
    "serialize_synthesis_manifest",
    "validate_synthesis_manifest",
    "validate_synthesis_manifest_payload",
]
