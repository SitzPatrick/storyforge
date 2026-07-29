from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .audio import probe_audio


@dataclass(frozen=True)
class M4BResult:
    output_file: Path
    chapter_count: int


class M4BError(RuntimeError):
    pass


def create_m4b(
    chapter_wavs: Sequence[Path],
    output_file: Path,
    metadata: dict[str, str],
    chapters: Sequence[dict],
    cover_path: Path | None,
    bitrate: str,
) -> M4BResult:
    if not chapter_wavs:
        raise M4BError("No chapter WAV files supplied for M4B creation.")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = output_file.parent / "_m4b_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    concat_list = temp_dir / "concat.txt"
    meta_file = temp_dir / "chapters.ffmetadata"

    concat_list.write_text("\n".join(f"file '{_escape_concat_path(path)}'" for path in chapter_wavs), encoding="utf-8")
    meta_file.write_text(_build_ffmetadata(metadata, chapters), encoding="utf-8")

    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
    ]
    if cover_path is not None and cover_path.exists():
        cmd.extend(["-i", str(cover_path)])
    cmd.extend([
        "-f",
        "ffmetadata",
        "-i",
        str(meta_file),
        "-map",
        "0:a",
    ])
    if cover_path is not None and cover_path.exists():
        cmd.extend(["-map", "1:v"])
    cmd.extend([
        "-map_metadata",
        "2",
        "-map_chapters",
        "2",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
    ])
    if cover_path is not None and cover_path.exists():
        cmd.extend(["-c:v", "copy", "-disposition:v:0", "attached_pic"])
    cmd.append(str(output_file))

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    try:
        concat_list.unlink(missing_ok=True)
        meta_file.unlink(missing_ok=True)
        if not any(temp_dir.iterdir()):
            temp_dir.rmdir()
    except OSError:
        pass

    if proc.returncode != 0:
        raise M4BError(f"FFmpeg M4B creation failed with exit code {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")

    return M4BResult(output_file=output_file, chapter_count=len(chapters))


def _build_ffmetadata(metadata: dict[str, str], chapters: Sequence[dict]) -> str:
    lines = [";FFMETADATA1"]
    for key, value in metadata.items():
        if value is None or value == "":
            continue
        lines.append(f"{key}={_escape_metadata(str(value))}")

    current_ms = 0
    for chapter in chapters:
        duration_seconds = float(chapter.get("duration_seconds") or chapter.get("actual_narration_seconds") or 0.0)
        duration_ms = max(1, int(round(duration_seconds * 1000)))
        start = current_ms
        end = current_ms + duration_ms
        current_ms = end
        chapter_title = str(chapter.get("title") or f"Chapter {chapter.get('chapter', '')}")
        lines.extend([
            "",
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start}",
            f"END={end}",
            f"title={_escape_metadata(chapter_title)}",
        ])
    lines.append("")
    return "\n".join(lines)


def _escape_metadata(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", " ").replace("\r", " ")


def _escape_concat_path(path: Path) -> str:
    return str(path).replace("'", "\\'")
