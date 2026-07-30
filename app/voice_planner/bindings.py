from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping, Sequence

from .models import AssignmentProvenance, ReassignmentHistoryEntry, SeriesBindings, SeriesVoiceBinding, dataclass_to_dict
from .registry import is_voice_selectable, voice_registry_key
from .schema import SCHEMA_VERSIONS, canonical_json_dumps, validate_series_bindings

_SERIES_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_TARGET_KINDS = {"narrator", "character"}
_PRECEDENCE_ORDER = {
    (True, True, "manual"): "locked manual override",
    (False, True, "manual"): "unlocked manual override",
    (True, False, "inherited"): "locked inherited series binding",
    (False, False, "inherited"): "inherited series binding",
}


@dataclass(frozen=True)
class BindingRegistryCheck:
    target_kind: str
    canonical_character_id: str | None
    provider: str | None
    provider_voice_id: str | None
    voice_id: str | None
    registry_voice_id: str | None
    registry_availability: str | None
    available: bool
    unavailable: bool
    reported_unavailable: bool
    notes: str | None = None


class SeriesBindingError(ValueError):
    pass


def empty_series_bindings(series_id: str, *, updated_at: str | None = None) -> SeriesBindings:
    _validate_series_id(series_id)
    return SeriesBindings(schema_version=SCHEMA_VERSIONS["series_bindings"], series_id=series_id, narrator=None, bindings=[], history=[], updated_at=updated_at)


def load_series_bindings(path: str | Path, *, series_id: str | None = None) -> SeriesBindings:
    binding_path = Path(path)
    if not binding_path.exists():
        resolved_series_id = series_id or _series_id_from_path(binding_path)
        return empty_series_bindings(resolved_series_id)

    try:
        raw_text = binding_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeriesBindingError(f"unable to read series bindings file {binding_path}: {exc.strerror or exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SeriesBindingError(
            f"malformed JSON in series bindings file {binding_path.name}: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc

    if not isinstance(raw, dict):
        raise SeriesBindingError("series bindings file must contain a JSON object")

    schema_version = raw.get("schema_version")
    if schema_version != SCHEMA_VERSIONS["series_bindings"]:
        raise SeriesBindingError(f"unsupported series bindings schema version: {schema_version!r}")

    errors = validate_series_bindings(raw)
    if errors:
        raise SeriesBindingError("; ".join(errors))

    resolved_series_id = _validate_series_id(raw.get("series_id"))
    if series_id is not None and series_id != resolved_series_id:
        raise SeriesBindingError(f"series_id mismatch: expected {series_id!r} but file contains {resolved_series_id!r}")

    narrator = _coerce_binding(raw.get("narrator"), allow_none=True)
    bindings = [_coerce_binding(item) for item in raw.get("bindings", [])]
    history = [_coerce_history_entry(item) for item in raw.get("history", [])] if isinstance(raw.get("history"), list) else []

    if narrator is not None:
        narrator = _normalize_binding(narrator, resolved_series_id)
    normalized_bindings = [_normalize_binding(binding, resolved_series_id) for binding in bindings]
    normalized_history = _normalize_history(history)

    if narrator is not None and narrator.target_kind != "narrator":
        raise SeriesBindingError("narrator binding must have target_kind 'narrator'")
    if any(binding.target_kind != "character" for binding in normalized_bindings):
        raise SeriesBindingError("character bindings must have target_kind 'character'")

    duplicate_ids = _duplicate_character_ids(normalized_bindings)
    if duplicate_ids:
        raise SeriesBindingError(f"duplicate character bindings: {', '.join(sorted(duplicate_ids))}")

    return SeriesBindings(
        schema_version=SCHEMA_VERSIONS["series_bindings"],
        series_id=resolved_series_id,
        narrator=narrator,
        bindings=normalized_bindings,
        history=normalized_history,
        updated_at=_optional_str(raw.get("updated_at")),
    )


def save_series_bindings(path: str | Path, bindings: SeriesBindings | Mapping[str, Any]) -> None:
    binding_path = Path(path)
    binding_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _coerce_series_bindings(bindings)
    errors = validate_series_bindings(dataclass_to_dict(normalized))
    if errors:
        raise SeriesBindingError("; ".join(errors))
    _validate_series_id(normalized.series_id)

    payload = canonical_json_dumps(normalized) + "\n"
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=binding_path.parent, prefix=f".{binding_path.name}.", suffix=".tmp", delete=False) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, binding_path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def get_narrator_binding(bindings: SeriesBindings | Mapping[str, Any]) -> SeriesVoiceBinding | None:
    state = _coerce_series_bindings(bindings)
    return state.narrator


