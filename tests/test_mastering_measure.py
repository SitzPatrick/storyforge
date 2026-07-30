from __future__ import annotations

import math
import wave
from pathlib import Path

import pytest

from app.mastering import MasteringConfig, measure_audio, silence_frame_count


def _write_pcm_wav(path: Path, samples: list[int], *, sample_rate: int = 24000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        payload = b"".join(int(sample).to_bytes(2, byteorder="little", signed=True) for sample in samples)
        handle.writeframes(payload)


def test_measure_audio_reports_peak_loudness_and_edge_silence(tmp_path: Path):
    wav_path = tmp_path / "chapter.wav"
    _write_pcm_wav(wav_path, [0] * 120 + [1200] * 240 + [0] * 80)

    measurements = measure_audio(wav_path, target_sample_rate_hz=24000, target_channel_count=1, target_sample_width_bytes=2)

    assert measurements.frame_count == 440
    assert measurements.leading_silence_frames == 120
    assert measurements.trailing_silence_frames == 80
    assert measurements.sample_peak == pytest.approx(1200 / 32767, rel=1e-6)
    assert measurements.true_peak is None
    assert measurements.integrated_loudness_dbfs < 0
    assert measurements.audio_content_hash


def test_silence_frame_count_rounds_deterministically():
    assert silence_frame_count(24000, 125) == 3000
    assert silence_frame_count(24000, 33) == 792


def test_measure_audio_rejects_incompatible_format(tmp_path: Path):
    wav_path = tmp_path / "chapter.wav"
    _write_pcm_wav(wav_path, [0, 1, 2, 3], sample_rate=22050)

    with pytest.raises(Exception):
        measure_audio(wav_path, target_sample_rate_hz=24000, target_channel_count=1, target_sample_width_bytes=2)
