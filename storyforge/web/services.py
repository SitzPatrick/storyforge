from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import load_settings
from app.diagnostics import _check_binary, _check_dir_writable
from app.kokoro_client import KokoroClient

from .config import WebSettings, load_web_settings
from .jobs import JobManager
from .application import WebApplicationService
from .projects import ProjectManager


@dataclass
class WebServices:
    settings: WebSettings
    projects: ProjectManager
    jobs: JobManager
    application: WebApplicationService

    @classmethod
    def create(cls, settings: WebSettings | None = None) -> WebServices:
        settings = settings or load_web_settings()
        projects = ProjectManager(settings)
        jobs = JobManager(settings, projects)
        return cls(
            settings=settings,
            projects=projects,
            jobs=jobs,
            application=WebApplicationService(settings, projects),
        )

    def diagnostics(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        errors: list[str] = []
        try:
            version = __import__("storyforge").__version__
        except Exception:
            version = "unknown"
        checks.append({"name": "StoryForge version", "status": "ok", "value": version})
        checks.append(
            {"name": "Python version", "status": "ok", "value": os.sys.version.split()[0]}
        )
        story_settings = load_settings()
        checks.append(
            {
                "name": "Configuration",
                "status": "ok",
                "value": (
                    str(Path("config/config.yaml"))
                    if Path("config/config.yaml").exists()
                    else str(story_settings.paths.output_dir)
                ),
            }
        )
        for path in self.settings.mapped_directories:
            try:
                _check_dir_writable(path)
                checks.append({"name": f"writable: {path}", "status": "ok"})
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
                checks.append({"name": f"writable: {path}", "status": "fail", "error": str(exc)})
        try:
            ffmpeg = _check_binary("ffmpeg")
            checks.append({"name": "FFmpeg availability", "status": "ok", "value": ffmpeg})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "FFmpeg availability", "status": "fail", "error": str(exc)})
            errors.append(str(exc))
        try:
            client = KokoroClient(base_url=self.settings.kokoro_url, api_key="not-needed")
            reachable = client.health_check()
            checks.append({"name": "Kokoro reachability", "status": "ok", "value": reachable})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "Kokoro reachability", "status": "warn", "error": str(exc)})
        active = self.jobs.active_status()
        checks.append(
            {
                "name": "Active build state",
                "status": "ok" if active else "idle",
                "value": active.to_dict() if active else None,
            }
        )
        return {"checks": checks, "errors": errors, "status": "ok" if not errors else "warn"}
