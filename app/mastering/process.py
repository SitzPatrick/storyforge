from __future__ import annotations

import hashlib
import io
import math
import os
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path

from .measure import MasteringMeasurementError, measure_audio, silence_frame_count
from .models import AudioMeasurements, MasteringConfig, MasteringFailure, MasteringFailureType


_PCM16_MAX = 32767
_PCM16_MIN = -32768
_PCM16_RANGE = 32767.0


@dataclass(frozen=True)
class MasteringProcessResult:
    audio_bytes: bytes
    measurements: AudioMeasurements
    requested_gain_db: float
    applied_gain_db: float
    gain_constrained: bool
    limiter_activated: bool
    limiter_amount_db: float | None
    original_leading_silence_frames: int
    original_trailing_silence_frames: int
    trimmed_leading_silence_frames: int
    trimmed_trailing_silence_frames: int
    final_leading_silence_frames: int
    final_trailing_silence_frames: int
    fade_in_frames: int
    fade_out_frames: int
    warnings: tuple[str, ...]


class MasteringProcessingError(RuntimeError):
    pass


def _dbfs_from_ratio(ratio: float) -> float:
    if ratio <= 0:
        return float("-inf")
    return 20.0 * math.log10(ratio)


def _ratio_from_dbfs(dbfs: float) -> float:
    if dbfs <= -120.0:
        return 0.0
    return 10.0 ** (dbfs / 20.0)


def _load_samples(path: Path) -> tuple[list[int], int, int, int]:
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1:
                raise MasteringProcessingError(f"unsupported channel count in {path}")
            if handle.getsampwidth() != 2:
                raise MasteringProcessingError(f"unsupported sample width in {path}")
            sample_rate_hz = handle.getframerate()
            frame_count = handle.getnframes()
            raw = handle.readframes(frame_count)
    except wave.Error as exc:
        raise MasteringProcessingError(f"unable to load wav: {path}") from exc
    samples = [int.from_bytes(raw[index : index + 2], byteorder="little", signed=True) for index in range(0, len(raw), 2)]
    return samples, frame_count, sample_rate_hz, 2


def _write_mastered_wav(path: Path, samples: list[int], sample_rate_hz: int) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate_hz)
        payload = b"".join(int(sample).to_bytes(2, byteorder="little", signed=True) for sample in samples)
        handle.writeframes(payload)
    return path.read_bytes()


def _leading_trailing_silence(samples: list[int], *, threshold_amplitude: int) -> tuple[int, int]:
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


def _trim_edges(samples: list[int], config: MasteringConfig, sample_rate_hz: int) -> tuple[list[int], int, int, int, int, int, int]:
    threshold_amplitude = int(round(_PCM16_RANGE * _ratio_from_dbfs(config.silence_detection_threshold_dbfs)))
    leading, trailing = _leading_trailing_silence(samples, threshold_amplitude=threshold_amplitude)
    leading_target = silence_frame_count(sample_rate_hz, config.leading_silence_target_ms) if config.trim_leading_silence_enabled else leading
    trailing_target = silence_frame_count(sample_rate_hz, config.trailing_silence_target_ms) if config.trim_trailing_silence_enabled else trailing
    minimum_required = silence_frame_count(sample_rate_hz, config.minimum_silence_duration_ms)
    trimmed_leading = 0
    trimmed_trailing = 0
    if config.trim_leading_silence_enabled and leading > leading_target and leading >= minimum_required:
        trimmed_leading = leading - leading_target
    if config.trim_trailing_silence_enabled and trailing > trailing_target and trailing >= minimum_required:
        trimmed_trailing = trailing - trailing_target
    start = trimmed_leading
    end = len(samples) - trimmed_trailing if trimmed_trailing else len(samples)
    trimmed = samples[start:end]
    final_leading, final_trailing = _leading_trailing_silence(trimmed, threshold_amplitude=threshold_amplitude)
    return trimmed, leading, trailing, trimmed_leading, trimmed_trailing, final_leading, final_trailing