def get_character_binding(bindings: SeriesBindings | Mapping[str, Any], canonical_character_id: str) -> SeriesVoiceBinding | None:
    state = _coerce_series_bindings(bindings)
    for binding in state.bindings:
        if binding.canonical_character_id == canonical_character_id:
            return binding
    return None


def set_narrator_binding(
    bindings: SeriesBindings | Mapping[str, Any],
    binding: SeriesVoiceBinding | Mapping[str, Any],
    *,
    history_entry: ReassignmentHistoryEntry | Mapping[str, Any] | None = None,
    updated_at: str | None = None,
) -> SeriesBindings:
    state = _coerce_series_bindings(bindings)
    coerced = _normalize_binding(_coerce_binding(binding), state.series_id)
    if coerced.target_kind != "narrator":
        raise SeriesBindingError("narrator binding must have target_kind 'narrator'")
    return _replace_binding(state, coerced, history_entry=history_entry, updated_at=updated_at)


def set_character_binding(
    bindings: SeriesBindings | Mapping[str, Any],
    binding: SeriesVoiceBinding | Mapping[str, Any],
    *,
    history_entry: ReassignmentHistoryEntry | Mapping[str, Any] | None = None,
    updated_at: str | None = None,
) -> SeriesBindings:
    state = _coerce_series_bindings(bindings)
    coerced = _normalize_binding(_coerce_binding(binding), state.series_id)
    if coerced.target_kind != "character":
        raise SeriesBindingError("character binding must have target_kind 'character'")
    if not coerced.canonical_character_id:
        raise SeriesBindingError("character binding requires canonical_character_id")
    return _replace_binding(state, coerced, history_entry=history_entry, updated_at=updated_at)


def record_reassignment(
    bindings: SeriesBindings | Mapping[str, Any],
    *,
    target_kind: str,
    canonical_character_id: str | None = None,
    previous_provider: str | None = None,
    previous_provider_voice_id: str | None = None,
    new_provider: str | None = None,
    new_provider_voice_id: str | None = None,
    timestamp: str,
    reason: str | None = None,
    source: str | None = None,
    prior_locked: bool | None = None,
    manual_change: bool | None = None,
    updated_at: str | None = None,
) -> SeriesBindings:
    state = _coerce_series_bindings(bindings)
    history_entry = ReassignmentHistoryEntry(
        target_kind=target_kind,
        canonical_character_id=canonical_character_id,
        previous_provider=previous_provider,
        previous_provider_voice_id=previous_provider_voice_id,
        new_provider=new_provider,
        new_provider_voice_id=new_provider_voice_id,
        timestamp=timestamp,
        reason=reason,
        source=source,
        prior_locked=prior_locked,
        manual_change=manual_change,
    )
    if target_kind == "narrator":
        current = state.narrator
        if current is None:
            raise SeriesBindingError("cannot record narrator reassignment without an existing narrator binding")
        return replace(state, narrator=_append_history(current, history_entry), history=_sorted_history([*state.history, history_entry]), updated_at=updated_at or state.updated_at)
    if target_kind != "character":
        raise SeriesBindingError("target_kind must be 'narrator' or 'character'")
    if not canonical_character_id:
        raise SeriesBindingError("character reassignment requires canonical_character_id")
    updated: list[SeriesVoiceBinding] = []
    matched = False
    for item in state.bindings:
        if item.canonical_character_id == canonical_character_id:
            updated.append(_append_history(item, history_entry))
            matched = True
        else:
            updated.append(item)
    if not matched:
        raise SeriesBindingError(f"unknown canonical_character_id: {canonical_character_id}")
    return replace(state, bindings=_sorted_bindings(updated), history=_sorted_history([*state.history, history_entry]), updated_at=updated_at or state.updated_at)


