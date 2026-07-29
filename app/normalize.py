from __future__ import annotations

import argparse
from pathlib import Path

from .normalization import normalize_analysis


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize a completed StoryForge Phase 3A analysis.")
    parser.add_argument("--analysis-dir", required=True, help="Path to the raw analysis directory")
    parser.add_argument("--output-dir", default=None, help="Optional explicit normalized output directory")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = normalize_analysis(Path(args.analysis_dir), Path(args.output_dir) if args.output_dir else None, args.config)
    print(f"Normalized output directory: {result['output_dir']}")
    print(f"Raw hashes unchanged: {'yes' if result['raw_hashes_before'] == result['raw_hashes_after'] else 'no'}")
    print(f"Validation errors: {len(result['validation_errors'])}")
    return 0 if not result["validation_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
