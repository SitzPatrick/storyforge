from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Sequence


class AudioConcatError(RuntimeError):
    pass


class AudioProbeError(RuntimeError):
    pass


def concatenate_wavs(input_files: Sequence[Path], output_file: Path) -> None:
    if not input_files:
        raise AudioConcatError("No WAV files supplied for concatenation.")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    concat_list = output_file.parent / f"{output_file.stem}.concat.txt"
    concat_list.write_text("\n".join(f"file '{_escape_concat_path(path)}'" for path in input_files), encoding="utf-8")

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c",
        "copy",
        str(output_file),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    finally:
        if concat_list.exists():
            concat_list.unlink(missing_ok=True)

    if proc.returncode != 0:
        raise AudioConcatError(
            "FFmpeg concatenation failed with exit code "
            f"{proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}"
        )


def probe_audio(path: Path) -> dict:
    if not path.exists():
        raise AudioProbeError(f"Audio file does not exist: {path}")

    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AudioProbeError(f"ffprobe failed for {path}: {proc.stderr.strip() or proc.stdout.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AudioProbeError(f"ffprobe produced invalid JSON for {path}: {exc}") from exc

    streams = data.get("streams") or []
    if not streams:
        raise AudioProbeError(f"ffprobe found no streams in {path}")
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not audio_streams:
        raise AudioProbeError(f"ffprobe found no audio stream in {path}")

    fmt = data.get("format") or {}
    duration = _safe_float(fmt.get("duration"))
    if duration is None or duration <= 0:
        raise AudioProbeError(f"ffprobe reported invalid duration for {path}: {fmt.get('duration')}")

    audio = audio_streams[0]
    return {
        "duration": duration,
        "codec_name": audio.get("codec_name"),
        "sample_rate": audio.get("sample_rate"),
        "channels": audio.get("channels"),
        "bit_rate": fmt.get("bit_rate"),
        "path": str(path),
    }


def ensure_ffmpeg_available() -> None:
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise AudioProbeError(f"Required binary not found on PATH: {binary}")


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _escape_concat_path(path: Path) -> str:
    return str(path).replace("'", "\\'")
