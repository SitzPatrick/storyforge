from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping, Sequence

from .bindings import binding_registry_status
from .models import AssignmentProvenance, CharacterPlan, NarratorPlan, PlanningReport, VoiceAssignment, VoicePlan, dataclass_to_dict
from .registry import is_voice_selectable
from .schema import SCHEMA_VERSIONS, canonical_json_dumps, validate_voice_plan

EDITABLE_VOICE_PLAN_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PlanValidationIssue:
    severity: str
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class PlanEditRecord:
    target_kind: str
    canonical_character_id: str | None = None
    previous_provider: str | None = None
    previous_provider_voice_id: str | None = None
    requested_provider: str | None = None
    requested_provider_voice_id: str | None = None
    effective_provider: str | None = None
    effective_provider_voice_id: str | None = None
    lock_state_change: str | None = None
    manual_override_change: str | None = None
    timestamp: str | None = None
    reason: str | None = None
    user_note: str | None = None
    validation_result: str | None = None


@dataclass(frozen=True)
class ManualOverride:
    target_kind: str
    canonical_character_id: str | None = None
    requested_provider: str | None = None
    requested_provider_voice_id: str | None = None
    locked: bool | None = None
    manual_override: bool | None = None
    notes: str | None = None
    override_reason: str | None = None
    pronunciation_notes: str | None = None
    casting_notes: str | None = None
    reuse_permission: bool | None = None
    separation_constraints: list[str] = field(default_factory=list)
    timestamp: str | None = None


@dataclass(frozen=True)
class EditableAssignment:
    target_kind: str
    canonical_character_id: str | None
    canonical_name: str | None
    generated_assignment: VoiceAssignment
    requested_provider: str | None = None
    requested_provider_voice_id: str | None = None
    locked: bool = False
    manual_override: bool = False
    user_modified: bool = False
    assignment_origin: str = "generated"
    notes: str | None = None
    override_reason: str | None = None
    pronunciation_notes: str | None = None
    casting_notes: str | None = None
    reuse_permission: bool | None = None
    separation_constraints: list[str] = field(default_factory=list)
    effective_assignment: VoiceAssignment | None = None
    validation_status: str = "valid"
    validation_issues: list[PlanValidationIssue] = field(default_factory=list)
    edit_history: list[PlanEditRecord] = field(default_factory=list)


@dataclass(frozen=True)
class EditableVoicePlan:
    schema_version: int
    book_id: str
    series_id: str
    source_analysis_hash: str
    source_analysis_path: str
    generated_plan: VoicePlan
    narrator: EditableAssignment
    characters: list[EditableAssignment] = field(default_factory=list)
    edit_history: list[PlanEditRecord] = field(default_factory=list)
    retired_assignments: list[PlanEditRecord] = field(default_factory=list)
    user_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation_issues: list[PlanValidationIssue] = field(default_factory=list)
    generated_content_hash: str = ""
    user_editable_hash: str = ""
    effective_plan_hash: str = ""
    generated_at: str | None = None
    generated_by: str | None = None
    source_voice_registry_hash: str | None = None
    source_series_bindings_hash: str | None = None


@dataclass(frozen=True)
class PlanMergeResult:
    editable_plan: EditableVoicePlan
    effective_plan: VoicePlan
    validation_issues: list[PlanValidationIssue] = field(default_factory=list)
    applied_edits: list[PlanEditRecord] = field(default_factory=list)
    preserved_edits: list[PlanEditRecord] = field(default_factory=list)
    rejected_edits: list[PlanEditRecord] = field(default_factory=list)
    removed_character_ids: list[str] = field(default_factory=list)


class EditableVoicePlanError(ValueError):
    pass


def load_editable_voice_plan(source: str | Path | Mapping[str, Any] | VoicePlan | EditableVoicePlan, *, generated_plan: VoicePlan | None = None, registry: Mapping[str, Any] | None = None) -> EditableVoicePlan:
    if isinstance(source, EditableVoicePlan):
        plan = source
    else:
        data = _load_source_data(source)
        if data is None:
            if generated_plan is None:
                raise EditableVoicePlanError("editable voice plan source does not exist and no generated plan was supplied")
            plan = _wrap_generated_plan(generated_plan)
        else:
            plan = _coerce_editable_plan(data, generated_plan=generated_plan)
    issues = validate_editable_voice_plan(plan, registry=registry)
    if issues:
        raise EditableVoicePlanError(_format_issues(issues))
    return plan


def validate_editable_voice_plan(data: EditableVoicePlan | Mapping[str, Any] | VoicePlan, *, registry: Mapping[str, Any] | None = None) -> list[PlanValidationIssue]:
    if isinstance(data, EditableVoicePlan):
        payload = dataclass_to_dict(data)
    elif isinstance(data, VoicePlan):
        payload = dataclass_to_dict(_wrap_generated_plan(data))
    else:
        payload = dict(data)
    issues = _validate_editable_payload(payload, registry=registry)
    return issues


def merge_voice_plans(previous: EditableVoicePlan | Mapping[str, Any] | VoicePlan | None, generated_plan: VoicePlan, *, registry: Mapping[str, Any] | None = None, allow_fallback: bool = True) -> PlanMergeResult:
    prior = _coerce_previous_plan(previous, generated_plan=generated_plan, registry=registry)
    merged = _merge_previous_and_generated(prior, generated_plan, registry=registry, allow_fallback=allow_fallback)
    effective_plan = resolve_effective_voice_plan(merged)
    merged = replace(
        merged,
        generated_content_hash=_hash_generated_plan(generated_plan),
        user_editable_hash=_hash_user_editable_plan(merged),
        effective_plan_hash=_hash_effective_plan(effective_plan),
    )
    validation_issues = validate_editable_voice_plan(merged, registry=registry)
    merged = replace(merged, validation_issues=validation_issues)
    return PlanMergeResult(
        editable_plan=merged,
        effective_plan=effective_plan,
        validation_issues=validation_issues,
        applied_edits=[record for record in merged.edit_history if record.validation_result == "applied"],
        preserved_edits=[record for record in merged.edit_history if record.validation_result == "preserved"],
        rejected_edits=[record for record in merged.edit_history if record.validation_result not in {"applied", "preserved"}],
        removed_character_ids=[record.canonical_character_id for record in merged.retired_assignments if record.canonical_character_id],
    )


