from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import load_settings
from app.runner import BookConversionRunner
from app.story_analysis import StoryAnalyzer

from .config import load_web_settings
from .projects import ProjectManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a StoryForge web worker task.")
    parser.add_argument("action", choices=["analyze", "build"])
    parser.add_argument("--project-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    web_settings = load_web_settings()
    projects = ProjectManager(web_settings)
    project_dir = Path(args.project_dir)
    project = projects.load_project(project_dir.name)
    epub_path = projects.project_source_path(project)

    if args.action == "analyze":
        settings.paths.output_dir = project_dir / web_settings.work_dirname / "analysis"
        settings.paths.temp_dir = project_dir / web_settings.work_dirname / "analysis-temp"
        settings.paths.log_dir = project_dir / web_settings.project_logs_dirname
        analyzer = StoryAnalyzer(settings)
        result = analyzer.analyze(epub_path)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
        return 0

    settings.paths.output_dir = project_dir / web_settings.artifacts_dirname
    settings.paths.temp_dir = project_dir / web_settings.work_dirname / "temp"
    settings.paths.log_dir = project_dir / web_settings.project_logs_dirname
    runner = BookConversionRunner(settings)
    result = runner.run_book(epub_path)
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