def validate_against_registry(bindings: SeriesBindings | Mapping[str, Any], registry: Mapping[str, Any]) -> list[str]:
    state = _coerce_series_bindings(bindings)
    _registry_index(registry)
    issues: list[str] = []

    for binding in _iter_bindings(state):
        if not binding.provider or not binding.provider_voice_id:
            issues.append(_binding_label(binding) + " missing provider reference")
            continue
        status = binding_registry_status(binding, registry)
        if status.registry_voice_id is None:
            issues.append(f"{_binding_label(binding)} references unknown voice {binding.provider}::{binding.provider_voice_id}")
        elif not status.available:
            issues.append(f"{_binding_label(binding)} references unavailable voice {status.registry_voice_id} ({status.registry_availability})")
    return issues


def binding_registry_status(binding: SeriesVoiceBinding | Mapping[str, Any], registry: Mapping[str, Any]) -> BindingRegistryCheck:
    coerced = _normalize_binding(_coerce_binding(binding), None)
    registry_index = _registry_index(registry)
    key = (coerced.provider or "", coerced.provider_voice_id or "")
    registry_voice = registry_index.get(key)
    if registry_voice is None:
        return BindingRegistryCheck(
            target_kind=coerced.target_kind,
            canonical_character_id=coerced.canonical_character_id,
            provider=coerced.provider,
            provider_voice_id=coerced.provider_voice_id,
            voice_id=coerced.voice_id,
            registry_voice_id=None,
            registry_availability=None,
            available=False,
            unavailable=True,
            reported_unavailable=True,
            notes="unknown registry voice",
        )
    availability = registry_voice.availability if hasattr(registry_voice, "availability") else registry_voice.get("availability")
    selectable = is_voice_selectable(registry_voice)
    registry_voice_id = registry_voice.voice_id if hasattr(registry_voice, "voice_id") else registry_voice.get("voice_id")
    return BindingRegistryCheck(
        target_kind=coerced.target_kind,
        canonical_character_id=coerced.canonical_character_id,
        provider=coerced.provider,
        provider_voice_id=coerced.provider_voice_id,
        voice_id=coerced.voice_id,
        registry_voice_id=str(registry_voice_id) if registry_voice_id is not None else None,
        registry_availability=str(availability) if availability is not None else None,
        available=selectable,
        unavailable=not selectable,
        reported_unavailable=not selectable,
        notes=None,
    )


def binding_precedence(binding: SeriesVoiceBinding | Mapping[str, Any] | None) -> str:
    if binding is None:
        return "no binding"
    coerced = _normalize_binding(_coerce_binding(binding), None)
    if coerced.manual_override and coerced.locked:
        return "locked manual override"
    if coerced.manual_override and not coerced.locked:
        return "unlocked manual override"
    if coerced.inherited and coerced.locked:
        return "locked inherited series binding"
    if coerced.inherited:
        return "inherited series binding"
    return "no binding"


def serialize_series_bindings(bindings: SeriesBindings | Mapping[str, Any]) -> str:
    return canonical_json_dumps(_coerce_series_bindings(bindings))


def _replace_binding(
    state: SeriesBindings,
    new_binding: SeriesVoiceBinding,
    *,
    history_entry: ReassignmentHistoryEntry | Mapping[str, Any] | None,
    updated_at: str | None,
) -> SeriesBindings:
    history_obj = _coerce_history_entry(history_entry) if history_entry is not None else None
    if new_binding.target_kind == "narrator":
        current = state.narrator
        if current is not None and _binding_identity(current) == _binding_identity(new_binding):
            return replace(state, narrator=new_binding, updated_at=updated_at or state.updated_at)
        if current is not None:
            history_obj = history_obj or _history_from_transition(current, new_binding)
            new_binding = _append_history(new_binding, history_obj)
            merged_history = _sorted_history([*state.history, history_obj])
        else:
            merged_history = state.history
        return replace(state, narrator=new_binding, history=merged_history, updated_at=updated_at or state.updated_at)

    updated: list[SeriesVoiceBinding] = []
    matched = False
    merged_history = list(state.history)
    for item in state.bindings:
        if item.canonical_character_id == new_binding.canonical_character_id:
            matched = True
            if _binding_identity(item) == _binding_identity(new_binding):
                updated.append(new_binding)
            else:
                history_obj = history_obj or _history_from_transition(item, new_binding)
                updated.append(_append_history(new_binding, history_obj))
                merged_history.append(history_obj)
        else:
            updated.append(item)
    if not matched:
        updated.append(new_binding)
    return replace(state, bindings=_sorted_bindings(updated), history=_sorted_history(merged_history), updated_at=updated_at or state.updated_at)