def apply_manual_override(plan: EditableVoicePlan | Mapping[str, Any] | VoicePlan, override: ManualOverride, *, registry: Mapping[str, Any] | None = None) -> EditableVoicePlan:
    editable = _coerce_previous_plan(plan, registry=registry)
    target, narrator_index = _locate_assignment(editable, override.target_kind, override.canonical_character_id)
    updated = _apply_override_to_assignment(target, override, registry=registry)
    edit_record = _build_edit_record(target, updated, override, validation_result=updated.validation_status)
    if narrator_index is None:
        characters = [updated if item is target else item for item in editable.characters]
        narrator = updated
    else:
        characters = list(editable.characters)
        characters[narrator_index] = updated
        narrator = editable.narrator if target is not editable.narrator else updated
    edit_history = [*editable.edit_history, edit_record]
    editable = replace(editable, narrator=narrator, characters=characters, edit_history=edit_history)
    return _finalize_editable_plan(editable, generated_plan=editable.generated_plan, registry=registry)


def set_assignment_lock(plan: EditableVoicePlan | Mapping[str, Any] | VoicePlan, *, target_kind: str, canonical_character_id: str | None = None, locked: bool, registry: Mapping[str, Any] | None = None) -> EditableVoicePlan:
    editable = _coerce_previous_plan(plan, registry=registry)
    target, narrator_index = _locate_assignment(editable, target_kind, canonical_character_id)
    updated = replace(target, locked=locked, user_modified=True, assignment_origin="user")
    record = PlanEditRecord(
        target_kind=target_kind,
        canonical_character_id=canonical_character_id,
        previous_provider=target.generated_assignment.provider,
        previous_provider_voice_id=target.generated_assignment.provider_voice_id,
        effective_provider=target.effective_assignment.provider if target.effective_assignment else target.generated_assignment.provider,
        effective_provider_voice_id=target.effective_assignment.provider_voice_id if target.effective_assignment else target.generated_assignment.provider_voice_id,
        lock_state_change=f"{target.locked}->{locked}",
        manual_override_change=None,
        validation_result="applied",
    )
    if narrator_index is None:
        characters = [updated if item is target else item for item in editable.characters]
        narrator = updated
    else:
        characters = list(editable.characters)
        characters[narrator_index] = updated
        narrator = editable.narrator if target is not editable.narrator else updated
    return _finalize_editable_plan(replace(editable, narrator=narrator, characters=characters, edit_history=[*editable.edit_history, record]), generated_plan=editable.generated_plan, registry=registry)


def save_voice_plan_atomic(path: str | Path, plan: EditableVoicePlan | Mapping[str, Any] | VoicePlan) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    editable = _coerce_previous_plan(plan)
    payload = serialize_editable_voice_plan(editable) + "\n"
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


def serialize_editable_voice_plan(plan: EditableVoicePlan | Mapping[str, Any]) -> str:
    if isinstance(plan, EditableVoicePlan):
        return canonical_json_dumps(plan)
    return canonical_json_dumps(_coerce_previous_plan(plan))


def resolve_effective_voice_plan(plan: EditableVoicePlan | Mapping[str, Any]) -> VoicePlan:
    editable = plan if isinstance(plan, EditableVoicePlan) else _coerce_previous_plan(plan)
    return _build_effective_voice_plan(editable)


def _build_effective_voice_plan(editable: EditableVoicePlan) -> VoicePlan:
    narrator_assignment = editable.narrator.effective_assignment or editable.narrator.generated_assignment
    characters: list[CharacterPlan] = []
    for assignment in editable.characters:
        effective = assignment.effective_assignment or assignment.generated_assignment
        characters.append(
            CharacterPlan(
                canonical_character_id=assignment.canonical_character_id or "",
                canonical_name=assignment.canonical_name or (assignment.canonical_character_id or ""),
                role=None,
                prominence=None,
                speaking_frequency=None,
                first_appearance=None,
                likely_recurrence=None,
                age_bucket=None,
                gender_presentation=None,
                species_or_archetype=None,
                scene_relationships=[],
                unresolved_metadata={},
                assignment=effective,
                notes=assignment.notes,
            )
        )
    return VoicePlan(
        schema_version=editable.generated_plan.schema_version,
        planner_version=editable.generated_plan.planner_version,
        book_id=editable.book_id,
        series_id=editable.series_id,
        source_analysis_hash=editable.source_analysis_hash,
        source_analysis_path=editable.source_analysis_path,
        narrator=NarratorPlan(assignment=narrator_assignment, rationale=editable.narrator.notes or editable.generated_plan.narrator.rationale),
        characters=characters,
        conflicts=list(editable.generated_plan.conflicts),
        scarcity_events=list(editable.generated_plan.scarcity_events),
        warnings=list(editable.generated_plan.warnings),
        statistics=dict(editable.generated_plan.statistics),
        generated_at=editable.generated_at or editable.generated_plan.generated_at,
        generated_by=editable.generated_by or editable.generated_plan.generated_by,
        source_voice_registry_hash=editable.source_voice_registry_hash or editable.generated_plan.source_voice_registry_hash,
        source_series_bindings_hash=editable.source_series_bindings_hash or editable.generated_plan.source_series_bindings_hash,
        notes=editable.generated_plan.notes,
        user_editable_notes=list(editable.user_notes),
    )


def _load_source_data(source: str | Path | Mapping[str, Any] | VoicePlan | EditableVoicePlan) -> Mapping[str, Any] | None:
    if isinstance(source, Mapping):
        return source
    if isinstance(source, VoicePlan):
        return dataclass_to_dict(_wrap_generated_plan(source))
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise EditableVoicePlanError(f"malformed JSON in editable voice plan file {path.name}: {exc.msg} at line {exc.lineno} column {exc.colno}") from exc
        except OSError as exc:
            raise EditableVoicePlanError(f"unable to read editable voice plan file {path}: {exc.strerror or exc}") from exc
    raise EditableVoicePlanError(f"unsupported editable voice plan source: {type(source)!r}")


def _wrap_generated_plan(plan: VoicePlan) -> EditableVoicePlan:
    narrator = _editable_assignment_from_generated("narrator", None, plan.narrator.assignment, plan.narrator.rationale)
    characters = [
        _editable_assignment_from_generated("character", character.canonical_character_id, character.assignment, character.notes)
        for character in plan.characters
    ]
    wrapped = EditableVoicePlan(
        schema_version=EDITABLE_VOICE_PLAN_SCHEMA_VERSION,
        book_id=plan.book_id,
        series_id=plan.series_id,
        source_analysis_hash=plan.source_analysis_hash,
        source_analysis_path=plan.source_analysis_path,
        generated_plan=plan,
        narrator=narrator,
        characters=characters,
        edit_history=[],
        retired_assignments=[],
        user_notes=list(plan.user_editable_notes),
        warnings=list(plan.warnings),
        validation_issues=[],
        generated_content_hash=_hash_generated_plan(plan),
        user_editable_hash="",
        effective_plan_hash="",
        generated_at=plan.generated_at,
        generated_by=plan.generated_by,
        source_voice_registry_hash=plan.source_voice_registry_hash,
        source_series_bindings_hash=plan.source_series_bindings_hash,
    )
    return _finalize_editable_plan(wrapped, generated_plan=plan)


