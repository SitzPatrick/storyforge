from __future__ import annotations

import sys
from collections.abc import Iterable

from . import __version__

_HELP = """Usage: storyforge <command> [args]

Commands:
  build       Run the EPUB-to-audiobook conversion pipeline
  analyze     Run the EPUB story analysis stage
  normalize   Normalize a completed analysis directory
  doctor      Check runtime dependencies and connectivity
  validate    Run the release-readiness audit

Notes:
  - `storyforge --version` prints the package version.
  - `storyforge --help` shows this summary.
  - Voice planning is currently a library API; use app.pipeline.plan_build from Python.
"""


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(_HELP.strip())
        return 0
    if args[0] in {"-V", "--version"}:
        print(f"StoryForge {__version__}")
        return 0

    command, command_args = args[0], args[1:]
    if command == "build":
        from app.convert import main as convert_main

        return convert_main(command_args)
    if command == "analyze":
        from app.analyze import main as analyze_main

        return analyze_main(command_args)
    if command == "normalize":
        from app.normalize import main as normalize_main

        return normalize_main(command_args)
    if command == "doctor":
        from app.diagnostics import main as diagnostics_main

        return diagnostics_main(command_args)
    if command == "validate":
        from .release_check import main as release_check_main

        return release_check_main(command_args)

    print(f"Unknown command: {command}", file=sys.stderr)
    print(_HELP.strip(), file=sys.stderr)
    return 2
