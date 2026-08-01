from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import WebSettings
from .models import JobStatus, ProjectRecord, now_iso
from .security import (
    atomic_write_json,
    ensure_within_root,
    safe_child,
    secure_filename,
    validate_slug,
)


class ProjectError(RuntimeError):
    pass


@dataclass
class ProjectPaths:
    root: Path
    input_dir: Path
    work_dir: Path
    artifacts_dir: Path
    logs_dir: Path
    project_file: Path
    status_file: Path
    build_log: Path


class ProjectManager:
    def __init__(self, settings: WebSettings) -> None:
        self.settings = settings
        for path in (
            settings.projects_dir,
            settings.books_dir,
            settings.cache_dir,
            settings.output_dir,
            settings.log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def project_paths(self, slug: str) -> ProjectPaths:
        slug = validate_slug(slug)
        root = safe_child(self.settings.projects_dir, slug)
        return ProjectPaths(
            root=root,
            input_dir=root / self.settings.input_dirname,
            work_dir=root / self.settings.work_dirname,
            artifacts_dir=root / self.settings.artifacts_dirname,
            logs_dir=root / self.settings.project_logs_dirname,
            project_file=root / self.settings.project_state_filename,
            status_file=root / self.settings.status_filename,
            build_log=root / self.settings.build_log_filename,
        )

    def list_projects(self) -> list[ProjectRecord]:
        items: list[ProjectRecord] = []
        if not self.settings.projects_dir.exists():
            return items
        for path in sorted(self.settings.projects_dir.iterdir(), reverse=True):
            if not path.is_dir():
                continue
            project_file = path / self.settings.project_state_filename
            if not project_file.exists():
                continue
            try:
                items.append(
                    ProjectRecord.from_dict(json.loads(project_file.read_text(encoding="utf-8")))
                )
            except Exception:
                continue
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items

    def load_project(self, slug: str) -> ProjectRecord:
        paths = self.project_paths(slug)
        if not paths.project_file.exists():
            raise ProjectError(f"project not found: {slug}")
        return ProjectRecord.from_dict(json.loads(paths.project_file.read_text(encoding="utf-8")))

    def load_status(self, slug: str) -> JobStatus:
        paths = self.project_paths(slug)
        if not paths.status_file.exists():
            return JobStatus.default(slug, str(paths.build_log))
        return JobStatus.from_dict(
            json.loads(paths.status_file.read_text(encoding="utf-8")),
            project_slug=slug,
            log_path=str(paths.build_log),
        )

    def save_project(self, record: ProjectRecord) -> None:
        paths = self.project_paths(record.project_slug)
        paths.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths.project_file, record.to_dict())

    def save_status(self, status: JobStatus) -> None:
        atomic_write_json(self.project_paths(status.project_slug).status_file, status.to_dict())

    def create_project(
        self,
        *,
        project_name: str,
        project_slug: str | None,
        source_filename: str,
        source_bytes: bytes,
    ) -> ProjectRecord:
        clean_name = project_name.strip()
        if not clean_name:
            raise ProjectError("project name is required")
        slug = validate_slug(
            project_slug or re.sub(r"[^a-z0-9]+", "-", clean_name.lower()).strip("-")
        )
        paths = self.project_paths(slug)
        if paths.project_file.exists():
            raise ProjectError(f"project already exists: {slug}")
        for path in (paths.input_dir, paths.work_dir, paths.artifacts_dir, paths.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        clean_filename = secure_filename(source_filename)
        if not clean_filename.lower().endswith(".epub"):
            raise ProjectError("source book must be an EPUB")
        source_path = self._write_unique_file(paths.input_dir, clean_filename, source_bytes)
        now = now_iso()
        record = ProjectRecord(
            project_id=uuid.uuid4().hex,
            project_slug=slug,
            project_name=clean_name,
            created_at=now,
            updated_at=now,
            source_book=str(source_path.relative_to(paths.root)),
            state="idle",
            last_build_id="",
            source_filename=clean_filename,
            analysis_state="not-run",
            selected_voice="",
            narrator="",
        )
        self.save_project(record)
        self.save_status(JobStatus.default(slug, str(paths.build_log)))
        return record

    def create_project_from_existing(
        self, *, project_name: str, project_slug: str | None, existing_book_path: Path
    ) -> ProjectRecord:
        return self.create_project(
            project_name=project_name,
            project_slug=project_slug,
            source_filename=existing_book_path.name,
            source_bytes=existing_book_path.read_bytes(),
        )

    def delete_project(self, slug: str) -> None:
        paths = self.project_paths(slug)
        if not paths.root.exists():
            raise ProjectError(f"project not found: {slug}")
        shutil.rmtree(paths.root)

    def project_source_path(self, record: ProjectRecord) -> Path:
        paths = self.project_paths(record.project_slug)
        source_path = (paths.root / record.source_book).resolve()
        ensure_within_root(paths.root, source_path)
        return source_path

    def read_build_log_tail(self, slug: str, limit: int = 120) -> list[str]:
        build_log = self.project_paths(slug).build_log
        if not build_log.exists():
            return []
        lines = build_log.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]

    def list_artifacts(self, slug: str) -> list[Path]:
        artifacts_dir = self.project_paths(slug).artifacts_dir
        if not artifacts_dir.exists():
            return []
        return [item for item in sorted(artifacts_dir.rglob("*")) if item.is_file()]

    def append_build_log(self, slug: str, text: str) -> None:
        build_log = self.project_paths(slug).build_log
        build_log.parent.mkdir(parents=True, exist_ok=True)
        with build_log.open("a", encoding="utf-8") as handle:
            handle.write(text)
            if text and not text.endswith("\n"):
                handle.write("\n")

    def _write_unique_file(self, directory: Path, filename: str, data: bytes) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        name = Path(filename).stem
        suffix = Path(filename).suffix
        candidate = directory / filename
        counter = 1
        while candidate.exists():
            candidate = directory / f"{name}-{counter}{suffix}"
            counter += 1
        candidate.write_bytes(data)
        ensure_within_root(directory, candidate)
        return candidate
