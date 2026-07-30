from __future__ import annotations

import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path


class RenderedAudioValidationError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedAudioValidationResult:
    path: Path
    file_size: int
    frame_count: int
    duration_seconds: float
    sample_rate: int
    channels: int
    sample_width: int
    audio_content_hash: str


def validate_rendered_audio(
    path: Path,
    *,
    expected_sample_rate: int,
    expected_channels: int,
    expected_sample_width: int,
    maximum_duration_seconds: float,
) -> RenderedAudioValidationResult:
    if not path.exists():
        raise RenderedAudioValidationError(f"audio file does not exist: {path}")
    file_size = path.stat().st_size
    if file_size <= 0:
        raise RenderedAudioValidationError(f"audio file is empty: {path}")
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
            sample_rate = handle.getframerate()
            frame_count = handle.getnframes()
            duration_seconds = frame_count / float(sample_rate or 1)
            if channels != expected_channels:
                raise RenderedAudioValidationError(f"unexpected channel count: expected {expected_channels}, got {channels}")
            if sample_rate != expected_sample_rate:
                raise RenderedAudioValidationError(f"unexpected sample rate: expected {expected_sample_rate}, got {sample_rate}")
            if sample_width != expected_sample_width:
                raise RenderedAudioValidationError(f"unexpected sample width: expected {expected_sample_width}, got {sample_width}")
            if duration_seconds <= 0:
                raise RenderedAudioValidationError(f"invalid duration reported for {path}: {duration_seconds}")
            if duration_seconds > maximum_duration_seconds:
                raise RenderedAudioValidationError(
                    f"duration exceeds safety maximum: {duration_seconds} > {maximum_duration_seconds}"
                )
    except wave.Error as exc:
        raise RenderedAudioValidationError(f"readable audio header could not be parsed for {path}: {exc}") from exc

    audio_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    return RenderedAudioValidationResult(
        path=path,
        file_size=file_size,
        frame_count=frame_count,
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
        channels=channels,
        sample_width=sample_width,
        audio_content_hash=audio_hash,
    )
