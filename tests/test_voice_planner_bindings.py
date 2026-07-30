from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.voice_planner import (
    BindingRegistryCheck,
    SeriesBindingError,
    binding_precedence,
    binding_registry_status,
    empty_series_bindings,
    get_character_binding,
    get_narrator_binding,
    load_series_bindings,
    record_reassignment,
    save_series_bindings,
    set_character_binding,
    set_narrator_binding,
    validate_against_registry,
)
from app.voice_planner.registry import load_voice_registry

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
BINDING_FIXTURE = FIXTURE_DIR / "series_bindings.sample.json"
REGISTRY_FIXTURE = FIXTURE_DIR / "voice_registry.sample.json"


def test_load_valid_series_bindings_and_lookup(tmp_path: Path):
    target = tmp_path / "series" / "pendragon" / "voices.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(BINDING_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    bindings = load_series_bindings(target)
    assert bindings.schema_version == 1
    assert bindings.series_id == "pendragon"
    assert get_narrator_binding(bindings).provider == "kokoro"
    assert get_character_binding(bindings, "bobby-pendragon").provider_voice_id == "nova"
    assert get_character_binding(bindings, "missing") is None
    assert [b.canonical_character_id for b in bindings.bindings] == ["bobby-pendragon", "courtney-chetwynde"]
    assert [entry.target_kind for entry in bindings.history] == ["narrator", "character"]
    assert binding_precedence(get_character_binding(bindings, "bobby-pendragon")) == "unlocked manual override"
    assert binding_precedence(get_narrator_binding(bindings)) == "locked manual override"


def test_empty_new_series_state_round_trips(tmp_path: Path):
    path = tmp_path / "series" / "new-series" / "voices.json"
    bindings = load_series_bindings(path)
    assert bindings.series_id == "new-series"
    assert bindings.narrator is None
    assert bindings.bindings == []
    assert bindings.history == []

    save_series_bindings(path, bindings)
    reloaded = load_series_bindings(path)
    assert reloaded == bindings
    assert path.read_text(encoding="utf-8").endswith("\n")


def test_missing_optional_fields_are_accepted(tmp_path: Path):
    path = tmp_path / "series" / "pendragon" / "voices.json"
    payload = {"schema_version": 1, "series_id": "pendragon", "narrator": None, "bindings": []}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    bindings = load_series_bindings(path)
    assert bindings.series_id == "pendragon"
    assert bindings.narrator is None
    assert bindings.bindings == []
    assert bindings.history == []
    save_series_bindings(path, bindings)
    assert load_series_bindings(path) == bindings


def test_save_and_reload_is_byte_identical_for_unchanged_bindings(tmp_path: Path):
    path = tmp_path / "series" / "pendragon" / "voices.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BINDING_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")

    first = load_series_bindings(path)
    save_series_bindings(path, first)
    bytes_after_first_save = path.read_bytes()
    second = load_series_bindings(path)
    save_series_bindings(path, second)
    bytes_after_second_save = path.read_bytes()

    assert bytes_after_first_save == bytes_after_second_save


def test_atomic_write_leaves_original_file_intact_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "series" / "pendragon" / "voices.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original = BINDING_FIXTURE.read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    bindings = load_series_bindings(path)

    replaced = {"called": False}

    def boom(src, dst):
        replaced["called"] = True
        raise OSError("replace failed")

    monkeypatch.setattr("app.voice_planner.bindings.os.replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        save_series_bindings(path, bindings)

    assert replaced["called"] is True
    assert path.read_text(encoding="utf-8") == original


def test_corruption_cases_fail_clearly(tmp_path: Path):
    path = tmp_path / "series" / "pendragon" / "voices.json"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SeriesBindingError, match="malformed JSON"):
        load_series_bindings(path)

    path.write_text(json.dumps({"schema_version": 99, "series_id": "pendragon"}), encoding="utf-8")
    with pytest.raises(SeriesBindingError, match="unsupported series bindings schema version"):
        load_series_bindings(path)

    duplicate = json.loads(BINDING_FIXTURE.read_text(encoding="utf-8"))
    duplicate["bindings"].append(dict(duplicate["bindings"][0]))
    path.write_text(json.dumps(duplicate), encoding="utf-8")
    with pytest.raises(SeriesBindingError, match="duplicate character bindings"):
        load_series_bindings(path)

    bad_history = json.loads(BINDING_FIXTURE.read_text(encoding="utf-8"))
    bad_history["history"] = [{"target_kind": "character"}]
    path.write_text(json.dumps(bad_history), encoding="utf-8")
    with pytest.raises(SeriesBindingError, match="history entry timestamp must be a non-empty string"):
        load_series_bindings(path)


