from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .audio import AudioProbeError, ensure_ffmpeg_available
from .kokoro_client import KokoroClient, KokoroError, KokoroVoiceError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Storyforge deployment diagnostic.")
    parser.add_argument("--books-dir", default=os.getenv("STORYFORGE_BOOKS_DIR", "/books"), help="Mounted EPUB input directory")
    parser.add_argument("--output-dir", default=os.getenv("STORYFORGE_OUTPUT_DIR", "/output"), help="Mounted audiobook output directory")
    parser.add_argument("--log-dir", default=os.getenv("STORYFORGE_LOG_DIR", "/app/logs"), help="Mounted logs directory")
    parser.add_argument("--temp-dir", default=os.getenv("STORYFORGE_TEMP_DIR", "/app/temp"), help="Mounted temp directory")
    parser.add_argument("--api-url", default=os.getenv("KOKORO_API_URL", "http://Kokoro-FastAPI:8880/v1"), help="Kokoro OpenAI-compatible base URL")
    parser.add_argument("--voice", default=os.getenv("KOKORO_VOICE", "af_bella"), help="Voice to validate")
    parser.add_argument("--api-key", default=os.getenv("KOKORO_API_KEY", "not-needed"), help="API key placeholder")
    parser.add_argument("--timeout", default=float(os.getenv("STORYFORGE_KOKORO_TIMEOUT", 120.0)), type=float, help="Request timeout")
    return parser


def _check_dir_readable(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"Path does not exist: {path}")
    if not os.access(path, os.R_OK):
        raise RuntimeError(f"Path is not readable: {path}")


def _check_dir_writable(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".storyforge-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        raise RuntimeError(f"Path is not writable: {path}: {exc}") from exc


def _check_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"Required binary not found on PATH: {name}")
    proc = subprocess.run([name, "-version"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{name} exists but failed to run: {proc.stderr.strip() or proc.stdout.strip()}")
    return path


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    client = KokoroClient(base_url=args.api_url, api_key=args.api_key, voice=args.voice, timeout=args.timeout)

    checks = []
    _check_dir_readable(Path(args.books_dir))
    checks.append(f"readable: {args.books_dir}")

    _check_dir_writable(Path(args.output_dir))
    checks.append(f"writable: {args.output_dir}")

    _check_dir_writable(Path(args.log_dir))
    checks.append(f"writable: {args.log_dir}")

    _check_dir_writable(Path(args.temp_dir))
    checks.append(f"writable: {args.temp_dir}")

    ffmpeg_path = _check_binary("ffmpeg")
    ffprobe_path = _check_binary("ffprobe")
    checks.append(f"ffmpeg: {ffmpeg_path}")
    checks.append(f"ffprobe: {ffprobe_path}")

    reachable_url = client.health_check()
    checks.append(f"kokoro reachable: {reachable_url}")

    voices = client.list_voices()
    if args.voice not in voices and args.voice not in client.list_voices():
        raise RuntimeError(f"Selected voice '{args.voice}' not accepted. Available voices: {', '.join(voices)}")
    checks.append(f"voice accepted: {args.voice}")

    print("Storyforge diagnostics passed")
    for item in checks:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