def _history_from_transition(previous: SeriesVoiceBinding, new: SeriesVoiceBinding) -> ReassignmentHistoryEntry:
    return ReassignmentHistoryEntry(
        target_kind=new.target_kind,
        canonical_character_id=new.canonical_character_id,
        previous_provider=previous.provider,
        previous_provider_voice_id=previous.provider_voice_id,
        new_provider=new.provider,
        new_provider_voice_id=new.provider_voice_id,
        timestamp=new.assignment_timestamp or "",
        reason=new.assignment_reason,
        source=_binding_source(new),
        prior_locked=previous.locked,
        manual_change=new.manual_override,
    )


def _append_history(binding: SeriesVoiceBinding, entry: ReassignmentHistoryEntry) -> SeriesVoiceBinding:
    return replace(binding, history=_sorted_history([*binding.history, entry]))


def _binding_identity(binding: SeriesVoiceBinding) -> tuple[Any, ...]:
    return (
        binding.target_kind,
        binding.canonical_character_id,
        binding.provider,
        binding.provider_voice_id,
        binding.voice_id,
        binding.locked,
        binding.manual_override,
        binding.inherited,
        binding.assignment_confidence,
        binding.assignment_reason,
        binding.assignment_timestamp,
        _provenance_identity(binding.provenance),
        binding.user_notes,
        binding.unavailable,
    )


def _provenance_identity(provenance: AssignmentProvenance | Mapping[str, Any] | None) -> tuple[Any, ...] | None:
    if provenance is None:
        return None
    if isinstance(provenance, AssignmentProvenance):
        return (provenance.source, provenance.reason, provenance.basis, tuple(provenance.selected_from), provenance.score, provenance.tie_breaker)
    if isinstance(provenance, Mapping):
        return (
            provenance.get("source"),
            provenance.get("reason"),
            provenance.get("basis"),
            tuple(provenance.get("selected_from", []) if isinstance(provenance.get("selected_from"), list) else []),
            provenance.get("score"),
            provenance.get("tie_breaker"),
        )
    return None


def _binding_source(binding: SeriesVoiceBinding) -> str:
    return "manual" if binding.manual_override else "inherited"


def _iter_bindings(state: SeriesBindings) -> list[SeriesVoiceBinding]:
    items = []
    if state.narrator is not None:
        items.append(state.narrator)
    items.extend(state.bindings)
    return items


def _sorted_bindings(bindings: Sequence[SeriesVoiceBinding]) -> list[SeriesVoiceBinding]:
    narrator = [binding for binding in bindings if binding.target_kind == "narrator"]
    characters = [binding for binding in bindings if binding.target_kind == "character"]
    narrator_sorted = sorted(narrator, key=lambda item: (_binding_sort_key(item)))
    character_sorted = sorted(characters, key=lambda item: (_binding_sort_key(item)))
    return [*narrator_sorted, *character_sorted]


def _sorted_history(history: Sequence[ReassignmentHistoryEntry]) -> list[ReassignmentHistoryEntry]:
    return sorted(history, key=_history_sort_key)


def _binding_sort_key(binding: SeriesVoiceBinding) -> tuple[Any, ...]:
    return (
        binding.target_kind,
        binding.canonical_character_id or "",
        binding.provider or "",
        binding.provider_voice_id or "",
        binding.assignment_timestamp or "",
        binding.assignment_reason or "",
    )


def _history_sort_key(entry: ReassignmentHistoryEntry) -> tuple[Any, ...]:
    return (
        entry.timestamp,
        entry.target_kind,
        entry.canonical_character_id or "",
        entry.previous_provider or "",
        entry.previous_provider_voice_id or "",
        entry.new_provider or "",
        entry.new_provider_voice_id or "",
        entry.reason or "",
        entry.source or "",
        entry.prior_locked if entry.prior_locked is not None else False,
        entry.manual_change if entry.manual_change is not None else False,
    )


