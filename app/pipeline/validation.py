from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from .graph import STAGE_ORDER, stage_index
from .models import BuildFailure, BuildFailureType, BuildRequest, BuildStage, BuildTarget, RebuildPolicy
from .serialization import canonical_json, canonicalize


class BuildRequestValidationError(ValueError):
    pass


_SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:")


def _validate_identifier(label: str, value: str) -> None:
    if not value or value.strip() != value:
        raise BuildRequestValidationError(f"{label} is required and must not contain leading/trailing whitespace")
    if any(part in value for part in ("..", "/", "\\")):
        raise BuildRequestValidationError(f"{label} contains unsafe path traversal characters")
    if not set(value) <= _SAFE_ID_CHARS:
        raise BuildRequestValidationError(f"{label} contains unsupported characters")


def _validate_workspace_root(path: Path) -> Path:
    original = Path(path)
    if any(part == ".." for part in original.parts):
        raise BuildRequestValidationError(f"unsafe workspace root: {path}")
    resolved = original.expanduser().resolve(strict=False)
    if any(part == ".." for part in resolved.parts):
        raise BuildRequestValidationError(f"unsafe workspace root: {path}")
    return resolved


def _validate_mapping(label: str, value: Any) -> None:
    if value is None:
        raise BuildRequestValidationError(f"{label} is required")
    if not isinstance(value, Mapping):
        raise BuildRequestValidationError(f"{label} must be a mapping")


def validate_build_request(request: BuildRequest) -> BuildRequest:
    _validate_identifier("project_id", request.project_id)
    _validate_identifier("book_id", request.book_id)
    _validate_workspace_root(request.workspace_root)
    if not isinstance(request.target_stage, BuildTarget):
        raise BuildRequestValidationError(f"invalid target stage: {request.target_stage!r}")
    if not isinstance(request.rebuild_policy, RebuildPolicy):
        raise BuildRequestValidationError(f"invalid rebuild policy: {request.rebuild_policy!r}")
    if not isinstance(request.pipeline_contract_version, int) or request.pipeline_contract_version <= 0:
        raise BuildRequestValidationError("pipeline_contract_version must be a positive integer")
    if not isinstance(request.orchestrator_version, str) or not request.orchestrator_version.strip():
        raise BuildRequestValidationError("orchestrator_version is required")
    _validate_mapping("voice_planning_config", request.voice_planning_config)
    _validate_mapping("manifest_config", request.manifest_config)
    _validate_mapping("renderer_config", request.renderer_config)
    _validate_mapping("assembler_config", request.assembler_config)
    _validate_mapping("mastering_config", request.mastering_config)
    _validate_mapping("packaging_config", request.packaging_config)
    if request.editable_plan is not None:
        _validate_mapping("editable_plan", request.editable_plan)
    if not isinstance(request.canonical_chapter_structure, tuple):
        raise BuildRequestValidationError("canonical_chapter_structure must be a tuple of mappings")
    for item in request.canonical_chapter_structure:
        _validate_mapping("canonical_chapter_structure entry", item)
    if request.cover_art is not None:
        _validate_mapping("cover_art", request.cover_art)
    return request


def build_request_identity(request: BuildRequest) -> str:
    payload = {
        "project_id": request.project_id,
        "book_id": request.book_id,
        "pipeline_contract_version": request.pipeline_contract_version,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def safe_workspace_paths(request: BuildRequest, build_id: str) -> dict[str, Path]:
    root = _validate_workspace_root(request.workspace_root)
    project_root = root / request.project_id / request.book_id / build_id
    stage_roots = {stage.value: project_root / stage.value for stage in STAGE_ORDER}
    report_path = project_root / "build_report.json"
    return {"workspace_root": root, "project_root": project_root, "report_path": report_path, **{f"stage:{key}": value for key, value in stage_roots.items()}}


def failure_from_exception(stage: BuildStage | None, exc: Exception, *, retryable: bool = False) -> BuildFailure:
    return BuildFailure(
        failure_type=BuildFailureType.UNKNOWN_ORCHESTRATION_FAILURE,
        message=str(exc),
        stage=stage,
        retryable=retryable,
        original_failure_type=type(exc).__name__,
        original_message=str(exc),
    )
