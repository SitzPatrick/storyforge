from __future__ import annotations

import hashlib
import io
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.renderer.audio_validation import RenderedAudioValidationError, validate_rendered_audio


@dataclass(frozen=True)
class WavInspection:
    path: Path
    file_size: int
    frame_count: int
    duration_seconds: float
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    audio_content_hash: str
    raw_bytes: bytes
    pcm_frames: bytes


class WavInspectionError(RenderedAudioValidationError):
    pass


def silence_frame_count(sample_rate_hz: int, milliseconds: int, *, rounding: str = "round") -> int:
    if milliseconds < 0:
        raise ValueError("silence milliseconds must be non-negative")
    raw = sample_rate_hz * milliseconds / 1000.0
    if rounding == "round":
        return int(round(raw))
    if rounding == "floor":
        return int(raw // 1)
    if rounding == "ceil":
        return int(-(-raw // 1))
    raise ValueError(f"unsupported rounding mode: {rounding}")


def generate_silence_bytes(frame_count: int, channel_count: int, sample_width_bytes: int) -> bytes:
    if frame_count < 0:
        raise ValueError("frame_count must be non-negative")
    if channel_count <= 0 or sample_width_bytes <= 0:
        raise ValueError("channel_count and sample_width_bytes must be positive")
    return b"\x00" * frame_count * channel_count * sample_width_bytes


def inspect_wav_file(
    path: Path,
    *,
    expected_sample_rate_hz: int | None = None,
    expected_channel_count: int | None = None,
    expected_sample_width_bytes: int | None = None,
    maximum_duration_seconds: float | None = None,
) -> WavInspection:
    raw_bytes = path.read_bytes()
    try:
        with wave.open(io.BytesIO(raw_bytes), "rb") as handle:
            sample_rate_hz = handle.getframerate()
            channel_count = handle.getnchannels()
            sample_width_bytes = handle.getsampwidth()
            frame_count = handle.getnframes()
            comptype = handle.getcomptype()
            if comptype != "NONE":
                raise WavInspectionError(f"compressed WAV inputs are unsupported: {comptype}")
            pcm_frames = handle.readframes(frame_count)
    except wave.Error as exc:
        raise WavInspectionError(f"invalid WAV file: {path}") from exc

    duration_seconds = frame_count / sample_rate_hz if sample_rate_hz else 0.0
    if frame_count <= 0:
        raise WavInspectionError(f"WAV file has no audio frames: {path}")
    if expected_sample_rate_hz is not None and sample_rate_hz != expected_sample_rate_hz:
        raise WavInspectionError(f"sample rate mismatch for {path}: expected {expected_sample_rate_hz}, got {sample_rate_hz}")
    if expected_channel_count is not None and channel_count != expected_channel_count:
        raise WavInspectionError(f"channel count mismatch for {path}: expected {expected_channel_count}, got {channel_count}")
    if expected_sample_width_bytes is not None and sample_width_bytes != expected_sample_width_bytes:
        raise WavInspectionError(f"sample width mismatch for {path}: expected {expected_sample_width_bytes}, got {sample_width_bytes}")
    if maximum_duration_seconds is not None and duration_seconds > maximum_duration_seconds:
        raise WavInspectionError(f"WAV duration exceeds maximum for {path}: {duration_seconds}")

    validate_rendered_audio(
        path,
        expected_sample_rate=sample_rate_hz,
        expected_channels=channel_count,
        expected_sample_width=sample_width_bytes,
        maximum_duration_seconds=maximum_duration_seconds or duration_seconds + 1.0,
    )

    return WavInspection(
        path=path,
        file_size=len(raw_bytes),
        frame_count=frame_count,
        duration_seconds=duration_seconds,
        sample_rate_hz=sample_rate_hz,
        channel_count=channel_count,
        sample_width_bytes=sample_width_bytes,
        audio_content_hash=hashlib.sha256(raw_bytes).hexdigest(),
        raw_bytes=raw_bytes,
        pcm_frames=pcm_frames,
    )