def _apply_gain(samples: list[int], gain_db: float, *, limiter_enabled: bool, limiter_ceiling_dbfs: float) -> tuple[list[int], bool, float | None]:
    multiplier = _ratio_from_dbfs(gain_db)
    ceiling_ratio = _ratio_from_dbfs(limiter_ceiling_dbfs)
    limiter_activated = False
    limiter_amount_db: float | None = None
    output: list[int] = []
    for sample in samples:
        value = sample * multiplier
        if limiter_enabled:
            capped = max(min(value, _PCM16_RANGE * ceiling_ratio), -_PCM16_RANGE * ceiling_ratio)
            if capped != value:
                limiter_activated = True
            value = capped
        clipped = max(min(int(round(value)), _PCM16_MAX), _PCM16_MIN)
        if clipped != int(round(value)):
            limiter_activated = True
        output.append(clipped)
    if limiter_enabled and limiter_activated:
        limiter_amount_db = max(0.0, gain_db - limiter_ceiling_dbfs)
    return output, limiter_activated, limiter_amount_db


def _apply_fade(samples: list[int], *, fade_in_frames: int, fade_out_frames: int) -> list[int]:
    if not samples:
        return samples
    output = samples[:]
    fade_in_frames = min(fade_in_frames, len(output))
    fade_out_frames = min(fade_out_frames, len(output))
    for index in range(fade_in_frames):
        factor = index / max(1, fade_in_frames - 1) if fade_in_frames > 1 else 1.0
        output[index] = int(round(output[index] * factor))
    for offset in range(fade_out_frames):
        factor = (fade_out_frames - 1 - offset) / max(1, fade_out_frames - 1) if fade_out_frames > 1 else 1.0
        index = len(output) - fade_out_frames + offset
        output[index] = int(round(output[index] * factor))
    return output


def _encode_wav_bytes(samples: list[int], sample_rate_hz: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate_hz)
        payload = b"".join(int(sample).to_bytes(2, byteorder="little", signed=True) for sample in samples)
        handle.writeframes(payload)
    return buffer.getvalue()


