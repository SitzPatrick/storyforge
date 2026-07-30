from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from .adapters import FakeStageAdapter, StageAdapter, StageContext
from .graph import STAGE_ORDER, dependency_chain, is_stage_requested, stage_index
from .models import (
    ArtifactRef,
    ArtifactLineage,
    BuildCompletionStatus,
    BuildFailure,
    BuildFailureType,
    BuildPlan,
    BuildPlanStage,
    BuildReport,
    BuildRequest,
    BuildStage,
    BuildTarget,
    RebuildPolicy,
    StageAction,
    StageInspection,
    StageResult,
    StageStatus,
)
from .planner import plan_build
from .serialization import canonical_json, canonicalize
from .validation import build_request_identity, failure_from_exception, safe_workspace_paths, validate_build_request


class PipelineOrchestratorError(RuntimeError):
    pass


class PipelineOrchestrator:
    def __init__(self, adapters: Mapping[BuildStage, StageAdapter], *, report_filename: str = "build_report.json") -> None:
        self.adapters = dict(adapters)
        self.report_filename = report_filename

    def plan(self, request: BuildRequest) -> BuildPlan:
        return plan_build(request, self.adapters)

    def build_storyforge_project(self, request: BuildRequest) -> BuildReport:
        request = validate_build_request(request)
        build_id = build_request_identity(request)
        plan = self.plan(request)
        workspace = safe_workspace_paths(request, build_id)
        stage_roots = {stage: workspace[f"stage:{stage.value}"] for stage in BuildStage}
        actual_report_path = workspace["project_root"] / self.report_filename
        report_path = Path(self.report_filename)
        if request.dry_run:
            report = self._report_from_plan(request, plan, build_id, stage_roots, report_path, dry_run=True)
            return report

        stage_results: list[StageResult] = []
        upstream_results: dict[BuildStage, StageResult] = {}
        warnings: list[str] = list(plan.warnings)
        errors: list[str] = []
        blocked = False
        stage_input_hashes: dict[str, str] = {}
        stage_output_hashes: dict[str, str] = {}
        stage_artifact_paths: dict[str, str] = {}
        chapter_ids: list[str] = []
        mastered_ids: list[str] = []
        final_artifact_ref: ArtifactRef | None = None
        last_executed_or_reused: StageResult | None = None

        for plan_stage in plan.stages:
            stage = plan_stage.stage
            adapter = self.adapters.get(stage)
            if not plan_stage.requested:
                result = self._not_requested_result(stage, plan_stage)
                stage_results.append(result)
                continue
            if blocked or plan_stage.dependency_status in {StageStatus.BLOCKED, StageStatus.FAILED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY}:
                result = self._dependency_blocked_result(stage, plan_stage)
                stage_results.append(result)
                blocked = True
                continue
            if adapter is None:
                failure = BuildFailure(BuildFailureType.MISSING_STAGE_ADAPTER, f"missing stage adapter: {stage.value}", stage=stage)
                result = StageResult(
                    stage=stage,
                    status=StageStatus.BLOCKED,
                    action=StageAction.BLOCK_DEPENDENCY,
                    started=False,
                    completed=False,
                    cache_reused=False,
                    new_artifacts=False,
                    artifact_refs=(),
                    stage_report_ref=None,
                    input_identity=plan_stage.expected_input_identity,
                    output_identity="",
                    warnings=(),
                    failures=(failure,),
                    blocking=True,
                    duration_seconds=0.0,
                    invocation_count=0,
                    requested=True,
                    dependency_status=plan_stage.dependency_status,
                    reason=failure.message,
                )
                stage_results.append(result)
                errors.append(failure.message)
                blocked = True
                continue

            context = StageContext(
                build_id=build_id,
                workspace_root=workspace["workspace_root"],
                project_root=workspace["project_root"],
                stage_root=stage_roots[stage],
                report_root=workspace["project_root"],
                upstream_stage_results=upstream_results,
                target_stage=BuildStage(request.target_stage.value),
                dry_run=request.dry_run,
                rebuild_policy=request.rebuild_policy.value,
            )
            result = self._execute_stage(request, plan_stage, adapter, context)
            stage_results.append(result)
            if result.warnings:
                warnings.extend(result.warnings)
            if result.failures:
                errors.extend(failure.message for failure in result.failures)
            if result.status in {StageStatus.FAILED, StageStatus.BLOCKED} and result.blocking:
                blocked = True
            if result.completed:
                upstream_results[stage] = result
                stage_input_hashes[stage.value] = result.input_identity
                stage_output_hashes[stage.value] = result.output_identity
                for ref in result.artifact_refs:
                    stage_artifact_paths[f"{stage.value}:{ref.relative_path}"] = ref.relative_path
                if stage == BuildStage.MANIFEST and result.output_identity:
                    stage_artifact_paths["voice_plan"] = result.output_identity
                if stage == BuildStage.RENDER and result.output_identity:
                    stage_artifact_paths["render_report"] = result.output_identity
                if stage == BuildStage.ASSEMBLE:
                    chapter_ids.extend(ref.identity or "" for ref in result.artifact_refs)
                if stage == BuildStage.MASTER:
                    mastered_ids.extend(ref.identity or "" for ref in result.artifact_refs)
                last_executed_or_reused = result
                if stage == BuildStage.PACKAGE and result.artifact_refs:
                    final_artifact_ref = result.artifact_refs[0]
            if request.failure_policy == "stop-on-blocking-failure" and blocked:
                continue

        if final_artifact_ref is None:
            for result in reversed(stage_results):
                if result.completed and result.artifact_refs:
                    final_artifact_ref = result.artifact_refs[0]
                    break

        completion_status = self._completion_status(request, stage_results)
        lineage = ArtifactLineage(
            build_id=build_id,
            project_id=request.project_id,
            book_id=request.book_id,
            effective_voice_plan_identity=stage_output_hashes.get(BuildStage.PLAN.value),
            synthesis_manifest_identity=stage_output_hashes.get(BuildStage.MANIFEST.value),
            render_report_identity=stage_output_hashes.get(BuildStage.RENDER.value),
            chapter_assembly_ids=tuple(item for item in chapter_ids if item),
            mastered_chapter_ids=tuple(item for item in mastered_ids if item),
            audiobook_package_id=stage_output_hashes.get(BuildStage.PACKAGE.value),
            stage_input_hashes=stage_input_hashes,
            stage_output_hashes=stage_output_hashes,
            artifact_relative_paths=stage_artifact_paths,
        )
        report = BuildReport(
            build_id=build_id,
            project_id=request.project_id,
            book_id=request.book_id,
            pipeline_contract_version=request.pipeline_contract_version,
            orchestrator_version=request.orchestrator_version,
            target_stage=request.target_stage,
            rebuild_policy=request.rebuild_policy,
            dry_run=request.dry_run,
            completion_status=completion_status,
            stages=tuple(stage_results),
            final_artifact_ref=final_artifact_ref,
            artifact_lineage=lineage,
            cache_reuse_summary=self._cache_summary(stage_results),
            stages_executed=tuple(result.stage.value for result in stage_results if result.started and not result.cache_reused and result.completed),
            stages_reused=tuple(result.stage.value for result in stage_results if result.cache_reused),
            stages_blocked=tuple(result.stage.value for result in stage_results if result.status == StageStatus.BLOCKED),
            stages_failed=tuple(result.stage.value for result in stage_results if result.status == StageStatus.FAILED),
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(dict.fromkeys(errors)),
            report_path=report_path,
            report_identity=hashlib.sha256(canonical_json({"build_id": build_id, "completion_status": completion_status.value, "stages": [result.status.value for result in stage_results]}).encode("utf-8")).hexdigest(),
            lineages={"artifacts": canonicalize(lineage)},
        )
        self._write_report_atomic(actual_report_path, report)
        return report

    def resume_build(self, request: BuildRequest) -> BuildReport:
        return self.build_storyforge_project(request)

    def _execute_stage(self, request: BuildRequest, plan_stage: BuildPlanStage, adapter: StageAdapter, context: StageContext) -> StageResult:
        stage = plan_stage.stage
        if request.dry_run:
            inspection = plan_stage.inspection
            return StageResult(
                stage=stage,
                status=StageStatus.DRY_RUN,
                action=StageAction.DRY_RUN_ONLY,
                started=False,
                completed=False,
                cache_reused=False,
                new_artifacts=False,
                artifact_refs=inspection.artifact_refs if inspection else (),
                stage_report_ref=inspection.report_ref if inspection else None,
                input_identity=plan_stage.expected_input_identity,
                output_identity=inspection.output_identity if inspection else "",
                warnings=inspection.warnings if inspection else (),
                failures=inspection.failures if inspection else (),
                blocking=False,
                duration_seconds=0.0,
                invocation_count=0,
                requested=True,
                dependency_status=plan_stage.dependency_status,
                reason="dry run",
            )
        if plan_stage.intended_action == StageAction.REUSE and plan_stage.inspection and plan_stage.inspection.cache_reusable:
            inspection = plan_stage.inspection
            return StageResult(
                stage=stage,
                status=StageStatus.REUSED if not inspection.warnings else StageStatus.COMPLETED_WITH_WARNINGS,
                action=StageAction.REUSE,
                started=False,
                completed=True,
                cache_reused=True,
                new_artifacts=False,
                artifact_refs=inspection.artifact_refs,
                stage_report_ref=inspection.report_ref,
                input_identity=inspection.input_identity,
                output_identity=inspection.output_identity,
                warnings=inspection.warnings,
                failures=inspection.failures,
                blocking=inspection.blocking,
                duration_seconds=0.0,
                invocation_count=0,
                requested=True,
                dependency_status=plan_stage.dependency_status,
                reason=inspection.reason,
            )
        try:
            result = adapter.execute(
                request,
                context,
                expected_input_identity=plan_stage.expected_input_identity,
                force_rebuild=plan_stage.force_rebuild,
                dry_run=False,
            )
        except Exception as exc:  # noqa: BLE001
            failure = failure_from_exception(stage, exc, retryable=False)
            return StageResult(
                stage=stage,
                status=StageStatus.FAILED,
                action=plan_stage.intended_action,
                started=True,
                completed=False,
                cache_reused=False,
                new_artifacts=False,
                artifact_refs=(),
                stage_report_ref=None,
                input_identity=plan_stage.expected_input_identity,
                output_identity="",
                warnings=(),
                failures=(failure,),
                blocking=True,
                duration_seconds=0.0,
                invocation_count=0,
                requested=True,
                dependency_status=plan_stage.dependency_status,
                reason=str(exc),
            )
        if result.failures and result.blocking:
            return result
        if result.status == StageStatus.COMPLETED and result.warnings:
            return replace(result, status=StageStatus.COMPLETED_WITH_WARNINGS)
        return result

    def _not_requested_result(self, stage: BuildStage, plan_stage: BuildPlanStage) -> StageResult:
        return StageResult(
            stage=stage,
            status=StageStatus.NOT_REQUESTED,
            action=StageAction.SKIP_NOT_REQUESTED,
            started=False,
            completed=False,
            cache_reused=False,
            new_artifacts=False,
            artifact_refs=(),
            stage_report_ref=None,
            input_identity=plan_stage.expected_input_identity,
            output_identity="",
            warnings=(),
            failures=(),
            blocking=False,
            duration_seconds=0.0,
            invocation_count=0,
            requested=False,
            dependency_status=plan_stage.dependency_status,
            reason=plan_stage.reason,
        )

    def _dependency_blocked_result(self, stage: BuildStage, plan_stage: BuildPlanStage) -> StageResult:
        return StageResult(
            stage=stage,
            status=StageStatus.SKIPPED_DUE_TO_DEPENDENCY,
            action=StageAction.BLOCK_DEPENDENCY,
            started=False,
            completed=False,
            cache_reused=False,
            new_artifacts=False,
            artifact_refs=(),
            stage_report_ref=None,
            input_identity=plan_stage.expected_input_identity,
            output_identity="",
            warnings=(),
            failures=(BuildFailure(BuildFailureType.DEPENDENCY_BLOCKED, plan_stage.reason, stage=stage, blocking=True),),
            blocking=True,
            duration_seconds=0.0,
            invocation_count=0,
            requested=True,
            dependency_status=plan_stage.dependency_status,
            reason=plan_stage.reason,
        )

    def _report_from_plan(self, request: BuildRequest, plan: BuildPlan, build_id: str, stage_roots: Mapping[BuildStage, Path], report_path: Path, *, dry_run: bool) -> BuildReport:
        stage_results: list[StageResult] = []
        for plan_stage in plan.stages:
            stage_results.append(
                StageResult(
                    stage=plan_stage.stage,
                    status=StageStatus.DRY_RUN if dry_run else StageStatus.NOT_REQUESTED,
                    action=StageAction.DRY_RUN_ONLY if dry_run and plan_stage.requested else StageAction.SKIP_NOT_REQUESTED,
                    started=False,
                    completed=False,
                    cache_reused=False,
                    new_artifacts=False,
                    artifact_refs=plan_stage.inspection.artifact_refs if plan_stage.inspection else (),
                    stage_report_ref=plan_stage.inspection.report_ref if plan_stage.inspection else None,
                    input_identity=plan_stage.expected_input_identity,
                    output_identity=plan_stage.inspection.output_identity if plan_stage.inspection else "",
                    warnings=plan_stage.inspection.warnings if plan_stage.inspection else (),
                    failures=plan_stage.inspection.failures if plan_stage.inspection else (),
                    blocking=False,
                    duration_seconds=0.0,
                    invocation_count=0,
                    requested=plan_stage.requested,
                    dependency_status=plan_stage.dependency_status,
                    reason=plan_stage.reason,
                )
            )
        lineage = ArtifactLineage(build_id=build_id, project_id=request.project_id, book_id=request.book_id)
        return BuildReport(
            build_id=build_id,
            project_id=request.project_id,
            book_id=request.book_id,
            pipeline_contract_version=request.pipeline_contract_version,
            orchestrator_version=request.orchestrator_version,
            target_stage=request.target_stage,
            rebuild_policy=request.rebuild_policy,
            dry_run=True,
            completion_status=BuildCompletionStatus.DRY_RUN,
            stages=tuple(stage_results),
            final_artifact_ref=None,
            artifact_lineage=lineage,
            cache_reuse_summary={},
            stages_executed=(),
            stages_reused=(),
            stages_blocked=(),
            stages_failed=(),
            warnings=plan.warnings,
            errors=tuple(failure.message for failure in plan.failures),
            report_path=report_path,
            report_identity=hashlib.sha256(canonical_json({"build_id": build_id, "dry_run": True}).encode("utf-8")).hexdigest(),
            lineages={"plan": canonicalize(plan)},
        )

    def _completion_status(self, request: BuildRequest, stage_results: list[StageResult]) -> BuildCompletionStatus:
        if request.dry_run:
            return BuildCompletionStatus.DRY_RUN
        target_stage = BuildStage(request.target_stage.value)
        target_result = next((result for result in stage_results if result.stage == target_stage), None)
        if target_result is None:
            return BuildCompletionStatus.FAILED
        if any(result.status in {StageStatus.BLOCKED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY} for result in stage_results if result.requested):
            return BuildCompletionStatus.BLOCKED
        if any(result.status == StageStatus.FAILED for result in stage_results if result.requested):
            return BuildCompletionStatus.FAILED
        if target_result.status in {StageStatus.REUSED, StageStatus.COMPLETED} and not any(result.warnings for result in stage_results if result.requested):
            return BuildCompletionStatus.COMPLETE
        if target_result.status in {StageStatus.REUSED, StageStatus.COMPLETED, StageStatus.COMPLETED_WITH_WARNINGS}:
            return BuildCompletionStatus.COMPLETE_WITH_WARNINGS if any(result.warnings for result in stage_results if result.requested) else BuildCompletionStatus.COMPLETE
        return BuildCompletionStatus.PARTIAL

    def _cache_summary(self, stage_results: list[StageResult]) -> dict[str, int]:
        return {
            "executed": sum(1 for result in stage_results if result.started and result.completed and not result.cache_reused),
            "reused": sum(1 for result in stage_results if result.cache_reused),
            "blocked": sum(1 for result in stage_results if result.status in {StageStatus.BLOCKED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY}),
            "failed": sum(1 for result in stage_results if result.status == StageStatus.FAILED),
        }

    def _write_report_atomic(self, report_path: Path, report: BuildReport) -> None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = report_path.parent / f".{report_path.name}.tmp"
        try:
            temp_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
            os.replace(temp_path, report_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()


def build_storyforge_project(request: BuildRequest, adapters: Mapping[BuildStage, StageAdapter]) -> BuildReport:
    return PipelineOrchestrator(adapters).build_storyforge_project(request)


def resume_build(request: BuildRequest, adapters: Mapping[BuildStage, StageAdapter]) -> BuildReport:
    return PipelineOrchestrator(adapters).resume_build(request)
