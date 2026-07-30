from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import (
    ArtifactRef,
    BuildFailure,
    BuildFailureType,
    BuildRequest,
    BuildStage,
    StageAction,
    StageInspection,
    StageResult,
    StageStatus,
)
from .serialization import canonical_json, canonicalize


@dataclass(frozen=True)
class StageContext:
    build_id: str
    workspace_root: Path
    project_root: Path
    stage_root: Path
    report_root: Path
    upstream_stage_results: Mapping[BuildStage, Any] = field(default_factory=dict)
    target_stage: BuildStage = BuildStage.PACKAGE
    dry_run: bool = False
    rebuild_policy: str = "normal"


_MISSING = object()


def _get_nested_value(mapping: Mapping[str, Any], dotted_path: str) -> Any:
    current: Any = mapping
    for part in dotted_path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return _MISSING
        current = current[part]
    return current


class StageAdapter(Protocol):
    stage: BuildStage
    dependencies: tuple[BuildStage, ...]

    def input_identity(self, request: BuildRequest, context: StageContext) -> str: ...

    def inspect(self, request: BuildRequest, context: StageContext, *, expected_input_identity: str, force_rebuild: bool = False) -> StageInspection: ...

    def execute(self, request: BuildRequest, context: StageContext, *, expected_input_identity: str, force_rebuild: bool = False, dry_run: bool = False) -> StageResult: ...


