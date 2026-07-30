from __future__ import annotations

import hashlib
import math
import wave
from dataclasses import dataclass
from pathlib import Path

from .models import AudioMeasurements


class MasteringMeasurementError(RuntimeError):
    pass


_PCM16_MAX = 32767.0
_PCM16_MIN = -32768.0


def silence_frame_count(sample_rate_hz: int, milliseconds: int) -> int:
    if milliseconds <= 0:
        return 0
    return int(round(sample_rate_hz * milliseconds / 1000.0))


def _dbfs_from_ratio(ratio: float) -> float:
    if ratio <= 0:
        return float("-inf")
    return 20.0 * math.log10(ratio)


def _ratio_from_dbfs(dbfs: float) -> float:
    if dbfs <= -120.0:
        return 0.0
    return 10.0 ** (dbfs / 20.0)


def _read_pcm16_mono(path: Path) -> tuple[list[int], int, int, int, int]:
    if not path.exists():
        raise MasteringMeasurementError(f"audio file does not exist: {path}")
    try:
        with wave.open(str(path), "rb") as handle:
            channel_count = handle.getnchannels()
            sample_width_bytes = handle.getsampwidth()
            sample_rate_hz = handle.getframerate()
            frame_count = handle.getnframes()
            if channel_count != 1:
                raise MasteringMeasurementError(f"unsupported channel count: {channel_count}")
            if sample_width_bytes != 2:
                raise MasteringMeasurementError(f"unsupported sample width: {sample_width_bytes}")
            if sample_rate_hz <= 0:
                raise MasteringMeasurementError(f"invalid sample rate: {sample_rate_hz}")
            raw = handle.readframes(frame_count)
    except wave.Error as exc:
        raise MasteringMeasurementError(f"unable to parse wav header: {path}") from exc
    if len(raw) != frame_count * 2:
        raise MasteringMeasurementError(f"unexpected PCM byte count in {path}")
    samples = [int.from_bytes(raw[i : i + 2], byteorder="little", signed=True) for i in range(0, len(raw), 2)]
    return samples, frame_count, sample_rate_hz, channel_count, sample_width_bytes


def _leading_trailing_silence_frames(samples: list[int], *, threshold_amplitude: int) -> tuple[int, int]:
    leading = 0
    for sample in samples:
        if abs(sample) <= threshold_amplitude:
            leading += 1
        else:
            break
    trailing = 0
    for sample in reversed(samples):
        if abs(sample) <= threshold_amplitude:
            trailing += 1
        else:
            break
    return leading, trailing


def measure_audio(
    path: Path,
    *,
    target_sample_rate_hz: int,
    target_channel_count: int,
    target_sample_width_bytes: int,
    silence_detection_threshold_dbfs: float = -60.0,
) -> AudioMeasurements:
    samples, frame_count, sample_rate_hz, channel_count, sample_width_bytes = _read_pcm16_mono(path)
    if sample_rate_hz != target_sample_rate_hz:
        raise MasteringMeasurementError(f"unexpected sample rate: {sample_rate_hz}")
    if channel_count != target_channel_count:
        raise MasteringMeasurementError(f"unexpected channel count: {channel_count}")
    if sample_width_bytes != target_sample_width_bytes:
        raise MasteringMeasurementError(f"unexpected sample width: {sample_width_bytes}")
    peak_abs = max((abs(sample) for sample in samples), default=0)
    sample_peak = peak_abs / _PCM16_MAX if peak_abs else 0.0
    sample_peak_dbfs = _dbfs_from_ratio(sample_peak) if sample_peak else float("-inf")
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples)) if samples else 0.0
    integrated_loudness_dbfs = _dbfs_from_ratio(rms / _PCM16_MAX) if rms else float("-inf")
    threshold_amplitude = int(round(_PCM16_MAX * _ratio_from_dbfs(silence_detection_threshold_dbfs)))
    leading_silence_frames, trailing_silence_frames = _leading_trailing_silence_frames(samples, threshold_amplitude=threshold_amplitude)
    return AudioMeasurements(
        path=path,
        file_size=path.stat().st_size,
        frame_count=frame_count,
        duration_seconds=frame_count / float(sample_rate_hz),
        sample_rate_hz=sample_rate_hz,
        channel_count=channel_count,
        sample_width_bytes=sample_width_bytes,
        sample_peak=round(sample_peak, 12),
        sample_peak_dbfs=round(sample_peak_dbfs, 6) if math.isfinite(sample_peak_dbfs) else sample_peak_dbfs,
        integrated_loudness_dbfs=round(integrated_loudness_dbfs, 6) if math.isfinite(integrated_loudness_dbfs) else integrated_loudness_dbfs,
        true_peak=None,
        leading_silence_frames=leading_silence_frames,
        trailing_silence_frames=trailing_silence_frames,
        audio_content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
