from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ProjectRecord:
    project_id: str
    project_slug: str
    project_name: str
    created_at: str
    updated_at: str
    source_book: str
    state: str = "idle"
    last_build_id: str = ""
    source_filename: str = ""
    analysis_state: str = "not-run"
    selected_voice: str = ""
    narrator: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectRecord:
        return cls(
            project_id=str(data.get("project_id", "")),
            project_slug=str(data.get("project_slug", "")),
            project_name=str(data.get("project_name", "")),
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
            source_book=str(data.get("source_book", "")),
            state=str(data.get("state", "idle")),
            last_build_id=str(data.get("last_build_id", "")),
            source_filename=str(data.get("source_filename", "")),
            analysis_state=str(data.get("analysis_state", "not-run")),
            selected_voice=str(data.get("selected_voice", "")),
            narrator=str(data.get("narrator", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_slug": self.project_slug,
            "project_name": self.project_name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_book": self.source_book,
            "state": self.state,
            "last_build_id": self.last_build_id,
            "source_filename": self.source_filename,
            "analysis_state": self.analysis_state,
            "selected_voice": self.selected_voice,
            "narrator": self.narrator,
        }


@dataclass
class JobStatus:
    project_slug: str
    job_id: str = ""
    action: str = ""
    status: str = "idle"
    stage: str = "idle"
    current_chapter: int | None = None
    total_chapters: int | None = None
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    updated_at: str = ""
    return_code: int | None = None
    pid: int | None = None
    log_path: str = ""
    source_book: str = ""
    active: bool = False
    build_log_tail: list[str] = field(default_factory=list)

    @classmethod
    def default(cls, project_slug: str, log_path: str) -> JobStatus:
        now = now_iso()
        return cls(
            project_slug=project_slug,
            status="idle",
            stage="idle",
            updated_at=now,
            log_path=log_path,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, project_slug: str, log_path: str) -> JobStatus:
        return cls(
            project_slug=project_slug,
            job_id=str(data.get("job_id", "")),
            action=str(data.get("action", "")),
            status=str(data.get("status", "idle")),
            stage=str(data.get("stage", "idle")),
            current_chapter=data.get("current_chapter"),
            total_chapters=data.get("total_chapters"),
            message=str(data.get("message", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            updated_at=str(data.get("updated_at", now_iso())),
            return_code=data.get("return_code"),
            pid=data.get("pid"),
            log_path=str(data.get("log_path", log_path)),
            source_book=str(data.get("source_book", "")),
            active=bool(data.get("active", False)),
            build_log_tail=list(data.get("build_log_tail", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_slug": self.project_slug,
            "job_id": self.job_id,
            "action": self.action,
            "status": self.status,
            "stage": self.stage,
            "current_chapter": self.current_chapter,
            "total_chapters": self.total_chapters,
            "message": self.message,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "updated_at": self.updated_at,
            "return_code": self.return_code,
            "pid": self.pid,
            "log_path": self.log_path,
            "source_book": self.source_book,
            "active": self.active,
            "build_log_tail": list(self.build_log_tail),
        }