def _coerce_previous_plan(source: EditableVoicePlan | Mapping[str, Any] | VoicePlan | None, *, generated_plan: VoicePlan | None = None, registry: Mapping[str, Any] | None = None) -> EditableVoicePlan:
    if source is None:
        if generated_plan is None:
            raise EditableVoicePlanError("previous plan is required when no generated plan is supplied")
        return _wrap_generated_plan(generated_plan)
    if isinstance(source, EditableVoicePlan):
        return source
    if isinstance(source, VoicePlan):
        return _wrap_generated_plan(source)
    data = _load_source_data(source)
    if data is None:
        if generated_plan is None:
            raise EditableVoicePlanError("editable voice plan source does not exist and no generated plan was supplied")
        return _wrap_generated_plan(generated_plan)
    return _coerce_editable_plan(data, generated_plan=generated_plan, registry=registry)


def _coerce_editable_plan(data: Mapping[str, Any], *, generated_plan: VoicePlan | None = None, registry: Mapping[str, Any] | None = None) -> EditableVoicePlan:
    if "generated_plan" in data:
        generated_value = data.get("generated_plan")
        if isinstance(generated_value, VoicePlan):
            gen = generated_value
            payload = dict(data)
            payload["generated_plan"] = dataclass_to_dict(gen)
        elif isinstance(generated_value, Mapping):
            gen = _coerce_voice_plan(generated_value)
            payload = dict(data)
        else:
            raise EditableVoicePlanError("editable voice plan generated_plan must be a generated voice plan or mapping")
    elif all(key in data for key in ("schema_version", "planner_version", "book_id", "series_id", "narrator", "characters", "conflicts", "scarcity_events", "warnings", "statistics")):
        gen = _coerce_voice_plan(data)
        payload = {
            "schema_version": EDITABLE_VOICE_PLAN_SCHEMA_VERSION,
            "book_id": gen.book_id,
            "series_id": gen.series_id,
            "source_analysis_hash": gen.source_analysis_hash,
            "source_analysis_path": gen.source_analysis_path,
            "generated_plan": dataclass_to_dict(gen),
            "editable": {"narrator": {}, "characters": []},
            "edit_history": [],
            "retired_assignments": [],
            "user_notes": list(gen.user_editable_notes),
            "warnings": list(gen.warnings),
            "validation_issues": [],
        }
    else:
        raise EditableVoicePlanError("voice plan payload must be either a generated voice plan or an editable voice plan")
    if generated_plan is not None:
        gen = generated_plan
        payload["generated_plan"] = dataclass_to_dict(gen)
    issues = _validate_editable_payload(payload, registry=registry)
    if issues:
        raise EditableVoicePlanError(_format_issues(issues))
    narrator_assignment = gen.narrator.assignment
    if narrator_assignment is None:
        raise EditableVoicePlanError("generated narrator assignment is missing")
    editable_section = payload.get("editable")
    if not isinstance(editable_section, Mapping):
        editable_section = payload
    narrator = _coerce_editable_assignment(editable_section.get("narrator"), "narrator", None, narrator_assignment, gen.narrator.rationale, registry=registry)
    characters: list[EditableAssignment] = []
    character_edits = editable_section.get("characters", []) if isinstance(editable_section, Mapping) else []
    edits_by_id = {}
    for entry in character_edits:
        if isinstance(entry, Mapping):
            edits_by_id[entry.get("canonical_character_id")] = entry
    for character in gen.characters:
        edits = edits_by_id.get(character.canonical_character_id)
        assignment = character.assignment
        if assignment is None:
            raise EditableVoicePlanError(f"generated character assignment is missing for {character.canonical_character_id}")
        characters.append(_coerce_editable_assignment(edits, "character", character.canonical_character_id, assignment, canonical_name=character.canonical_name, notes=character.notes, registry=registry))
    plan = EditableVoicePlan(
        schema_version=EDITABLE_VOICE_PLAN_SCHEMA_VERSION,
        book_id=payload.get("book_id", gen.book_id),
        series_id=payload.get("series_id", gen.series_id),
        source_analysis_hash=payload.get("source_analysis_hash", gen.source_analysis_hash),
        source_analysis_path=payload.get("source_analysis_path", gen.source_analysis_path),
        generated_plan=gen,
        narrator=narrator,
        characters=characters,
        edit_history=[_coerce_edit_record(item) for item in payload.get("edit_history", []) if isinstance(item, Mapping)],
        retired_assignments=[_coerce_edit_record(item) for item in payload.get("retired_assignments", []) if isinstance(item, Mapping)],
        user_notes=list(payload.get("user_notes", []) or []),
        warnings=list(payload.get("warnings", []) or []),
        validation_issues=[_coerce_issue(item) for item in payload.get("validation_issues", []) if isinstance(item, Mapping)],
        generated_content_hash=str(payload.get("generated_content_hash", _hash_generated_plan(gen))),
        user_editable_hash=str(payload.get("user_editable_hash", "")),
        effective_plan_hash=str(payload.get("effective_plan_hash", "")),
        generated_at=payload.get("generated_at", gen.generated_at),
        generated_by=payload.get("generated_by", gen.generated_by),
        source_voice_registry_hash=payload.get("source_voice_registry_hash", gen.source_voice_registry_hash),
        source_series_bindings_hash=payload.get("source_series_bindings_hash", gen.source_series_bindings_hash),
    )
    return _finalize_editable_plan(plan, generated_plan=gen, registry=registry)