def _normalize_binding(binding: SeriesVoiceBinding, series_id: str | None) -> SeriesVoiceBinding:
    if binding.target_kind not in _TARGET_KINDS:
        raise SeriesBindingError(f"invalid target_kind: {binding.target_kind!r}")
    if binding.target_kind == "character" and not binding.canonical_character_id:
        raise SeriesBindingError("character binding requires canonical_character_id")
    if binding.target_kind == "narrator" and binding.canonical_character_id is not None:
        raise SeriesBindingError("narrator binding must not include canonical_character_id")
    if binding.provider is None or not str(binding.provider).strip():
        raise SeriesBindingError(f"{_binding_label(binding)} missing provider")
    if binding.provider_voice_id is None or not str(binding.provider_voice_id).strip():
        raise SeriesBindingError(f"{_binding_label(binding)} missing provider_voice_id")
    if not isinstance(binding.locked, bool):
        raise SeriesBindingError(f"{_binding_label(binding)} locked must be a boolean")
    if not isinstance(binding.manual_override, bool):
        raise SeriesBindingError(f"{_binding_label(binding)} manual_override must be a boolean")
    if not isinstance(binding.inherited, bool):
        raise SeriesBindingError(f"{_binding_label(binding)} inherited must be a boolean")
    if not isinstance(binding.unavailable, bool):
        raise SeriesBindingError(f"{_binding_label(binding)} unavailable must be a boolean")
    if binding.assignment_confidence is not None and not isinstance(binding.assignment_confidence, (int, float)):
        raise SeriesBindingError(f"{_binding_label(binding)} assignment_confidence must be numeric or null")
    if binding.assignment_timestamp is not None and not isinstance(binding.assignment_timestamp, str):
        raise SeriesBindingError(f"{_binding_label(binding)} assignment_timestamp must be a string or null")
    if binding.assignment_reason is not None and not isinstance(binding.assignment_reason, str):
        raise SeriesBindingError(f"{_binding_label(binding)} assignment_reason must be a string or null")
    if binding.user_notes is not None and not isinstance(binding.user_notes, str):
        raise SeriesBindingError(f"{_binding_label(binding)} user_notes must be a string or null")
    return replace(binding, history=_sorted_history(binding.history), provenance=_normalize_provenance(binding.provenance))


def _normalize_provenance(provenance: AssignmentProvenance | Mapping[str, Any] | None) -> AssignmentProvenance | None:
    if provenance is None:
        return None
    if isinstance(provenance, AssignmentProvenance):
        return provenance
    if not isinstance(provenance, Mapping):
        raise SeriesBindingError("assignment provenance must be a mapping or AssignmentProvenance")
    selected_from = provenance.get("selected_from", [])
    if selected_from is None:
        selected_from = []
    if not isinstance(selected_from, Sequence) or isinstance(selected_from, (str, bytes)):
        raise SeriesBindingError("assignment provenance selected_from must be a sequence")
    return AssignmentProvenance(
        source=_required_str(provenance, "source", "assignment provenance"),
        reason=_required_str(provenance, "reason", "assignment provenance"),
        basis=_required_str(provenance, "basis", "assignment provenance"),
        selected_from=[str(item) for item in selected_from],
        score=_optional_float(provenance.get("score")),
        tie_breaker=_optional_str(provenance.get("tie_breaker")),
    )


def _coerce_series_bindings(bindings: SeriesBindings | Mapping[str, Any]) -> SeriesBindings:
    if isinstance(bindings, SeriesBindings):
        return bindings
    if not isinstance(bindings, Mapping):
        raise SeriesBindingError("series bindings must be a mapping or SeriesBindings")
    narrator = _coerce_binding(bindings.get("narrator"), allow_none=True)
    narrator = _normalize_binding(narrator, _optional_str(bindings.get("series_id"))) if narrator is not None else None
    character_bindings = [_normalize_binding(_coerce_binding(item), _optional_str(bindings.get("series_id"))) for item in bindings.get("bindings", []) if item is not None]
    return SeriesBindings(
        schema_version=int(bindings.get("schema_version", SCHEMA_VERSIONS["series_bindings"])),
        series_id=_required_str(bindings, "series_id", "series bindings"),
        narrator=narrator,
        bindings=_sorted_bindings(character_bindings),
        history=_sorted_history([_coerce_history_entry(entry) for entry in bindings.get("history", []) if entry is not None]),
        updated_at=_optional_str(bindings.get("updated_at")),
    )


