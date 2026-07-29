from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_settings
from .epub_utils import list_chapters, read_epub
from .queue import ConversionQueue
from .runner import BookConversionRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert a DRM-free EPUB into a full audiobook (WAV chapters + M4B).")
    parser.add_argument("--epub", required=False, help="Path to the EPUB file to convert")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--resume-all", action="store_true", help="Resume any unfinished jobs before processing the requested EPUB")
    parser.add_argument("--list-chapters", action="store_true", help="List detected chapters and exit without generating audio")
    parser.add_argument("--dry-run", action="store_true", help="Alias for --list-chapters")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    runner = BookConversionRunner(settings)

    if settings.conversion.resume_on_startup or args.resume_all:
        runner.resume_unfinished_jobs()

    if not args.epub:
        parser.print_help()
        return 0

    if args.list_chapters or args.dry_run:
        book = read_epub(Path(args.epub))
        for chapter in list_chapters(book):
            print(f"{chapter.number:03d}\t{chapter.display_title}\t{chapter.href}")
        return 0

    result = runner.run_book(Path(args.epub))
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