def _coerce_voice_plan(data: Mapping[str, Any]) -> VoicePlan:
    errors = validate_voice_plan(data)
    if errors:
        raise EditableVoicePlanError("; ".join(errors))
    narrator = _coerce_voice_assignment(data.get("narrator", {}).get("assignment") if isinstance(data.get("narrator"), Mapping) else data.get("narrator"), default_target="narrator")
    narrator_plan = NarratorPlan(assignment=narrator, rationale=_optional_str(data.get("narrator", {}).get("rationale")) if isinstance(data.get("narrator"), Mapping) else None, notes=_optional_str(data.get("narrator", {}).get("notes")) if isinstance(data.get("narrator"), Mapping) else None)
    characters: list[CharacterPlan] = []
    for item in data.get("characters", []):
        if not isinstance(item, Mapping):
            raise EditableVoicePlanError("voice plan character must be a mapping")
        assignment = _coerce_voice_assignment(item.get("assignment"), default_target="character")
        characters.append(
            CharacterPlan(
                canonical_character_id=str(item.get("canonical_character_id", "")),
                canonical_name=str(item.get("canonical_name", item.get("canonical_character_id", ""))),
                role=_optional_str(item.get("role")),
                prominence=_optional_str(item.get("prominence")),
                speaking_frequency=int(item.get("speaking_frequency") or 0),
                first_appearance=item.get("first_appearance"),
                likely_recurrence=item.get("likely_recurrence"),
                age_bucket=_optional_str(item.get("age_bucket")),
                gender_presentation=_optional_str(item.get("gender_presentation")),
                species_or_archetype=_optional_str(item.get("species_or_archetype")),
                scene_relationships=list(item.get("scene_relationships", []) or []),
                unresolved_metadata=dict(item.get("unresolved_metadata", {}) or {}),
                assignment=assignment,
                notes=_optional_str(item.get("notes")),
            )
        )
    return VoicePlan(
        schema_version=int(data.get("schema_version", 1)),
        planner_version=str(data.get("planner_version", "")),
        book_id=str(data.get("book_id", "")),
        series_id=str(data.get("series_id", "")),
        source_analysis_hash=str(data.get("source_analysis_hash", "")),
        source_analysis_path=str(data.get("source_analysis_path", "")),
        narrator=narrator_plan,
        characters=characters,
        conflicts=[],
        scarcity_events=[],
        warnings=list(data.get("warnings", []) or []),
        statistics=dict(data.get("statistics", {}) or {}),
        generated_at=_optional_str(data.get("generated_at")),
        generated_by=_optional_str(data.get("generated_by")),
        source_voice_registry_hash=_optional_str(data.get("source_voice_registry_hash")),
        source_series_bindings_hash=_optional_str(data.get("source_series_bindings_hash")),
        notes=_optional_str(data.get("notes")),
        user_editable_notes=list(data.get("user_editable_notes", []) or []),
    )


def _coerce_voice_assignment(data: Any, *, default_target: str) -> VoiceAssignment:
    if isinstance(data, VoiceAssignment):
        return data
    if not isinstance(data, Mapping):
        return VoiceAssignment(voice_id=None, provider=None, provider_voice_id=None, source="unassigned", continuity_status="unassigned", generated=False, rationale="unassigned")
    return VoiceAssignment(
        voice_id=_optional_str(data.get("voice_id")),
        provider=_optional_str(data.get("provider")),
        provider_voice_id=_optional_str(data.get("provider_voice_id")),
        locked=bool(data.get("locked", False)),
        source=str(data.get("source", "automatic")),
        continuity_status=_optional_str(data.get("continuity_status")),
        registry_key=_optional_str(data.get("registry_key")),
        score=data.get("score"),
        score_components=list(data.get("score_components", []) or []),
        scarcity_effects=list(data.get("scarcity_effects", []) or []),
        conflict_effects=list(data.get("conflict_effects", []) or []),
        relaxed_constraints=list(data.get("relaxed_constraints", []) or []),
        preserved_constraints=list(data.get("preserved_constraints", []) or []),
        confidence=data.get("confidence"),
        unavailable_reason=_optional_str(data.get("unavailable_reason")),
        rationale=_optional_str(data.get("rationale")) or f"{default_target} assignment",
        rejected_candidates=list(data.get("rejected_candidates", []) or []),
        edited_at=_optional_str(data.get("edited_at")),
        edited_by=_optional_str(data.get("edited_by")),
        notes=_optional_str(data.get("notes")),
        generated=bool(data.get("generated", True)),
        provenance=_coerce_provenance(data.get("provenance")),
    )


def _coerce_provenance(data: Any) -> AssignmentProvenance | None:
    if data is None:
        return None
    if isinstance(data, AssignmentProvenance):
        return data
    if not isinstance(data, Mapping):
        return None
    return AssignmentProvenance(
        source=str(data.get("source", "")),
        reason=str(data.get("reason", "")),
        basis=str(data.get("basis", "")),
        selected_from=list(data.get("selected_from", []) or []),
        score=data.get("score"),
        tie_breaker=_optional_str(data.get("tie_breaker")),
    )


def _coerce_editable_assignment(data: Mapping[str, Any] | None, target_kind: str, canonical_character_id: str | None, generated_assignment: VoiceAssignment, canonical_name: str | None = None, notes: str | None = None, *, registry: Mapping[str, Any] | None = None) -> EditableAssignment:
    if data is None:
        data = {}
    requested_provider = _optional_str(data.get("requested_provider"))
    requested_provider_voice_id = _optional_str(data.get("requested_provider_voice_id"))
    locked = bool(data.get("locked", False))
    manual_override = bool(data.get("manual_override", False))
    notes = _optional_str(data.get("notes")) or notes
    override_reason = _optional_str(data.get("override_reason"))
    pronunciation_notes = _optional_str(data.get("pronunciation_notes"))
    casting_notes = _optional_str(data.get("casting_notes"))
    reuse_permission = data.get("reuse_permission") if data.get("reuse_permission") is None or isinstance(data.get("reuse_permission"), bool) else None
    separation_constraints = [str(item) for item in (data.get("separation_constraints", []) or [])]
    user_modified = bool(data.get("user_modified", False)) or any(
        [requested_provider, requested_provider_voice_id, locked, manual_override, notes, override_reason, pronunciation_notes, casting_notes, reuse_permission is not None, separation_constraints]
    )
    effective_assignment = _effective_assignment_from_request(
        generated_assignment,
        requested_provider=requested_provider,
        requested_provider_voice_id=requested_provider_voice_id,
        registry=registry,
    )
    validation_status, validation_issues = _validate_assignment_choice(
        generated_assignment,
        requested_provider=requested_provider,
        requested_provider_voice_id=requested_provider_voice_id,
        effective_assignment=effective_assignment,
        registry=registry,
        locked=locked,
        manual_override=manual_override,
    )
    assignment_origin = str(data.get("assignment_origin", "user" if user_modified else "generated"))
    return EditableAssignment(
        target_kind=target_kind,
        canonical_character_id=canonical_character_id,
        canonical_name=canonical_name,
        generated_assignment=generated_assignment,
        requested_provider=requested_provider,
        requested_provider_voice_id=requested_provider_voice_id,
        locked=locked,
        manual_override=manual_override,
        user_modified=user_modified,
        assignment_origin=assignment_origin,
        notes=notes,
        override_reason=override_reason,
        pronunciation_notes=pronunciation_notes,
        casting_notes=casting_notes,
        reuse_permission=reuse_permission,
        separation_constraints=separation_constraints,
        effective_assignment=effective_assignment,
        validation_status=validation_status,
        validation_issues=validation_issues,
        edit_history=[_coerce_edit_record(item) for item in data.get("edit_history", []) if isinstance(item, Mapping)],
    )


