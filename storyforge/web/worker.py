from __future__ import annotations

import argparse
from pathlib import Path

from .application import WebApplicationError, WebApplicationService
from .config import load_web_settings
from .projects import ProjectManager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a StoryForge web worker task.")
    parser.add_argument("action", choices=["analyze", "normalize", "plan", "manifest", "build"])
    parser.add_argument("--project-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    web_settings = load_web_settings()
    projects = ProjectManager(web_settings)
    project_dir = Path(args.project_dir)
    project = projects.load_project(project_dir.name)
    try:
        result = WebApplicationService(web_settings, projects).run(project, args.action)
    except WebApplicationError as exc:
        print(f"BLOCKED: {exc}")
        return 2
    output_path = result.get("analysis_dir", result.get("output_dir", "application layer"))
    print(f"{args.action} completed: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
