from __future__ import annotations

import json

from app.voice_planner.schema import canonical_json_dumps, migrate_schema, validate_series_bindings


def test_series_bindings_schema_validation():
    valid = {
        "schema_version": 1,
        "series_id": "pendragon",
        "bindings": [],
        "narrator": None,
        "history": [],
        "updated_at": "2026-01-01T00:00:00Z",
    }
    assert validate_series_bindings(valid) == []


def test_canonical_json_dumps_is_stable_for_nested_mappings():
    payload = {
        "b": 2,
        "a": {"z": 1, "y": 2},
        "c": [
            {"b": 2, "a": 1},
            {"c": 3, "a": 1},
        ],
    }
    first = canonical_json_dumps(payload)
    second = canonical_json_dumps(payload)
    assert first == second
    assert json.loads(first)["a"]["y"] == 2


def test_schema_migration_rejects_unknown_versions():
    try:
        migrate_schema({"schema_version": 99}, "voice_plan")
    except ValueError as exc:
        assert "Unsupported schema migration" in str(exc)
    else:
        raise AssertionError("expected schema migration to fail")