def process_mastered_audio(source_path: Path, config: MasteringConfig) -> MasteringProcessResult:
    try:
        source_measurements = measure_audio(
            source_path,
            target_sample_rate_hz=config.sample_rate_hz,
            target_channel_count=config.channel_count,
            target_sample_width_bytes=config.sample_width_bytes,
            silence_detection_threshold_dbfs=config.silence_detection_threshold_dbfs,
        )
    except Exception as exc:  # noqa: BLE001
        raise MasteringProcessingError(str(exc)) from exc

    samples, _, sample_rate_hz, _ = _load_samples(source_path)
    trimmed_samples, original_leading, original_trailing, trimmed_leading, trimmed_trailing, final_leading, final_trailing = _trim_edges(
        samples, config, sample_rate_hz
    )

    if not trimmed_samples:
        raise MasteringProcessingError("mastering would produce empty audio")

    input_peak_dbfs = source_measurements.sample_peak_dbfs if math.isfinite(source_measurements.sample_peak_dbfs) else -120.0
    requested_gain_db = config.target_integrated_loudness_dbfs - source_measurements.integrated_loudness_dbfs
    gain_constrained = False
    if requested_gain_db > config.max_gain_increase_db:
        requested_gain_db = config.max_gain_increase_db
        gain_constrained = True
    if requested_gain_db < -config.max_gain_reduction_db:
        requested_gain_db = -config.max_gain_reduction_db
        gain_constrained = True

    applied_gain_db = requested_gain_db
    limiter_activated = False
    limiter_amount_db: float | None = None
    if not config.limiter_enabled:
        peak_headroom_db = config.max_sample_peak_dbfs - input_peak_dbfs
        if applied_gain_db > peak_headroom_db:
            applied_gain_db = peak_headroom_db
            gain_constrained = True
    mastered_samples, limiter_activated, limiter_amount_db = _apply_gain(
        trimmed_samples,
        applied_gain_db,
        limiter_enabled=config.limiter_enabled,
        limiter_ceiling_dbfs=config.limiter_ceiling_dbfs,
    )

    fade_in_frames = silence_frame_count(sample_rate_hz, config.fade_in_ms)
    fade_out_frames = silence_frame_count(sample_rate_hz, config.fade_out_ms)
    if fade_in_frames or fade_out_frames:
        mastered_samples = _apply_fade(mastered_samples, fade_in_frames=fade_in_frames, fade_out_frames=fade_out_frames)

    output_bytes = _encode_wav_bytes(mastered_samples, sample_rate_hz)
    output_peak_abs = max((abs(sample) for sample in mastered_samples), default=0)
    output_peak_ratio = output_peak_abs / _PCM16_RANGE if output_peak_abs else 0.0
    output_peak_dbfs = _dbfs_from_ratio(output_peak_ratio) if output_peak_ratio else float("-inf")
    output_rms = math.sqrt(sum(sample * sample for sample in mastered_samples) / len(mastered_samples)) if mastered_samples else 0.0
    output_integrated_loudness_dbfs = _dbfs_from_ratio(output_rms / _PCM16_RANGE) if output_rms else float("-inf")
    output_measurements = AudioMeasurements(
        path=source_path,
        file_size=len(output_bytes),
        frame_count=len(mastered_samples),
        duration_seconds=len(mastered_samples) / float(sample_rate_hz),
        sample_rate_hz=sample_rate_hz,
        channel_count=1,
        sample_width_bytes=2,
        sample_peak=round(output_peak_ratio, 12),
        sample_peak_dbfs=round(output_peak_dbfs, 6) if math.isfinite(output_peak_dbfs) else output_peak_dbfs,
        integrated_loudness_dbfs=round(output_integrated_loudness_dbfs, 6) if math.isfinite(output_integrated_loudness_dbfs) else output_integrated_loudness_dbfs,
        true_peak=None,
        leading_silence_frames=final_leading,
        trailing_silence_frames=final_trailing,
        audio_content_hash=hashlib.sha256(output_bytes).hexdigest(),
    )
    warnings: list[str] = []
    if abs(output_integrated_loudness_dbfs - config.target_integrated_loudness_dbfs) > config.loudness_tolerance_db:
        if gain_constrained or limiter_activated:
            warnings.append("target loudness constrained by peak or gain limits")
        else:
            warnings.append("output loudness outside tolerance")
    return MasteringProcessResult(
        audio_bytes=output_bytes,
        measurements=output_measurements,
        requested_gain_db=round(source_measurements.integrated_loudness_dbfs * -1 + config.target_integrated_loudness_dbfs, 6)
        if math.isfinite(source_measurements.integrated_loudness_dbfs)
        else requested_gain_db,
        applied_gain_db=round(applied_gain_db, 6),
        gain_constrained=gain_constrained,
        limiter_activated=limiter_activated,
        limiter_amount_db=round(limiter_amount_db, 6) if limiter_amount_db is not None else None,
        original_leading_silence_frames=original_leading,
        original_trailing_silence_frames=original_trailing,
        trimmed_leading_silence_frames=trimmed_leading,
        trimmed_trailing_silence_frames=trimmed_trailing,
        final_leading_silence_frames=final_leading,
        final_trailing_silence_frames=final_trailing,
        fade_in_frames=fade_in_frames,
        fade_out_frames=fade_out_frames,
        warnings=tuple(warnings),
    )


def validate_mastered_audio(path: Path, config: MasteringConfig) -> tuple[AudioMeasurements, tuple[str, ...]]:
    measurements = measure_audio(
        path,
        target_sample_rate_hz=config.sample_rate_hz,
        target_channel_count=config.channel_count,
        target_sample_width_bytes=config.sample_width_bytes,
        silence_detection_threshold_dbfs=config.silence_detection_threshold_dbfs,
    )
    warnings: list[str] = []
    if measurements.frame_count <= 0:
        raise MasteringProcessingError("mastered audio is empty")
    if measurements.sample_peak_dbfs > config.max_sample_peak_dbfs + config.peak_tolerance_db:
        raise MasteringProcessingError("mastered audio exceeds peak ceiling")
    return measurements, tuple(warnings)
