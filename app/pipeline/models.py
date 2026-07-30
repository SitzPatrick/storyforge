from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class BuildStage(str, Enum):
    PLAN = "plan"
    APPLY_EDITS = "apply-edits"
    MANIFEST = "manifest"
    RENDER = "render"
    ASSEMBLE = "assemble"
    MASTER = "master"
    PACKAGE = "package"


class BuildTarget(str, Enum):
    PLAN = "plan"
    APPLY_EDITS = "apply-edits"
    MANIFEST = "manifest"
    RENDER = "render"
    ASSEMBLE = "assemble"
    MASTER = "master"
    PACKAGE = "package"


class RebuildPolicy(str, Enum):
    NORMAL = "normal"
    REBUILD_CURRENT_STAGE = "rebuild-current-stage"
    REBUILD_FROM_STAGE = "rebuild-from-stage"
    REBUILD_ALL = "rebuild-all"


class StageAction(str, Enum):
    EXECUTE = "execute"
    CACHE_CHECK = "cache-check"
    REUSE = "reuse"
    SKIP_NOT_REQUESTED = "skip-not-requested"
    BLOCK_DEPENDENCY = "block-dependency"
    FORCE_REBUILD = "force-rebuild"
    DRY_RUN_ONLY = "dry-run-only"


class StageStatus(str, Enum):
    NOT_REQUESTED = "not-requested"
    PENDING = "pending"
    REUSED = "reused"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed-with-warnings"
    BLOCKED = "blocked"
    FAILED = "failed"
    SKIPPED_DUE_TO_DEPENDENCY = "skipped-due-to-dependency"
    DRY_RUN = "dry-run"


class BuildCompletionStatus(str, Enum):
    COMPLETE = "complete"
    COMPLETE_WITH_WARNINGS = "complete-with-warnings"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"
    DRY_RUN = "dry-run"


class BuildFailureType(str, Enum):
    INVALID_BUILD_REQUEST = "invalid_build_request"
    INVALID_TARGET_STAGE = "invalid_target_stage"
    INVALID_REBUILD_POLICY = "invalid_rebuild_policy"
    UNSAFE_WORKSPACE_PATH = "unsafe_workspace_path"
    MISSING_STAGE_ADAPTER = "missing_stage_adapter"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    DEPENDENCY_BLOCKED = "dependency_blocked"
    STAGE_INVOCATION_FAILURE = "stage_invocation_failure"
    STAGE_RESULT_INVALID = "stage_result_invalid"
    ARTIFACT_LINEAGE_MISMATCH = "artifact_lineage_mismatch"
    BUILD_REPORT_WRITE_FAILURE = "build_report_write_failure"
    INTERRUPTED_BUILD = "interrupted_build"
    UNKNOWN_ORCHESTRATION_FAILURE = "unknown_orchestration_failure"


@dataclass(frozen=True)
class BuildFailure:
    failure_type: BuildFailureType
    message: str
    stage: BuildStage | None = None
    retryable: bool = False
    blocking: bool = True
    dependency_stage: BuildStage | None = None
    original_failure_type: str | None = None
    original_message: str | None = None
    backend_diagnostic_excerpt: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ArtifactRef:
    stage: BuildStage
    relative_path: str
    content_hash: str | None = None
    identity: str | None = None
    report_relative_path: str | None = None


@dataclass(frozen=True)
class StageInspection:
    stage: BuildStage
    requested: bool
    dependency_status: StageStatus
    input_identity: str
    output_identity: str
    prior_artifact_state: str
    cache_reusable: bool
    action: StageAction
    reason: str
    artifact_refs: tuple[ArtifactRef, ...] = ()
    report_ref: str | None = None
    warnings: tuple[str, ...] = ()
    failures: tuple[BuildFailure, ...] = ()
    blocking: bool = False


@dataclass(frozen=True)
class StageResult:
    stage: BuildStage
    status: StageStatus
    action: StageAction
    started: bool
    completed: bool
    cache_reused: bool
    new_artifacts: bool
    artifact_refs: tuple[ArtifactRef, ...] = ()
    stage_report_ref: str | None = None
    input_identity: str = ""
    output_identity: str = ""
    warnings: tuple[str, ...] = ()
    failures: tuple[BuildFailure, ...] = ()
    blocking: bool = False
    duration_seconds: float = 0.0
    invocation_count: int = 0
    requested: bool = True
    dependency_status: StageStatus = StageStatus.PENDING
    reason: str = ""


@dataclass(frozen=True)
class BuildPlanStage:
    stage: BuildStage
    requested: bool
    dependencies: tuple[BuildStage, ...]
    dependency_status: StageStatus
    expected_input_identity: str
    known_prior_artifact_state: str
    intended_action: StageAction
    reason: str
    cache_reusable: bool
    force_rebuild: bool
    dry_run: bool
    inspection: StageInspection | None = None


@dataclass(frozen=True)
class BuildPlan:
    build_id: str
    project_id: str
    book_id: str
    target_stage: BuildTarget
    rebuild_policy: RebuildPolicy
    dry_run: bool
    stages: tuple[BuildPlanStage, ...]
    warnings: tuple[str, ...] = ()
    failures: tuple[BuildFailure, ...] = ()


@dataclass(frozen=True)
class ArtifactLineage:
    build_id: str
    project_id: str
    book_id: str
    effective_voice_plan_identity: str | None = None
    synthesis_manifest_identity: str | None = None
    render_report_identity: str | None = None
    chapter_assembly_ids: tuple[str, ...] = ()
    mastered_chapter_ids: tuple[str, ...] = ()
    audiobook_package_id: str | None = None
    stage_input_hashes: dict[str, str] = field(default_factory=dict)
    stage_output_hashes: dict[str, str] = field(default_factory=dict)
    artifact_relative_paths: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildReport:
    build_id: str
    project_id: str
    book_id: str
    pipeline_contract_version: int
    orchestrator_version: str
    target_stage: BuildTarget
    rebuild_policy: RebuildPolicy
    dry_run: bool
    completion_status: BuildCompletionStatus
    stages: tuple[StageResult, ...]
    final_artifact_ref: ArtifactRef | None = None
    artifact_lineage: ArtifactLineage | None = None
    cache_reuse_summary: dict[str, int] = field(default_factory=dict)
    stages_executed: tuple[str, ...] = ()
    stages_reused: tuple[str, ...] = ()
    stages_blocked: tuple[str, ...] = ()
    stages_failed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    report_path: Path | None = None
    report_identity: str | None = None
    lineages: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildRequest:
    project_id: str
    book_id: str
    story_input: Any
    voice_planning_config: Mapping[str, Any]
    editable_plan: Mapping[str, Any] | None
    manifest_config: Mapping[str, Any]
    renderer_config: Mapping[str, Any]
    assembler_config: Mapping[str, Any]
    mastering_config: Mapping[str, Any]
    packaging_config: Mapping[str, Any]
    canonical_chapter_structure: tuple[Mapping[str, Any], ...] = ()
    cover_art: Mapping[str, Any] | None = None
    target_stage: BuildTarget = BuildTarget.PACKAGE
    rebuild_policy: RebuildPolicy = RebuildPolicy.NORMAL
    dry_run: bool = False
    failure_policy: str = "stop-on-blocking-failure"
    workspace_root: Path = Path("projects")
    pipeline_contract_version: int = 1
    orchestrator_version: str = "milestone-15"
    stage_overrides: Mapping[str, Any] = field(default_factory=dict)

    def build_id(self) -> str:
        from .validation import build_request_identity

        return build_request_identity(self)