def _effective_assignment_from_request(generated_assignment: VoiceAssignment, *, requested_provider: str | None, requested_provider_voice_id: str | None, registry: Mapping[str, Any] | None) -> VoiceAssignment:
    if requested_provider and requested_provider_voice_id and _registry_voice_available(registry, requested_provider, requested_provider_voice_id):
        return replace(generated_assignment, provider=requested_provider, provider_voice_id=requested_provider_voice_id, voice_id=f"{requested_provider}.{requested_provider_voice_id}")
    return generated_assignment


def _validate_assignment_choice(generated_assignment: VoiceAssignment, *, requested_provider: str | None, requested_provider_voice_id: str | None, effective_assignment: VoiceAssignment, registry: Mapping[str, Any] | None, locked: bool, manual_override: bool) -> tuple[str, list[PlanValidationIssue]]:
    issues: list[PlanValidationIssue] = []
    if requested_provider or requested_provider_voice_id:
        if not requested_provider or not requested_provider_voice_id:
            issues.append(PlanValidationIssue(severity="error", path="requested_provider", code="provider-reference-invalid", message="requested provider and provider voice ID must both be present"))
        elif registry is not None:
            record = _registry_voice_record(registry, requested_provider, requested_provider_voice_id)
            if record is None:
                issues.append(PlanValidationIssue(severity="error", path="requested_provider", code="voice-missing", message=f"requested voice {requested_provider}::{requested_provider_voice_id} is not in the registry"))
            elif str(record.get("availability", "available")) == "unavailable":
                issues.append(PlanValidationIssue(severity="error", path="requested_provider", code="voice-unavailable", message=f"requested voice {record.get('voice_id', f'{requested_provider}.{requested_provider_voice_id}')} is unavailable"))
    if locked and not manual_override and requested_provider is None and requested_provider_voice_id is None:
        return "valid", issues
    if issues:
        return "unresolved", issues
    if requested_provider or requested_provider_voice_id:
        return "valid", issues
    if effective_assignment.provider == generated_assignment.provider and effective_assignment.provider_voice_id == generated_assignment.provider_voice_id:
        return "valid", issues
    return "valid", issues


def _coerce_edit_record(data: Mapping[str, Any]) -> PlanEditRecord:
    return PlanEditRecord(
        target_kind=str(data.get("target_kind", "character")),
        canonical_character_id=_optional_str(data.get("canonical_character_id")),
        previous_provider=_optional_str(data.get("previous_provider")),
        previous_provider_voice_id=_optional_str(data.get("previous_provider_voice_id")),
        requested_provider=_optional_str(data.get("requested_provider")),
        requested_provider_voice_id=_optional_str(data.get("requested_provider_voice_id")),
        effective_provider=_optional_str(data.get("effective_provider")),
        effective_provider_voice_id=_optional_str(data.get("effective_provider_voice_id")),
        lock_state_change=_optional_str(data.get("lock_state_change")),
        manual_override_change=_optional_str(data.get("manual_override_change")),
        timestamp=_optional_str(data.get("timestamp")),
        reason=_optional_str(data.get("reason")),
        user_note=_optional_str(data.get("user_note")),
        validation_result=_optional_str(data.get("validation_result")),
    )


def _coerce_issue(data: Mapping[str, Any]) -> PlanValidationIssue:
    return PlanValidationIssue(
        severity=str(data.get("severity", "error")),
        path=str(data.get("path", "")),
        code=str(data.get("code", "invalid")),
        message=str(data.get("message", "")),
    )


def _build_edit_record(previous: EditableAssignment, updated: EditableAssignment, override: ManualOverride, *, validation_result: str) -> PlanEditRecord:
    return PlanEditRecord(
        target_kind=override.target_kind,
        canonical_character_id=override.canonical_character_id,
        previous_provider=previous.effective_assignment.provider if previous.effective_assignment else previous.generated_assignment.provider,
        previous_provider_voice_id=previous.effective_assignment.provider_voice_id if previous.effective_assignment else previous.generated_assignment.provider_voice_id,
        requested_provider=override.requested_provider,
        requested_provider_voice_id=override.requested_provider_voice_id,
        effective_provider=updated.effective_assignment.provider if updated.effective_assignment else updated.generated_assignment.provider,
        effective_provider_voice_id=updated.effective_assignment.provider_voice_id if updated.effective_assignment else updated.generated_assignment.provider_voice_id,
        lock_state_change=f"{previous.locked}->{updated.locked}",
        manual_override_change=f"{previous.manual_override}->{updated.manual_override}",
        timestamp=override.timestamp,
        reason=override.override_reason,
        user_note=override.notes,
        validation_result=validation_result,
    )


def _apply_override_to_assignment(assignment: EditableAssignment, override: ManualOverride, *, registry: Mapping[str, Any] | None = None) -> EditableAssignment:
    requested_provider = override.requested_provider if override.requested_provider is not None else assignment.requested_provider
    requested_provider_voice_id = override.requested_provider_voice_id if override.requested_provider_voice_id is not None else assignment.requested_provider_voice_id
    manual_override = override.manual_override if override.manual_override is not None else assignment.manual_override
    locked = override.locked if override.locked is not None else assignment.locked
    notes = override.notes if override.notes is not None else assignment.notes
    reuse_permission = override.reuse_permission if override.reuse_permission is not None else assignment.reuse_permission
    separation_constraints = override.separation_constraints if override.separation_constraints else list(assignment.separation_constraints)
    updated = replace(
        assignment,
        requested_provider=requested_provider,
        requested_provider_voice_id=requested_provider_voice_id,
        locked=locked,
        manual_override=manual_override,
        user_modified=True,
        assignment_origin="user",
        notes=notes,
        override_reason=override.override_reason if override.override_reason is not None else assignment.override_reason,
        pronunciation_notes=override.pronunciation_notes if override.pronunciation_notes is not None else assignment.pronunciation_notes,
        casting_notes=override.casting_notes if override.casting_notes is not None else assignment.casting_notes,
        reuse_permission=reuse_permission,
        separation_constraints=separation_constraints,
    )
    effective_assignment = _effective_assignment_from_request(assignment.generated_assignment, requested_provider=requested_provider, requested_provider_voice_id=requested_provider_voice_id, registry=registry)
    validation_status, validation_issues = _validate_assignment_choice(
        assignment.generated_assignment,
        requested_provider=requested_provider,
        requested_provider_voice_id=requested_provider_voice_id,
        effective_assignment=effective_assignment,
        registry=registry,
        locked=locked,
        manual_override=manual_override,
    )
    return replace(updated, effective_assignment=effective_assignment, validation_status=validation_status, validation_issues=validation_issues)


