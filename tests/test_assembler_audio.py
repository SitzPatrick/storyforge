from __future__ import annotations

import wave
from pathlib import Path

from app.assembler.audio import generate_silence_bytes, inspect_wav_file, silence_frame_count


def _make_wav(path: Path, *, duration_ms: int = 50, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> Path:
    frame_count = max(1, round(sample_rate * duration_ms / 1000))
    payload = bytes((index % 251 for index in range(frame_count * channels * sample_width)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(payload)
    return path


def test_silence_generation_is_deterministic():
    assert silence_frame_count(24000, 10) == 240
    payload = generate_silence_bytes(240, 1, 2)
    assert len(payload) == 480
    assert set(payload) == {0}


def test_inspect_wav_file_validates_and_hashes(tmp_path: Path):
    wav_path = _make_wav(tmp_path / "sample.wav", duration_ms=50)
    inspection = inspect_wav_file(
        wav_path,
        expected_sample_rate_hz=24000,
        expected_channel_count=1,
        expected_sample_width_bytes=2,
        maximum_duration_seconds=1.0,
    )

    assert inspection.frame_count > 0
    assert inspection.duration_seconds > 0
    assert inspection.sample_rate_hz == 24000
    assert inspection.channel_count == 1
    assert inspection.sample_width_bytes == 2
    assert inspection.audio_content_hash == inspect_wav_file(
        wav_path,
        expected_sample_rate_hz=24000,
        expected_channel_count=1,
        expected_sample_width_bytes=2,
        maximum_duration_seconds=1.0,
    ).audio_content_hash