def test_unavailable_voice_is_preserved_but_reported(tmp_path: Path):
    bindings = load_series_bindings(BINDING_FIXTURE)
    registry = load_voice_registry(REGISTRY_FIXTURE)

    narrator_status = binding_registry_status(get_narrator_binding(bindings), registry)
    assert isinstance(narrator_status, BindingRegistryCheck)
    assert narrator_status.available is False
    assert narrator_status.unavailable is True
    assert narrator_status.registry_voice_id == "kokoro.af_bella"
    assert get_narrator_binding(bindings).provider_voice_id == "af_bella"

    issues = validate_against_registry(bindings, registry)
    assert any("unavailable voice" in issue for issue in issues)


def test_record_reassignment_preserves_history_and_sorting(tmp_path: Path):
    bindings = empty_series_bindings("pendragon")
    narrator = set_narrator_binding(
        bindings,
        {
            "target_kind": "narrator",
            "provider": "openai",
            "provider_voice_id": "nova",
            "voice_id": "openai.nova",
            "locked": False,
            "manual_override": False,
            "inherited": True,
            "assignment_timestamp": "2026-07-29T23:30:00Z",
        },
    )
    character = set_character_binding(
        narrator,
        {
            "target_kind": "character",
            "canonical_character_id": "bobby-pendragon",
            "provider": "elevenlabs",
            "provider_voice_id": "rachel",
            "voice_id": "elevenlabs.rachel",
            "locked": True,
            "manual_override": True,
            "inherited": False,
            "assignment_timestamp": "2026-07-29T23:31:00Z",
        },
    )
    updated = record_reassignment(
        character,
        target_kind="character",
        canonical_character_id="bobby-pendragon",
        previous_provider="elevenlabs",
        previous_provider_voice_id="rachel",
        new_provider="openai",
        new_provider_voice_id="nova",
        timestamp="2026-07-29T23:32:00Z",
        reason="manual refresh",
        source="manual",
        prior_locked=True,
        manual_change=True,
    )

    latest = get_character_binding(updated, "bobby-pendragon")
    assert latest is not None
    assert latest.provider == "elevenlabs"
    assert latest.history[-1].previous_provider == "elevenlabs"
    assert latest.history[-1].new_provider == "openai"
    assert latest.history[-1].target_kind == "character"
    assert updated.history[-1].timestamp == "2026-07-29T23:32:00Z"

    path = tmp_path / "series" / "pendragon" / "voices.json"
    save_series_bindings(path, updated)
    round_tripped = load_series_bindings(path)
    assert round_tripped == updated


def test_manual_override_and_lock_state_are_preserved(tmp_path: Path):
    bindings = load_series_bindings(BINDING_FIXTURE)
    narrator = get_narrator_binding(bindings)
    bobby = get_character_binding(bindings, "bobby-pendragon")

    assert narrator.manual_override is True
    assert narrator.locked is True
    assert narrator.user_notes.startswith("Keep as the series narrator")
    assert narrator.provenance.source == "manual"
    assert bobby.manual_override is True
    assert bobby.locked is False
    assert bobby.user_notes == "Use for Bobby in the current book."
    assert bobby.assignment_confidence == pytest.approx(0.88)


def test_setters_reject_bad_input(tmp_path: Path):
    bindings = empty_series_bindings("pendragon")
    with pytest.raises(SeriesBindingError, match="character binding requires canonical_character_id"):
        set_character_binding(
            bindings,
            {
                "target_kind": "character",
                "provider": "openai",
                "provider_voice_id": "nova",
            },
        )
    mismatch = tmp_path / "series" / "pendragon" / "voices.json"
    mismatch.parent.mkdir(parents=True, exist_ok=True)
    mismatch.write_text(json.dumps({"schema_version": 1, "series_id": "pendragon", "narrator": None, "bindings": []}), encoding="utf-8")
    with pytest.raises(SeriesBindingError, match="series_id mismatch"):
        load_series_bindings(mismatch, series_id="other-series")