def _locate_assignment(plan: EditableVoicePlan, target_kind: str, canonical_character_id: str | None) -> tuple[EditableAssignment, int | None]:
    if target_kind == "narrator":
        return plan.narrator, None
    for index, character in enumerate(plan.characters):
        if character.canonical_character_id == canonical_character_id:
            return character, index
    raise EditableVoicePlanError(f"unknown canonical character ID: {canonical_character_id}")


def _merge_previous_and_generated(previous: EditableVoicePlan, generated_plan: VoicePlan, *, registry: Mapping[str, Any] | None, allow_fallback: bool) -> EditableVoicePlan:
    active_records: list[EditableAssignment] = []
    edit_history = list(previous.edit_history)
    retired_assignments = list(previous.retired_assignments)
    applied_records: list[PlanEditRecord] = []
    preserved_records: list[PlanEditRecord] = []
    rejected_records: list[PlanEditRecord] = []
    removed_character_ids: list[str] = []

    narrator_generated = generated_plan.narrator.assignment
    if narrator_generated is None:
        raise EditableVoicePlanError("generated narrator assignment is missing")
    narrator = _merge_assignment(previous.narrator, "narrator", None, narrator_generated, generated_plan.narrator.rationale, registry=registry, allow_fallback=allow_fallback)
    if narrator.validation_status == "valid" and narrator.user_modified:
        applied_records.append(_build_edit_record(previous.narrator, narrator, ManualOverride(target_kind="narrator", requested_provider=narrator.requested_provider, requested_provider_voice_id=narrator.requested_provider_voice_id, locked=narrator.locked, manual_override=narrator.manual_override, notes=narrator.notes, override_reason=narrator.override_reason, pronunciation_notes=narrator.pronunciation_notes, casting_notes=narrator.casting_notes, reuse_permission=narrator.reuse_permission, separation_constraints=list(narrator.separation_constraints)), validation_result="applied"))
    elif not narrator.user_modified:
        preserved_records.append(_build_edit_record(previous.narrator, narrator, ManualOverride(target_kind="narrator"), validation_result="preserved"))

    prior_character_map = {character.canonical_character_id: character for character in previous.characters if character.canonical_character_id is not None}
    for character in generated_plan.characters:
        prior = prior_character_map.pop(character.canonical_character_id, None)
        generated_assignment = character.assignment
        if generated_assignment is None:
            raise EditableVoicePlanError(f"generated character assignment is missing for {character.canonical_character_id}")
        merged = _merge_assignment(prior, "character", character.canonical_character_id, generated_assignment, character.notes, canonical_name=character.canonical_name, registry=registry, allow_fallback=allow_fallback)
        active_records.append(merged)
        if prior is None:
            applied_records.append(_build_edit_record(merged, merged, ManualOverride(target_kind="character", canonical_character_id=character.canonical_character_id), validation_result="applied" if merged.user_modified else "preserved"))
        elif merged.user_modified:
            applied_records.append(_build_edit_record(prior, merged, ManualOverride(target_kind="character", canonical_character_id=character.canonical_character_id), validation_result="applied"))
        else:
            preserved_records.append(_build_edit_record(prior, merged, ManualOverride(target_kind="character", canonical_character_id=character.canonical_character_id), validation_result="preserved"))

    for removed_id, removed in prior_character_map.items():
        removed_character_ids.append(removed_id)
        record = PlanEditRecord(
            target_kind="character",
            canonical_character_id=removed_id,
            previous_provider=removed.effective_assignment.provider if removed.effective_assignment else removed.generated_assignment.provider,
            previous_provider_voice_id=removed.effective_assignment.provider_voice_id if removed.effective_assignment else removed.generated_assignment.provider_voice_id,
            effective_provider=None,
            effective_provider_voice_id=None,
            reason="removed from current roster",
            validation_result="retired",
        )
        edit_history.append(record)
        retired_assignments.append(record)

    merged = EditableVoicePlan(
        schema_version=EDITABLE_VOICE_PLAN_SCHEMA_VERSION,
        book_id=generated_plan.book_id,
        series_id=generated_plan.series_id,
        source_analysis_hash=generated_plan.source_analysis_hash,
        source_analysis_path=generated_plan.source_analysis_path,
        generated_plan=generated_plan,
        narrator=narrator,
        characters=sorted(active_records, key=lambda item: (item.canonical_character_id or "")),
        edit_history=_merge_history(edit_history, applied_records + preserved_records + rejected_records),
        retired_assignments=retired_assignments,
        user_notes=list(previous.user_notes),
        warnings=list(generated_plan.warnings),
        validation_issues=[],
        generated_content_hash=_hash_generated_plan(generated_plan),
        user_editable_hash="",
        effective_plan_hash="",
        generated_at=generated_plan.generated_at,
        generated_by=generated_plan.generated_by,
        source_voice_registry_hash=generated_plan.source_voice_registry_hash,
        source_series_bindings_hash=generated_plan.source_series_bindings_hash,
    )
    merged = _finalize_editable_plan(merged, generated_plan=generated_plan, registry=registry)
    return merged


