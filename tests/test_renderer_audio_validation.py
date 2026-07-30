from __future__ import annotations

import wave
from pathlib import Path

import pytest

from app.renderer.audio_validation import RenderedAudioValidationError, validate_rendered_audio


def _make_wav(path: Path, *, duration: float = 0.1, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> Path:
    nframes = max(1, int(duration * sample_rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00" * nframes * channels * sample_width)
    return path


def test_validate_rendered_audio_accepts_valid_wav(tmp_path: Path):
    audio = _make_wav(tmp_path / "segment.wav")

    result = validate_rendered_audio(
        audio,
        expected_sample_rate=24000,
        expected_channels=1,
        expected_sample_width=2,
        maximum_duration_seconds=5.0,
    )

    assert result.sample_rate == 24000
    assert result.channels == 1
    assert result.sample_width == 2
    assert result.duration_seconds > 0
    assert result.audio_content_hash


def test_validate_rendered_audio_rejects_corrupt_wav(tmp_path: Path):
    audio = tmp_path / "segment.wav"
    audio.write_bytes(b"not a wav file")

    with pytest.raises(RenderedAudioValidationError, match="readable audio header"):
        validate_rendered_audio(
            audio,
            expected_sample_rate=24000,
            expected_channels=1,
            expected_sample_width=2,
            maximum_duration_seconds=5.0,
        )
