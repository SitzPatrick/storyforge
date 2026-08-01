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
    # Artifact pointers are deliberately optional: older project.json files do not
    # contain them and are migrated on read without rewriting user data.
    analysis_path: str = ""
    analysis_status: str = "not-run"
    normalized_path: str = ""
    normalized_analysis_path: str = ""
    normalization_status: str = "not-run"
    character_profiles_path: str = ""
    voice_plan_path: str = ""
    voice_plan_status: str = "not-run"
    voice_assignment_report_path: str = ""
    manifest_path: str = ""
    synthesis_manifest_path: str = ""
    synthesis_manifest_status: str = "not-run"
    build_mode: str = "character-aware"
    last_pipeline_build_id: str = ""
    pipeline_contract_version: int = 1
    series_id: str = ""
    cover_art_path: str = ""
    artifact_map: dict[str, str] = field(default_factory=dict)
    orchestrator_version: str = "storyforge-pipeline"

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
            analysis_path=str(data.get("analysis_path", "")),
            analysis_status=str(
                data.get("analysis_status", "completed" if data.get("analysis_path") else "not-run")
            ),
            normalized_path=str(
                data.get("normalized_path", data.get("normalized_analysis_path", ""))
            ),
            normalized_analysis_path=str(
                data.get("normalized_analysis_path", data.get("normalized_path", ""))
            ),
            normalization_status=str(
                data.get(
                    "normalization_status",
                    (
                        "completed"
                        if data.get("normalized_path") or data.get("normalized_analysis_path")
                        else "not-run"
                    ),
                )
            ),
            character_profiles_path=str(data.get("character_profiles_path", "")),
            voice_plan_path=str(data.get("voice_plan_path", "")),
            voice_plan_status=str(
                data.get(
                    "voice_plan_status", "completed" if data.get("voice_plan_path") else "not-run"
                )
            ),
            voice_assignment_report_path=str(data.get("voice_assignment_report_path", "")),
            manifest_path=str(data.get("manifest_path", data.get("synthesis_manifest_path", ""))),
            synthesis_manifest_path=str(
                data.get("synthesis_manifest_path", data.get("manifest_path", ""))
            ),
            synthesis_manifest_status=str(
                data.get(
                    "synthesis_manifest_status",
                    (
                        "completed"
                        if data.get("manifest_path") or data.get("synthesis_manifest_path")
                        else "not-run"
                    ),
                )
            ),
            build_mode=str(
                data.get(
                    "build_mode",
                    (
                        "legacy/single-voice"
                        if data.get("last_build_id") and not data.get("voice_plan_path")
                        else "character-aware"
                    ),
                )
            ),
            last_pipeline_build_id=str(
                data.get("last_pipeline_build_id", data.get("last_build_id", ""))
            ),
            pipeline_contract_version=int(data.get("pipeline_contract_version", 1)),
            series_id=str(data.get("series_id", "")),
            cover_art_path=str(data.get("cover_art_path", "")),
            artifact_map={str(k): str(v) for k, v in (data.get("artifact_map") or {}).items()},
            orchestrator_version=str(data.get("orchestrator_version", "storyforge-pipeline")),
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
            "analysis_path": self.analysis_path,
            "analysis_status": self.analysis_status,
            "normalized_path": self.normalized_path,
            "normalized_analysis_path": self.normalized_analysis_path,
            "normalization_status": self.normalization_status,
            "character_profiles_path": self.character_profiles_path,
            "voice_plan_path": self.voice_plan_path,
            "voice_plan_status": self.voice_plan_status,
            "voice_assignment_report_path": self.voice_assignment_report_path,
            "manifest_path": self.manifest_path,
            "synthesis_manifest_path": self.synthesis_manifest_path,
            "synthesis_manifest_status": self.synthesis_manifest_status,
            "build_mode": self.build_mode,
            "last_pipeline_build_id": self.last_pipeline_build_id,
            "pipeline_contract_version": self.pipeline_contract_version,
            "series_id": self.series_id,
            "cover_art_path": self.cover_art_path,
            "artifact_map": dict(self.artifact_map),
            "orchestrator_version": self.orchestrator_version,
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
