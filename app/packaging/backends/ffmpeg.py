from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..models import PackagingBackendResult, PackagingRequest, PackagingValidationStatus
from .base import PackagingBackend


class FFmpegPackagingBackend(PackagingBackend):
    backend_name = "ffmpeg"
    backend_version = "unknown"
    encoder_name = "aac"
    encoder_version: str | None = None

    def __init__(self, ffmpeg_path: str | None = None, ffprobe_path: str | None = None):
        self.ffmpeg_path = ffmpeg_path or shutil.which("ffmpeg")
        self.ffprobe_path = ffprobe_path or shutil.which("ffprobe")
        self._version_line: str | None = None

    def is_available(self) -> bool:
        return bool(self.ffmpeg_path and self.ffprobe_path)

    def _ensure_version(self) -> None:
        if self._version_line is not None:
            return
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg executable not available")
        result = subprocess.run(
            [self.ffmpeg_path, "-version"], check=True, capture_output=True, text=True
        )
        self._version_line = result.stdout.splitlines()[0] if result.stdout else "ffmpeg unknown"
        if " version " in self._version_line:
            self.backend_version = self._version_line.split(" version ", 1)[1].split()[0]
        else:
            self.backend_version = self._version_line
        self.encoder_version = self.backend_version

    def package(self, request: PackagingRequest) -> PackagingBackendResult:
        if not self.is_available():
            raise RuntimeError("ffmpeg/ffprobe are not available")
        self._ensure_version()
        request.temp_output_path.parent.mkdir(parents=True, exist_ok=True)
        concat_file = request.temp_output_path.with_suffix(".concat.txt")
        metadata_file = request.temp_output_path.with_suffix(".ffmetadata")
        concat_file.write_text(
            "".join(
                f"file '{chapter.mastered_audio_path.as_posix()}'\n"
                for chapter in request.chapter_inputs
            ),
            encoding="utf-8",
        )
        metadata_file.write_text(_build_ffmetadata(request), encoding="utf-8")
        cmd = [
            self.ffmpeg_path,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-f",
            "ffmetadata",
            "-i",
            str(metadata_file),
            "-map",
            "0:a",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:a",
            "aac",
            "-profile:a",
            request.config.encoder_profile,
            "-b:a",
            f"{request.config.audio_bitrate_kbps}k",
            "-ar",
            str(request.config.sample_rate_hz),
            "-ac",
            str(request.config.channel_count),
            "-movflags",
            "+faststart" if request.config.fast_start else "-faststart",
            str(request.temp_output_path),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"ffmpeg encoding failed: {exc.stderr or exc.stdout}") from exc
        probe = self.probe(request.temp_output_path)
        return PackagingBackendResult(
            output_path=request.temp_output_path,
            output_artifact_relative_path=request.output_artifact_relative_path,
            output_container=request.config.container_format,
            audio_codec=request.config.audio_codec,
            audio_bitrate_kbps=request.config.audio_bitrate_kbps,
            sample_rate_hz=request.config.sample_rate_hz,
            channel_count=request.config.channel_count,
            duration_seconds=float(probe["duration_seconds"]),
            chapter_count=len(request.chapter_inputs),
            chapter_probe_data=tuple(probe.get("chapter_probe_data", [])),
            metadata_probe_data=dict(probe.get("metadata_probe_data", {})),
            cover_art_probe_state=probe.get("cover_art_probe_state"),
            backend_name=self.backend_name,
            backend_version=self.backend_version,
            encoder_name=self.encoder_name,
            encoder_version=self.encoder_version,
            file_size=request.temp_output_path.stat().st_size,
            audio_content_hash=_sha256(request.temp_output_path),
            validation_result=PackagingValidationStatus.PASSED,
            warnings=(),
            errors=(),
            probe_data=probe,
        )

    def probe(self, path: Path | None = None) -> dict[str, Any]:
        if not self.ffprobe_path:
            raise RuntimeError("ffprobe executable not available")
        if path is None:
            self._ensure_version()
            return {
                "backend_name": self.backend_name,
                "backend_version": self.backend_version,
                "encoder_name": self.encoder_name,
                "encoder_version": self.encoder_version,
                "ffmpeg_version_line": self._version_line or "ffmpeg unknown",
            }
        result = subprocess.run(
            [
                self.ffprobe_path,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                "-show_chapters",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        probe = json.loads(result.stdout)
        format_info = probe.get("format", {})
        streams = probe.get("streams", [])
        audio_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"), {}
        )
        chapters = []
        for chapter in probe.get("chapters", []):
            start_seconds = float(chapter.get("start_time", 0) or 0)
            end_seconds = float(chapter.get("end_time", 0) or 0)
            chapters.append(
                {
                    "chapter_id": chapter.get("tags", {}).get("chapter_id"),
                    "chapter_title": chapter.get("tags", {}).get("title"),
                    "chapter_order": int(chapter.get("tags", {}).get("chapter_order", 0) or 0),
                    "mastered_chapter_id": chapter.get("tags", {}).get("mastered_chapter_id"),
                    "start_time": int(round(start_seconds * 1_000_000)),
                    "end_time": int(round(end_seconds * 1_000_000)),
                    "duration_ticks": int(round((end_seconds - start_seconds) * 1_000_000)),
                    "timebase": 1_000_000,
                    "book_id": chapter.get("tags", {}).get("book_id"),
                }
            )
        output_container = str(format_info.get("format_name", "m4b"))
        if path.suffix.lower() == ".m4b":
            output_container = "m4b"
        return {
            "output_path": str(path),
            "output_container": output_container,
            "audio_codec": audio_stream.get("codec_name", "aac"),
            "audio_bitrate_kbps": (
                int(float(audio_stream.get("bit_rate", 0)) / 1000)
                if audio_stream.get("bit_rate")
                else 0
            ),
            "sample_rate_hz": int(audio_stream.get("sample_rate", 0) or 0),
            "channel_count": int(audio_stream.get("channels", 0) or 0),
            "duration_seconds": float(format_info.get("duration", 0.0) or 0.0),
            "chapter_count": len(chapters),
            "chapter_probe_data": chapters,
            "metadata_probe_data": format_info.get("tags", {}),
            "cover_art_probe_state": None,
            "backend_name": self.backend_name,
            "backend_version": self.backend_version,
            "encoder_name": self.encoder_name,
            "encoder_version": self.encoder_version,
            "file_size": path.stat().st_size,
            "audio_content_hash": _sha256(path),
            "validation_result": "passed",
            "warnings": [],
            "errors": [],
        }


def _build_ffmetadata(request: PackagingRequest) -> str:
    lines = [";FFMETADATA1"]
    for key, value in request.normalized_metadata.__dict__.items():
        if value is None:
            continue
        lines.append(f"{key}={str(value).replace(chr(10), ' ').replace(chr(13), ' ')}")
    for chapter in request.chapter_timeline:
        lines.extend(
            [
                "[CHAPTER]",
                f"TIMEBASE=1/{chapter.timebase}",
                f"START={chapter.start_time}",
                f"END={chapter.end_time}",
                f"title={chapter.chapter_title or chapter.chapter_id}",
                f"chapter_id={chapter.chapter_id}",
                f"mastered_chapter_id={chapter.mastered_chapter_id}",
                f"book_id={chapter.book_id}",
            ]
        )
    return "\n".join(lines) + "\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