@dataclass
class FakeStageAdapter:
    stage: BuildStage
    dependencies: tuple[BuildStage, ...] = ()
    identity_fields: tuple[str, ...] = ()
    upstream_identity_sources: tuple[BuildStage, ...] = ()
    available: bool = True
    validation_ok: bool = True
    warning_messages: tuple[str, ...] = ()
    execution_failure: BuildFailure | None = None
    artifact_suffix: str = ".json"
    report_suffix: str = ".report.json"
    input_channel: str = "config"
    output_seed: str = ""
    invocation_count: int = 0
    inspection_count: int = 0
    cache: dict[str, StageResult] = field(default_factory=dict)
    artifact_exists: bool = True
    corrupt_cached_output: bool = False
    stage_result_status: StageStatus = StageStatus.COMPLETED

    def input_identity(self, request: BuildRequest, context: StageContext) -> str:
        payload = self._input_payload(request, context)
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def inspect(self, request: BuildRequest, context: StageContext, *, expected_input_identity: str, force_rebuild: bool = False) -> StageInspection:
        self.inspection_count += 1
        cached = self.cache.get(expected_input_identity)
        cache_reusable = bool(cached and self.artifact_exists and not force_rebuild and self.validation_ok)
        prior_state = "missing"
        output_identity = ""
        if cached:
            prior_state = "present" if self.artifact_exists else "missing"
            output_identity = cached.output_identity
            if self.corrupt_cached_output:
                prior_state = "corrupt"
                cache_reusable = False
        action = StageAction.REUSE if cache_reusable else StageAction.EXECUTE
        reason = "validated cache reusable" if cache_reusable else "cache miss or rebuild required"
        if force_rebuild:
            action = StageAction.FORCE_REBUILD
            reason = "forced rebuild"
        if not self.available:
            reason = "adapter unavailable"
        if not output_identity:
            output_identity = expected_input_identity
        return StageInspection(
            stage=self.stage,
            requested=True,
            dependency_status=StageStatus.PENDING,
            input_identity=expected_input_identity,
            output_identity=output_identity,
            prior_artifact_state=prior_state,
            cache_reusable=cache_reusable,
            action=action,
            reason=reason,
            artifact_refs=tuple(cached.artifact_refs) if cached else (),
            report_ref=cached.stage_report_ref if cached else None,
            warnings=self.warning_messages,
            failures=(self.execution_failure,) if self.execution_failure else (),
            blocking=not self.available,
        )

    def execute(self, request: BuildRequest, context: StageContext, *, expected_input_identity: str, force_rebuild: bool = False, dry_run: bool = False) -> StageResult:
        started = not dry_run
        if dry_run:
            return StageResult(
                stage=self.stage,
                status=StageStatus.DRY_RUN,
                action=StageAction.DRY_RUN_ONLY,
                started=False,
                completed=False,
                cache_reused=False,
                new_artifacts=False,
                artifact_refs=(),
                stage_report_ref=None,
                input_identity=expected_input_identity,
                output_identity=expected_input_identity,
                warnings=self.warning_messages,
                failures=(),
                blocking=False,
                duration_seconds=0.0,
                invocation_count=self.invocation_count,
                requested=True,
                dependency_status=StageStatus.PENDING,
                reason="dry run",
            )
        self.invocation_count += 1
        if not self.available:
            failure = BuildFailure(
                failure_type=BuildFailureType.DEPENDENCY_UNAVAILABLE,
                message=f"{self.stage.value} adapter unavailable",
                stage=self.stage,
                retryable=False,
            )
            return StageResult(
                stage=self.stage,
                status=StageStatus.BLOCKED,
                action=StageAction.EXECUTE,
                started=True,
                completed=False,
                cache_reused=False,
                new_artifacts=False,
                artifact_refs=(),
                stage_report_ref=None,
                input_identity=expected_input_identity,
                output_identity="",
                warnings=self.warning_messages,
                failures=(failure,),
                blocking=True,
                duration_seconds=0.0,
                invocation_count=self.invocation_count,
                requested=True,
                dependency_status=StageStatus.BLOCKED,
                reason="adapter unavailable",
            )
        if self.execution_failure is not None:
            failure = self.execution_failure
            return StageResult(
                stage=self.stage,
                status=StageStatus.FAILED,
                action=StageAction.FORCE_REBUILD if force_rebuild else StageAction.EXECUTE,
                started=True,
                completed=False,
                cache_reused=False,
                new_artifacts=False,
                artifact_refs=(),
                stage_report_ref=None,
                input_identity=expected_input_identity,
                output_identity="",
                warnings=self.warning_messages,
                failures=(failure,),
                blocking=failure.blocking,
                duration_seconds=0.0,
                invocation_count=self.invocation_count,
                requested=True,
                dependency_status=StageStatus.PENDING,
                reason=failure.message,
            )
        artifact_path = context.stage_root / f"{self.stage.value}{self.artifact_suffix}"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        output_identity = hashlib.sha256(
            canonical_json({"stage": self.stage.value, "input": expected_input_identity, "seed": self.output_seed}).encode("utf-8")
        ).hexdigest()
        payload = {
            "stage": self.stage.value,
            "input_identity": expected_input_identity,
            "output_identity": output_identity,
            "build_id": context.build_id,
        }
        artifact_path.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        report_path = context.report_root / f"{self.stage.value}{self.report_suffix}"
        report_path.write_text(canonical_json({"stage": self.stage.value, "output_identity": output_identity}) + "\n", encoding="utf-8")
        artifact_ref = ArtifactRef(stage=self.stage, relative_path=str(artifact_path.relative_to(context.project_root)), content_hash=hashlib.sha256(artifact_path.read_bytes()).hexdigest(), identity=output_identity, report_relative_path=str(report_path.relative_to(context.project_root)))
        result = StageResult(
            stage=self.stage,
            status=self.stage_result_status,
            action=StageAction.FORCE_REBUILD if force_rebuild else StageAction.EXECUTE,
            started=started,
            completed=True,
            cache_reused=False,
            new_artifacts=True,
            artifact_refs=(artifact_ref,),
            stage_report_ref=str(report_path.relative_to(context.project_root)),
            input_identity=expected_input_identity,
            output_identity=output_identity,
            warnings=self.warning_messages,
            failures=(),
            blocking=False,
            duration_seconds=0.0,
            invocation_count=self.invocation_count,
            requested=True,
            dependency_status=StageStatus.PENDING,
            reason="executed",
        )
        self.cache[expected_input_identity] = result
        return result

    def _input_payload(self, request: BuildRequest, context: StageContext) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "project_id": request.project_id,
            "book_id": request.book_id,
            "stage": self.stage.value,
            "target_stage": request.target_stage.value,
            "story_input": request.story_input,
            "voice_planning_config": request.voice_planning_config,
            "editable_plan": request.editable_plan,
            "manifest_config": request.manifest_config,
            "renderer_config": request.renderer_config,
            "assembler_config": request.assembler_config,
            "mastering_config": request.mastering_config,
            "packaging_config": request.packaging_config,
            "canonical_chapter_structure": request.canonical_chapter_structure,
            "cover_art": request.cover_art,
        }
        selected: dict[str, Any] = {}
        for field in self.identity_fields:
            value = _get_nested_value(request_payload, field)
            if value is not _MISSING:
                selected[field] = canonicalize(value)
        upstream: dict[str, Any] = {}
        for source in self.upstream_identity_sources:
            upstream_result = context.upstream_stage_results.get(source)
            upstream[source.value] = upstream_result.output_identity if upstream_result else None
        return {
            "selected": selected,
            "upstream": upstream,
            "build_id": context.build_id,
            "stage": self.stage.value,
        }
