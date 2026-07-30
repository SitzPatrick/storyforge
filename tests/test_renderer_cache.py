from __future__ import annotations

from pathlib import Path

import pytest

from app.renderer.cache import RenderCacheEntry, build_render_cache_key, cache_entry_matches, load_render_sidecar


def test_render_cache_key_is_deterministic_and_affects_audio_inputs():
    base = {
        "render_unit_id": "ru_1",
        "synthesis_input_hash": "abc",
        "renderer_contract_version": 1,
        "provider": "kokoro",
        "provider_voice_id": "af_bella",
        "provider_adapter_version": "1.0.0",
        "model_version": "model-a",
        "output_format": "wav",
        "sample_rate_hz": 24000,
        "channel_count": 1,
        "sample_width_bytes": 2,
        "deterministic_seed": 7,
    }
    key_1 = build_render_cache_key(base)
    key_2 = build_render_cache_key(dict(base))
    assert key_1 == key_2
    assert len(key_1) == 64

    changed = dict(base, provider_voice_id="af_sarah")
    assert build_render_cache_key(changed) != key_1


def test_cache_entry_requires_matching_audio_and_sidecar(tmp_path: Path):
    audio = tmp_path / "segment.wav"
    sidecar = tmp_path / "segment.wav.json"
    audio.write_bytes(b"data")
    sidecar.write_text(
        "{\n"
        '  "render_unit_id": "ru_1",\n'
        '  "synthesis_input_hash": "abc",\n'
        '  "renderer_contract_version": 1,\n'
        '  "provider": "kokoro",\n'
        '  "provider_voice_id": "af_bella",\n'
        '  "provider_adapter_version": "1.0.0",\n'
        '  "model_version": "model-a",\n'
        '  "output_format": "wav",\n'
        '  "sample_rate_hz": 24000,\n'
        '  "channel_count": 1,\n'
        '  "sample_width_bytes": 2,\n'
        '  "frame_count": 2400,\n'
        '  "duration_seconds": 0.1,\n'
        '  "cache_key": "deadbeef",\n'
        '  "audio_content_hash": "bad",\n'
        '  "artifact_relative_path": "segment.wav",\n'
        '  "validation_result": "passed"\n'
        "}\n",
        encoding="utf-8",
    )

    entry = load_render_sidecar(sidecar)
    assert isinstance(entry, RenderCacheEntry)
    assert entry.render_unit_id == "ru_1"
    assert cache_entry_matches(
        entry,
        render_unit_id="ru_1",
        synthesis_input_hash="abc",
        renderer_contract_version=1,
        provider="kokoro",
        provider_voice_id="af_bella",
        provider_adapter_version="1.0.0",
        model_version="model-a",
        cache_key="deadbeef",
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
        artifact_path=audio,
        artifact_relative_path="segment.wav",
    ) is False


def test_cache_entry_matches_rejects_identity_mismatches(tmp_path: Path):
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"data")
    sidecar = tmp_path / "segment.wav.json"
    sidecar.write_text(
        "{\n"
        '  "render_unit_id": "ru_1",\n'
        '  "synthesis_input_hash": "abc",\n'
        '  "renderer_contract_version": 1,\n'
        '  "provider": "kokoro",\n'
        '  "provider_voice_id": "af_bella",\n'
        '  "provider_adapter_version": "1.0.0",\n'
        '  "model_version": "model-a",\n'
        '  "output_format": "wav",\n'
        '  "sample_rate_hz": 24000,\n'
        '  "channel_count": 1,\n'
        '  "sample_width_bytes": 2,\n'
        '  "frame_count": 2400,\n'
        '  "duration_seconds": 0.1,\n'
        '  "cache_key": "deadbeef",\n'
        '  "audio_content_hash": "bad",\n'
        '  "artifact_relative_path": "segment.wav",\n'
        '  "validation_result": "passed"\n'
        "}\n",
        encoding="utf-8",
    )

    entry = load_render_sidecar(sidecar)
    assert isinstance(entry, RenderCacheEntry)
    assert cache_entry_matches(
        entry,
        render_unit_id="ru_1",
        synthesis_input_hash="abc",
        renderer_contract_version=1,
        provider="kokoro",
        provider_voice_id="af_bella",
        provider_adapter_version="1.0.0",
        model_version="model-a",
        cache_key="deadbeef",
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
        artifact_path=audio,
        artifact_relative_path="segment.wav",
    ) is False

    assert cache_entry_matches(
        entry,
        render_unit_id="ru_1",
        synthesis_input_hash="abc",
        renderer_contract_version=1,
        provider="kokoro",
        provider_voice_id="af_bella",
        provider_adapter_version="1.0.0",
        model_version="model-a",
        cache_key="deadbeef",
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
        artifact_path=audio,
        artifact_relative_path="nested/segment.wav",
    ) is False


def test_missing_or_corrupt_sidecar_is_not_a_cache_hit(tmp_path: Path):
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"data")
    assert load_render_sidecar(tmp_path / "missing.json") is None

    bad_sidecar = tmp_path / "segment.wav.json"
    bad_sidecar.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_render_sidecar(bad_sidecar)
