from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_settings
from .story_analysis import BookAnalysisError, StoryAnalyzer


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze a DRM-free EPUB and produce structured story data.")
    parser.add_argument("--epub", required=True, help="Path to the EPUB file to analyze")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(args.config)
    analyzer = StoryAnalyzer(settings)

    try:
        plan = analyzer.prepare(Path(args.epub))
    except BookAnalysisError as exc:
        print(f"Analysis failed: {exc}")
        return 1

    print(f"Provider: {settings.analysis.llm_provider}")
    print(f"Model: {settings.analysis.ollama_model}")
    print(f"Ollama URL: {settings.analysis.ollama_url}")
    print(f"Source EPUB: {plan.epub_path}")
    print(f"Source hash: {plan.source_hash}")
    print(f"Output directory: {plan.analysis_dir}")
    print(f"Cache: {'hit' if plan.cache_hit else 'miss'}")
    print(f"Analyzing: {plan.epub_path}")

    try:
        result = analyzer.analyze(Path(args.epub))
    except BookAnalysisError as exc:
        print(f"Analysis failed: {exc}")
        return 1

    print(f"Title: {result.story['title']}")
    print(f"Characters found: {len(result.story['characters'])}")
    print(f"Places found: {len(result.story['places'])}")
    print(f"Organizations found: {len(result.story['organizations'])}")
    print(f"Scenes detected: {len(result.scenes)}")
    print(f"Processing time: {result.processing_seconds:.2f}s")
    print(f"Cache hit: {'yes' if result.cache_hit else 'no'}")
    print(f"Analysis directory: {result.analysis_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