def _coerce_binding(value: SeriesVoiceBinding | Mapping[str, Any] | None, *, allow_none: bool = False) -> SeriesVoiceBinding | None:
    if value is None:
        if allow_none:
            return None
        raise SeriesBindingError("binding cannot be null")
    if isinstance(value, SeriesVoiceBinding):
        return value
    if not isinstance(value, Mapping):
        raise SeriesBindingError("binding must be a mapping or SeriesVoiceBinding")
    provenance = value.get("provenance")
    history = value.get("history", [])
    if history is None:
        history = []
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise SeriesBindingError("binding history must be a sequence")
    target_kind = _required_str(value, "target_kind", "binding")
    return SeriesVoiceBinding(
        target_kind=target_kind,
        canonical_character_id=_optional_str(value.get("canonical_character_id")),
        provider=_optional_str(value.get("provider")),
        provider_voice_id=_optional_str(value.get("provider_voice_id")),
        voice_id=_optional_str(value.get("voice_id")),
        locked=value.get("locked", False),
        manual_override=value.get("manual_override", False),
        inherited=value.get("inherited", True),
        assignment_confidence=_optional_float(value.get("assignment_confidence")),
        assignment_reason=_optional_str(value.get("assignment_reason")),
        assignment_timestamp=_optional_str(value.get("assignment_timestamp")),
        provenance=provenance,  # normalized later
        user_notes=_optional_str(value.get("user_notes")),
        unavailable=value.get("unavailable", False),
        history=[_coerce_history_entry(entry) for entry in history if entry is not None],
    )


def _coerce_history_entry(value: ReassignmentHistoryEntry | Mapping[str, Any]) -> ReassignmentHistoryEntry:
    if isinstance(value, ReassignmentHistoryEntry):
        return value
    if not isinstance(value, Mapping):
        raise SeriesBindingError("history entry must be a mapping or ReassignmentHistoryEntry")
    return ReassignmentHistoryEntry(
        target_kind=_required_str(value, "target_kind", "history entry"),
        canonical_character_id=_optional_str(value.get("canonical_character_id")),
        previous_provider=_optional_str(value.get("previous_provider")),
        previous_provider_voice_id=_optional_str(value.get("previous_provider_voice_id")),
        new_provider=_optional_str(value.get("new_provider")),
        new_provider_voice_id=_optional_str(value.get("new_provider_voice_id")),
        timestamp=_required_str(value, "timestamp", "history entry"),
        reason=_optional_str(value.get("reason")),
        source=_optional_str(value.get("source")),
        prior_locked=_optional_bool(value.get("prior_locked")),
        manual_change=_optional_bool(value.get("manual_change")),
    )


def _normalize_history(history: Sequence[ReassignmentHistoryEntry]) -> list[ReassignmentHistoryEntry]:
    return _sorted_history(history)


def _required_str(data: Mapping[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SeriesBindingError(f"{label} {key} must be a non-empty string")
    return value.strip()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SeriesBindingError("string fields must be strings or null")
    return value


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise SeriesBindingError("history entry boolean fields must be booleans")
    return value


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise SeriesBindingError("numeric fields must be numeric")
    return float(value)


def _duplicate_character_ids(bindings: Sequence[SeriesVoiceBinding]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for binding in bindings:
        if binding.canonical_character_id is None:
            continue
        if binding.canonical_character_id in seen:
            duplicates.append(binding.canonical_character_id)
        seen.add(binding.canonical_character_id)
    return duplicates


def _validate_series_id(series_id: Any) -> str:
    if not isinstance(series_id, str) or not series_id.strip():
        raise SeriesBindingError("series_id must be a non-empty string")
    normalized = series_id.strip()
    if not _SERIES_ID_PATTERN.fullmatch(normalized):
        raise SeriesBindingError(f"series_id must match {_SERIES_ID_PATTERN.pattern}: {normalized!r}")
    return normalized


def _series_id_from_path(path: Path) -> str:
    if path.name == "voices.json" and path.parent.name:
        return _validate_series_id(path.parent.name)
    if path.suffix:
        return _validate_series_id(path.stem)
    return _validate_series_id(path.name)


def _binding_label(binding: SeriesVoiceBinding) -> str:
    if binding.target_kind == "narrator":
        return "narrator binding"
    return f"character binding {binding.canonical_character_id or '<missing>'}"


def _registry_index(registry: Mapping[str, Any]) -> dict[tuple[str, str], Any]:
    voices = registry.get("voices") if isinstance(registry, Mapping) else None
    if not isinstance(voices, Sequence) or isinstance(voices, (str, bytes)):
        raise SeriesBindingError("registry must contain a voices sequence")
    index: dict[tuple[str, str], Any] = {}
    for voice in voices:
        provider, provider_voice_id = voice_registry_key(voice)
        index[(provider, provider_voice_id)] = voice
    return index