def _merge_assignment(
    previous: EditableAssignment | None,
    target_kind: str,
    canonical_character_id: str | None,
    generated_assignment: VoiceAssignment,
    notes: str | None,
    *,
    canonical_name: str | None = None,
    registry: Mapping[str, Any] | None,
    allow_fallback: bool,
) -> EditableAssignment:
    base = _editable_assignment_from_generated(target_kind, canonical_character_id, generated_assignment, notes, canonical_name=canonical_name)
    if previous is None:
        return base
    if previous.user_modified:
        requested_provider = previous.requested_provider
        requested_provider_voice_id = previous.requested_provider_voice_id
        if not requested_provider and not requested_provider_voice_id and previous.locked and not previous.manual_override:
            previous_effective = previous.effective_assignment or previous.generated_assignment
            if _assignment_is_available(previous_effective, registry):
                return replace(
                    previous,
                    generated_assignment=generated_assignment,
                    canonical_name=canonical_name or previous.canonical_name,
                    effective_assignment=previous_effective,
                    validation_status="valid",
                    validation_issues=[],
                    user_modified=True,
                )
        effective_assignment = _effective_assignment_from_request(base.generated_assignment, requested_provider=requested_provider, requested_provider_voice_id=requested_provider_voice_id, registry=registry)
        validation_status, validation_issues = _validate_assignment_choice(base.generated_assignment, requested_provider=requested_provider, requested_provider_voice_id=requested_provider_voice_id, effective_assignment=effective_assignment, registry=registry, locked=previous.locked, manual_override=previous.manual_override)
        if not allow_fallback and validation_status != "valid":
            effective_assignment = base.generated_assignment
        return replace(
            previous,
            generated_assignment=generated_assignment,
            canonical_name=canonical_name or previous.canonical_name,
            effective_assignment=effective_assignment,
            validation_status=validation_status,
            validation_issues=validation_issues,
            user_modified=True,
        )
    if previous.locked:
        previous_effective = previous.effective_assignment or previous.generated_assignment
        if _assignment_is_available(previous_effective, registry):
            return replace(
                previous,
                generated_assignment=generated_assignment,
                canonical_name=canonical_name or previous.canonical_name,
                effective_assignment=previous_effective,
                validation_status="valid",
                validation_issues=[],
            )
        validation_issue = PlanValidationIssue(severity="error", path=_assignment_path(target_kind, canonical_character_id), code="voice-unavailable", message="locked generated assignment is no longer available")
        return replace(previous, generated_assignment=generated_assignment, canonical_name=canonical_name or previous.canonical_name, validation_status="unresolved", validation_issues=[validation_issue], effective_assignment=generated_assignment if allow_fallback else previous_effective)
    return base


def _assignment_is_available(assignment: VoiceAssignment, registry: Mapping[str, Any] | None) -> bool:
    if registry is None or not assignment.provider or not assignment.provider_voice_id:
        return True
    record = _registry_voice_record(registry, assignment.provider, assignment.provider_voice_id)
    return bool(record and str(record.get("availability", "available")) != "unavailable")


def _editable_assignment_from_generated(target_kind: str, canonical_character_id: str | None, generated_assignment: VoiceAssignment, notes: str | None, *, canonical_name: str | None = None) -> EditableAssignment:
    return EditableAssignment(
        target_kind=target_kind,
        canonical_character_id=canonical_character_id,
        canonical_name=canonical_name,
        generated_assignment=generated_assignment,
        requested_provider=None,
        requested_provider_voice_id=None,
        locked=generated_assignment.locked,
        manual_override=False,
        user_modified=False,
        assignment_origin="generated",
        notes=notes,
        override_reason=None,
        pronunciation_notes=None,
        casting_notes=None,
        reuse_permission=None,
        separation_constraints=[],
        effective_assignment=generated_assignment,
        validation_status="valid",
        validation_issues=[],
        edit_history=[],
    )


def _finalize_editable_plan(plan: EditableVoicePlan, *, generated_plan: VoicePlan, registry: Mapping[str, Any] | None = None) -> EditableVoicePlan:
    narrator = _finalize_assignment(plan.narrator, registry=registry)
    characters = [_finalize_assignment(character, registry=registry) for character in plan.characters]
    finalized = replace(plan, narrator=narrator, characters=characters, generated_plan=generated_plan)
    generated_content_hash = _hash_generated_plan(generated_plan)
    user_editable_hash = _hash_user_editable_plan(replace(finalized, generated_content_hash=generated_content_hash, user_editable_hash="", effective_plan_hash=""))
    effective_plan = _build_effective_voice_plan(replace(finalized, generated_content_hash=generated_content_hash, user_editable_hash=user_editable_hash, effective_plan_hash=""))
    effective_plan_hash = _hash_effective_plan(effective_plan)
    validation_issues = _validate_editable_payload(dataclass_to_dict(replace(finalized, generated_content_hash=generated_content_hash, user_editable_hash=user_editable_hash, effective_plan_hash=effective_plan_hash)), registry=registry)
    return replace(
        finalized,
        generated_content_hash=generated_content_hash,
        user_editable_hash=user_editable_hash,
        effective_plan_hash=effective_plan_hash,
        validation_issues=validation_issues,
    )


def _finalize_assignment(assignment: EditableAssignment, *, registry: Mapping[str, Any] | None = None) -> EditableAssignment:
    effective_assignment = assignment.effective_assignment or assignment.generated_assignment
    validation_status, validation_issues = _validate_assignment_choice(assignment.generated_assignment, requested_provider=assignment.requested_provider, requested_provider_voice_id=assignment.requested_provider_voice_id, effective_assignment=effective_assignment, registry=registry, locked=assignment.locked, manual_override=assignment.manual_override)
    return replace(assignment, effective_assignment=effective_assignment, validation_status=validation_status, validation_issues=validation_issues)


