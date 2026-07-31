from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WebSettings:
    host: str
    port: int
    config_dir: Path
    data_dir: Path
    books_dir: Path
    projects_dir: Path
    cache_dir: Path
    output_dir: Path
    log_dir: Path
    kokoro_url: str
    max_upload_bytes: int
    project_state_filename: str = "project.json"
    status_filename: str = "status.json"
    build_log_filename: str = "build.log"
    work_dirname: str = "work"
    input_dirname: str = "input"
    artifacts_dirname: str = "artifacts"
    project_logs_dirname: str = "logs"

    @property
    def mapped_directories(self) -> tuple[Path, ...]:
        return (
            self.config_dir,
            self.books_dir,
            self.projects_dir,
            self.cache_dir,
            self.output_dir,
            self.log_dir,
        )


def _path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser()


def load_web_settings() -> WebSettings:
    return WebSettings(
        host=os.getenv("STORYFORGE_HOST", "0.0.0.0"),
        port=int(os.getenv("STORYFORGE_PORT", "8787")),
        config_dir=_path("STORYFORGE_CONFIG_DIR", "/config"),
        data_dir=_path("STORYFORGE_DATA_DIR", "/data"),
        books_dir=_path("STORYFORGE_BOOKS_DIR", "/data/books"),
        projects_dir=_path("STORYFORGE_PROJECTS_DIR", "/data/projects"),
        cache_dir=_path("STORYFORGE_CACHE_DIR", "/data/cache"),
        output_dir=_path("STORYFORGE_OUTPUT_DIR", "/data/output"),
        log_dir=_path("STORYFORGE_LOG_DIR", "/data/logs"),
        kokoro_url=os.getenv("KOKORO_URL", os.getenv("KOKORO_API_URL", "http://kokoro:8880")),
        max_upload_bytes=int(os.getenv("STORYFORGE_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))),
    )
