from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from .adapters import StageAdapter, StageContext
from .graph import dependency_chain, is_stage_requested, requested_stages, stage_index, topo_requested_stages
from .models import BuildFailure, BuildFailureType, BuildPlan, BuildPlanStage, BuildRequest, BuildStage, BuildTarget, RebuildPolicy, StageAction, StageInspection, StageStatus
from .validation import build_request_identity, validate_build_request


class BuildPlannerError(RuntimeError):
    pass


def plan_build(request: BuildRequest, adapters: Mapping[BuildStage, StageAdapter]) -> BuildPlan:
    request = validate_build_request(request)
    build_id = build_request_identity(request)
    requested_stage = BuildStage(request.target_stage.value)
    requested = topo_requested_stages(requested_stage)
    context = _stage_context(request, build_id, adapters)
    stages: list[BuildPlanStage] = []
    upstream_results: dict[BuildStage, StageInspection] = {}
    warnings: list[str] = []
    failures: list[BuildFailure] = []

    for stage in requested:
        adapter = adapters.get(stage)
        if adapter is None:
            failure = BuildFailure(BuildFailureType.MISSING_STAGE_ADAPTER, f"missing stage adapter: {stage.value}", stage=stage)
            failures.append(failure)
            stages.append(
                BuildPlanStage(
                    stage=stage,
                    requested=True,
                    dependencies=dependency_chain(stage),
                    dependency_status=StageStatus.BLOCKED,
                    expected_input_identity="",
                    known_prior_artifact_state="missing-adapter",
                    intended_action=StageAction.BLOCK_DEPENDENCY,
                    reason=failure.message,
                    cache_reusable=False,
                    force_rebuild=False,
                    dry_run=request.dry_run,
                    inspection=None,
                )
            )
            continue

        stage_context = _stage_context(request, build_id, adapters, upstream_results)
        expected_input_identity = adapter.input_identity(request, stage_context)
        force_rebuild = _force_rebuild_for_stage(request.rebuild_policy, requested_stage, stage)
        inspection = adapter.inspect(request, stage_context, expected_input_identity=expected_input_identity, force_rebuild=force_rebuild)
        dependency_status = _dependency_status(stage, stages)
        is_requested = is_stage_requested(stage, requested_stage)
        intended_action = _plan_action(request, stage, force_rebuild, inspection, dependency_status)
        if dependency_status in {StageStatus.BLOCKED, StageStatus.FAILED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY}:
            intended_action = StageAction.BLOCK_DEPENDENCY
        if not is_requested:
            intended_action = StageAction.SKIP_NOT_REQUESTED
        if request.dry_run and is_requested and dependency_status not in {StageStatus.BLOCKED, StageStatus.FAILED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY}:
            intended_action = StageAction.DRY_RUN_ONLY
        known_prior_artifact_state = inspection.prior_artifact_state if inspection else "unknown"
        reason = _plan_reason(is_requested, dependency_status, force_rebuild, inspection, request.dry_run)
        stages.append(
            BuildPlanStage(
                stage=stage,
                requested=is_requested,
                dependencies=dependency_chain(stage),
                dependency_status=dependency_status,
                expected_input_identity=expected_input_identity,
                known_prior_artifact_state=known_prior_artifact_state,
                intended_action=intended_action,
                reason=reason,
                cache_reusable=inspection.cache_reusable,
                force_rebuild=force_rebuild,
                dry_run=request.dry_run,
                inspection=inspection,
            )
        )
        upstream_results[stage] = inspection
        warnings.extend(inspection.warnings)
        failures.extend(inspection.failures)

    return BuildPlan(
        build_id=build_id,
        project_id=request.project_id,
        book_id=request.book_id,
        target_stage=request.target_stage,
        rebuild_policy=request.rebuild_policy,
        dry_run=request.dry_run,
        stages=tuple(stages),
        warnings=tuple(dict.fromkeys(warnings)),
        failures=tuple(failures),
    )


def _stage_context(
    request: BuildRequest,
    build_id: str,
    adapters: Mapping[BuildStage, StageAdapter],
    upstream_results: Mapping[BuildStage, StageInspection] | None = None,
) -> StageContext:
    from .validation import safe_workspace_paths

    paths = safe_workspace_paths(request, build_id)
    stage_roots = {stage: paths[f"stage:{stage.value}"] for stage in BuildStage}
    return StageContext(
        build_id=build_id,
        workspace_root=paths["workspace_root"],
        project_root=paths["project_root"],
        stage_root=stage_roots[BuildStage.PLAN],
        report_root=paths["project_root"],
        upstream_stage_results={},
        target_stage=BuildStage(request.target_stage.value),
        dry_run=request.dry_run,
        rebuild_policy=request.rebuild_policy.value,
    )


def _dependency_status(stage: BuildStage, stages: list[BuildPlanStage]) -> StageStatus:
    for dependency in dependency_chain(stage):
        dep_plan = next((item for item in stages if item.stage == dependency), None)
        if dep_plan is None:
            return StageStatus.BLOCKED
        if dep_plan.dependency_status in {StageStatus.BLOCKED, StageStatus.FAILED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY}:
            return StageStatus.SKIPPED_DUE_TO_DEPENDENCY
        if dep_plan.intended_action == StageAction.BLOCK_DEPENDENCY:
            return StageStatus.SKIPPED_DUE_TO_DEPENDENCY
        if dep_plan.inspection and dep_plan.inspection.blocking:
            return StageStatus.BLOCKED
    return StageStatus.PENDING


def _plan_action(
    request: BuildRequest,
    stage: BuildStage,
    force_rebuild: bool,
    inspection: StageInspection,
    dependency_status: StageStatus,
) -> StageAction:
    if dependency_status in {StageStatus.BLOCKED, StageStatus.FAILED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY}:
        return StageAction.BLOCK_DEPENDENCY
    if not is_stage_requested(stage, BuildStage(request.target_stage.value)):
        return StageAction.SKIP_NOT_REQUESTED
    if request.dry_run:
        return StageAction.DRY_RUN_ONLY
    if force_rebuild:
        return StageAction.FORCE_REBUILD
    if inspection.cache_reusable:
        return StageAction.REUSE
    return StageAction.CACHE_CHECK


def _plan_reason(requested: bool, dependency_status: StageStatus, force_rebuild: bool, inspection: StageInspection, dry_run: bool) -> str:
    if not requested:
        return "stage not requested"
    if dependency_status in {StageStatus.BLOCKED, StageStatus.FAILED, StageStatus.SKIPPED_DUE_TO_DEPENDENCY}:
        return f"dependency status: {dependency_status.value}"
    if dry_run:
        return "dry run"
    if force_rebuild:
        return "rebuild policy requires execution"
    if inspection.cache_reusable:
        return "validated cache reusable"
    return inspection.reason


def _force_rebuild_for_stage(policy: RebuildPolicy, target: BuildStage, stage: BuildStage) -> bool:
    if policy == RebuildPolicy.NORMAL:
        return False
    if policy == RebuildPolicy.REBUILD_ALL:
        return is_stage_requested(stage, target)
    if policy == RebuildPolicy.REBUILD_CURRENT_STAGE:
        return stage == target
    if policy == RebuildPolicy.REBUILD_FROM_STAGE:
        return stage_index(stage) >= stage_index(target)
    return False