def _validate_editable_payload(payload: Mapping[str, Any], *, registry: Mapping[str, Any] | None = None) -> list[PlanValidationIssue]:
    issues: list[PlanValidationIssue] = []
    schema_version = payload.get("schema_version")
    if schema_version != EDITABLE_VOICE_PLAN_SCHEMA_VERSION:
        issues.append(PlanValidationIssue(severity="error", path="schema_version", code="unsupported-version", message=f"unsupported editable voice plan schema version: {schema_version!r}"))
        return issues
    generated_plan = payload.get("generated_plan")
    if not isinstance(generated_plan, Mapping):
        issues.append(PlanValidationIssue(severity="error", path="generated_plan", code="missing-generated-plan", message="editable voice plan must include generated_plan"))
        return issues
    generated_errors = validate_voice_plan(generated_plan)
    for error in generated_errors:
        issues.append(PlanValidationIssue(severity="error", path="generated_plan", code="generated-plan-invalid", message=error))
    editable = payload.get("editable")
    if not isinstance(editable, Mapping):
        editable = payload
    narrator = editable.get("narrator")
    if narrator is not None and not isinstance(narrator, Mapping):
        issues.append(PlanValidationIssue(severity="error", path="editable.narrator", code="invalid-narrator", message="editable narrator must be a mapping or null"))
    characters = editable.get("characters", [])
    if not isinstance(characters, Sequence) or isinstance(characters, (str, bytes)):
        issues.append(PlanValidationIssue(severity="error", path="editable.characters", code="invalid-characters", message="editable characters must be a sequence"))
        characters = []
    seen_ids: set[str] = set()
    generated_ids = {item.get("canonical_character_id") for item in generated_plan.get("characters", []) if isinstance(item, Mapping)}
    for idx, character in enumerate(characters):
        if not isinstance(character, Mapping):
            issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}]", code="invalid-entry", message="character edit must be a mapping"))
            continue
        char_id = character.get("canonical_character_id")
        if not isinstance(char_id, str) or not char_id:
            issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}].canonical_character_id", code="missing-character-id", message="character edit must include canonical_character_id"))
            continue
        if char_id in seen_ids:
            issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}].canonical_character_id", code="duplicate-character", message=f"duplicate editable character entry: {char_id}"))
        seen_ids.add(char_id)
        if char_id not in generated_ids:
            issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}].canonical_character_id", code="unknown-character", message=f"unknown canonical character ID: {char_id}"))
        requested_provider = character.get("requested_provider")
        requested_voice = character.get("requested_provider_voice_id")
        if requested_provider is not None and not isinstance(requested_provider, str):
            issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}].requested_provider", code="invalid-provider", message="requested provider must be a string or null"))
        if requested_voice is not None and not isinstance(requested_voice, str):
            issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}].requested_provider_voice_id", code="invalid-provider-voice", message="requested provider voice ID must be a string or null"))
        if registry is not None and isinstance(requested_provider, str) and isinstance(requested_voice, str):
            record = _registry_voice_record(registry, requested_provider, requested_voice)
            if record is None:
                issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}].requested_provider", code="voice-missing", message=f"requested voice {requested_provider}::{requested_voice} is not in the registry"))
            elif str(record.get("availability", "available")) == "unavailable":
                issues.append(PlanValidationIssue(severity="error", path=f"editable.characters[{idx}].requested_provider", code="voice-unavailable", message=f"requested voice {record.get('voice_id', f'{requested_provider}.{requested_voice}')} is unavailable"))
    if narrator is not None and isinstance(narrator, Mapping):
        requested_provider = narrator.get("requested_provider")
        requested_voice = narrator.get("requested_provider_voice_id")
        if requested_provider is not None and not isinstance(requested_provider, str):
            issues.append(PlanValidationIssue(severity="error", path="editable.narrator.requested_provider", code="invalid-provider", message="requested provider must be a string or null"))
        if requested_voice is not None and not isinstance(requested_voice, str):
            issues.append(PlanValidationIssue(severity="error", path="editable.narrator.requested_provider_voice_id", code="invalid-provider-voice", message="requested provider voice ID must be a string or null"))
        if registry is not None and isinstance(requested_provider, str) and isinstance(requested_voice, str):
            record = _registry_voice_record(registry, requested_provider, requested_voice)
            if record is None:
                issues.append(PlanValidationIssue(severity="error", path="editable.narrator.requested_provider", code="voice-missing", message=f"requested voice {requested_provider}::{requested_voice} is not in the registry"))
            elif str(record.get("availability", "available")) == "unavailable":
                issues.append(PlanValidationIssue(severity="error", path="editable.narrator.requested_provider", code="voice-unavailable", message=f"requested voice {record.get('voice_id', f'{requested_provider}.{requested_voice}')} is unavailable"))
    return issues


def _merge_history(existing: list[PlanEditRecord], new_records: list[PlanEditRecord]) -> list[PlanEditRecord]:
    return list(existing) + list(new_records)


def _hash_generated_plan(plan: VoicePlan) -> str:
    return sha256(canonical_json_dumps(plan).encode("utf-8")).hexdigest()


def _hash_user_editable_plan(plan: EditableVoicePlan) -> str:
    payload = {
        "book_id": plan.book_id,
        "series_id": plan.series_id,
        "source_analysis_hash": plan.source_analysis_hash,
        "source_analysis_path": plan.source_analysis_path,
        "narrator": _editable_assignment_payload(plan.narrator),
        "characters": [_editable_assignment_payload(character) for character in sorted(plan.characters, key=lambda item: item.canonical_character_id or "")],
        "edit_history": [dataclass_to_dict(record) for record in plan.edit_history],
        "retired_assignments": [dataclass_to_dict(record) for record in plan.retired_assignments],
        "user_notes": list(plan.user_notes),
        "warnings": list(plan.warnings),
    }
    return sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()


def _hash_effective_plan(plan: VoicePlan) -> str:
    return sha256(canonical_json_dumps(plan).encode("utf-8")).hexdigest()


def _editable_assignment_payload(assignment: EditableAssignment) -> dict[str, Any]:
    return {
        "target_kind": assignment.target_kind,
        "canonical_character_id": assignment.canonical_character_id,
        "canonical_name": assignment.canonical_name,
        "generated_assignment": dataclass_to_dict(assignment.generated_assignment),
        "requested_provider": assignment.requested_provider,
        "requested_provider_voice_id": assignment.requested_provider_voice_id,
        "locked": assignment.locked,
        "manual_override": assignment.manual_override,
        "assignment_origin": assignment.assignment_origin,
        "user_modified": assignment.user_modified,
        "notes": assignment.notes,
        "override_reason": assignment.override_reason,
        "pronunciation_notes": assignment.pronunciation_notes,
        "casting_notes": assignment.casting_notes,
        "reuse_permission": assignment.reuse_permission,
        "separation_constraints": list(assignment.separation_constraints),
        "effective_assignment": dataclass_to_dict(assignment.effective_assignment or assignment.generated_assignment),
        "validation_status": assignment.validation_status,
        "validation_issues": [dataclass_to_dict(issue) for issue in assignment.validation_issues],
        "edit_history": [dataclass_to_dict(record) for record in assignment.edit_history],
    }


def _assignment_path(target_kind: str, canonical_character_id: str | None) -> str:
    if target_kind == "narrator":
        return "editable.narrator"
    return f"editable.characters[{canonical_character_id or ''}]"


def _registry_voice_record(registry: Mapping[str, Any] | None, provider: str, provider_voice_id: str) -> Mapping[str, Any] | None:
    if registry is None:
        return {"provider": provider, "provider_voice_id": provider_voice_id, "availability": "available", "voice_id": f"{provider}.{provider_voice_id}"}
    voices = registry.get("voices", []) if isinstance(registry, Mapping) else []
    for voice in voices:
        if isinstance(voice, Mapping) and voice.get("provider") == provider and voice.get("provider_voice_id") == provider_voice_id:
            return voice
    return None


def _registry_voice_available(registry: Mapping[str, Any] | None, provider: str, provider_voice_id: str) -> bool:
    record = _registry_voice_record(registry, provider, provider_voice_id)
    return bool(record and str(record.get("availability", "available")) != "unavailable")


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _format_issues(issues: Sequence[PlanValidationIssue]) -> str:
    return "; ".join(f"{issue.path}: {issue.message}" for issue in issues)


def _assignment_name(target_kind: str, canonical_character_id: str | None) -> str:
    return "Narrator" if target_kind == "narrator" else canonical_character_id or "Unknown"
