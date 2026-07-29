from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.voice_planner.registry import dump_voice_registry, is_voice_selectable, load_voice_registry, selectable_voices
from app.voice_planner.schema import SchemaValidationError

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "voice_registry.sample.json"


def test_voice_registry_loads_and_preserves_metadata():
    registry = load_voice_registry(FIXTURE)
    assert registry["schema_version"] == 1
    assert registry["registry_version"] == "2026-07-29"
    assert [voice.provider for voice in registry["voices"]] == ["elevenlabs", "kokoro", "openai"]
    assert [voice.provider_voice_id for voice in registry["voices"]] == ["rachel", "af_bella", "nova"]

    available = selectable_voices(registry)
    assert [voice.voice_id for voice in available] == ["elevenlabs.rachel", "openai.nova"]
    assert registry["voices"][1].availability == "unavailable"
    assert is_voice_selectable(registry["voices"][1]) is False

    kokoro_voice = registry["voices"][1]
    assert kokoro_voice.provider == "kokoro"
    assert kokoro_voice.provider_voice_id == "af_bella"
    assert kokoro_voice.supported_controls == ["pitch", "rate", "volume"]
    assert kokoro_voice.supported_languages == ["en-US"]
    assert kokoro_voice.licensing_information == "Kokoro model terms"
    assert kokoro_voice.latency_estimate_ms == 480
    assert kokoro_voice.sample_rate_hz == 24000
    assert kokoro_voice.similarity_cluster == "cluster-k1"
    assert kokoro_voice.base_priority == 90


def test_voice_registry_serialization_is_deterministic():
    registry = load_voice_registry(FIXTURE)
    first = dump_voice_registry(registry)
    second = dump_voice_registry(registry)
    assert first == second
    assert json.loads(first)["voices"][0]["provider"] == "elevenlabs"


def test_duplicate_registry_keys_are_rejected(tmp_path: Path):
    registry_path = tmp_path / "duplicate.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "registry_version": "2026-07-29",
                "voices": [
                    {
                        "schema_version": 1,
                        "voice_id": "voice-a",
                        "provider": "kokoro",
                        "provider_voice_id": "shared",
                        "display_name": "Voice A",
                        "availability": "available",
                        "quality_score": 0.8,
                        "base_priority": 10,
                    },
                    {
                        "schema_version": 1,
                        "voice_id": "voice-b",
                        "provider": "kokoro",
                        "provider_voice_id": "shared",
                        "display_name": "Voice B",
                        "availability": "available",
                        "quality_score": 0.7,
                        "base_priority": 10,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SchemaValidationError, match="duplicate voice registry key"):
        load_voice_registry(registry_path)


def test_invalid_metadata_reports_clear_errors(tmp_path: Path):
    registry_path = tmp_path / "invalid.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "registry_version": "2026-07-29",
                "voices": [
                    {
                        "schema_version": 1,
                        "voice_id": "bad.voice",
                        "provider": "openai",
                        "provider_voice_id": "bad",
                        "display_name": "Bad Voice",
                        "availability": "available",
                        "quality_score": "high",
                        "latency_estimate_ms": "fast",
                        "sample_rate_hz": 24000,
                        "supported_languages": ["en-US"],
                        "supported_controls": "rate",
                        "licensing_information": 123,
                        "base_priority": "90",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SchemaValidationError) as excinfo:
        load_voice_registry(registry_path)
    message = str(excinfo.value)
    assert "quality_score must be numeric" in message
    assert "latency_estimate_ms must be an integer or null" in message
    assert "supported_controls must be a sequence" in message
    assert "licensing_information must be a string or null" in message
    assert "base_priority must be an integer" in message
