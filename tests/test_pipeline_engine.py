from __future__ import annotations

from pathlib import Path

import pytest

from app.pipeline import (
    BuildCompletionStatus,
    BuildFailure,
    BuildFailureType,
    BuildRequest,
    BuildRequestValidationError,
    BuildStage,
    BuildTarget,
    FakeStageAdapter,
    PipelineOrchestrator,
    RebuildPolicy,
    StageStatus,
    build_request_identity,
)


def _request(tmp_path: Path, *, target: BuildTarget = BuildTarget.PACKAGE, dry_run: bool = False, rebuild_policy: RebuildPolicy = RebuildPolicy.NORMAL, packaging_config: dict | None = None) -> BuildRequest:
    return BuildRequest(
        project_id="project-1",
        book_id="book-1",
        story_input={"story_revision": "rev-a"},
        voice_planning_config={"voice_mode": "narration"},
        editable_plan={"notes": "notes-a", "pronunciation": "pron-a", "voice_assignments": "voice-a"},
        manifest_config={"manifest_rev": "manifest-a"},
        renderer_config={"renderer_rev": "renderer-a"},
        assembler_config={"spacing": "spacing-a"},
        mastering_config={"master_rev": "master-a"},
        packaging_config=packaging_config or {"metadata": "meta-a", "cover_art": "cover-a", "bitrate": 128},
        canonical_chapter_structure=(),
        cover_art=None,
        target_stage=target,
        rebuild_policy=rebuild_policy,
        dry_run=dry_run,
        failure_policy="stop-on-blocking-failure",
        workspace_root=tmp_path,
        pipeline_contract_version=1,
        orchestrator_version="milestone-15",
    )


def _adapters() -> dict[BuildStage, FakeStageAdapter]:
    return {
        BuildStage.PLAN: FakeStageAdapter(BuildStage.PLAN, identity_fields=("story_input.story_revision", "voice_planning_config.voice_mode"), output_seed="plan"),
        BuildStage.APPLY_EDITS: FakeStageAdapter(BuildStage.APPLY_EDITS, dependencies=(BuildStage.PLAN,), identity_fields=("editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.PLAN,), output_seed="edits"),
        BuildStage.MANIFEST: FakeStageAdapter(BuildStage.MANIFEST, dependencies=(BuildStage.APPLY_EDITS,), identity_fields=("manifest_config.manifest_rev",), upstream_identity_sources=(BuildStage.APPLY_EDITS,), output_seed="manifest"),
        BuildStage.RENDER: FakeStageAdapter(BuildStage.RENDER, dependencies=(BuildStage.MANIFEST,), identity_fields=("renderer_config.renderer_rev",), upstream_identity_sources=(BuildStage.MANIFEST,), output_seed="render"),
        BuildStage.ASSEMBLE: FakeStageAdapter(BuildStage.ASSEMBLE, dependencies=(BuildStage.RENDER,), identity_fields=("assembler_config.spacing",), upstream_identity_sources=(BuildStage.RENDER,), output_seed="assemble"),
        BuildStage.MASTER: FakeStageAdapter(BuildStage.MASTER, dependencies=(BuildStage.ASSEMBLE,), identity_fields=("mastering_config.master_rev",), upstream_identity_sources=(BuildStage.ASSEMBLE,), output_seed="master"),
        BuildStage.PACKAGE: FakeStageAdapter(BuildStage.PACKAGE, dependencies=(BuildStage.MASTER,), identity_fields=("packaging_config.metadata", "packaging_config.cover_art", "packaging_config.bitrate", "mastering_config.master_rev", "assembler_config.spacing", "editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.MASTER,), output_seed="package"),
    }


def test_full_clean_build_produces_package_and_report(tmp_path: Path) -> None:
    request = _request(tmp_path)
    adapters = _adapters()
    orchestrator = PipelineOrchestrator(adapters)
    report = orchestrator.build_storyforge_project(request)

    assert report.build_id == build_request_identity(request)
    assert report.completion_status == BuildCompletionStatus.COMPLETE
    assert [stage.stage for stage in report.stages] == list(BuildStage)
    assert all(stage.started for stage in report.stages if stage.requested)
    assert all(stage.completed for stage in report.stages if stage.requested)
    assert report.final_artifact_ref is not None and report.final_artifact_ref.stage == BuildStage.PACKAGE
    assert report.report_path == Path("build_report.json")
    for adapter in adapters.values():
        assert adapter.invocation_count == 1

    actual_report_path = tmp_path / request.project_id / request.book_id / report.build_id / "build_report.json"
    assert actual_report_path.exists()


def test_fully_cached_rerun_reuses_every_stage(tmp_path: Path) -> None:
    request = _request(tmp_path)
    adapters = _adapters()
    orchestrator = PipelineOrchestrator(adapters)
    first = orchestrator.build_storyforge_project(request)
    second = orchestrator.build_storyforge_project(request)

    assert first.completion_status == BuildCompletionStatus.COMPLETE
    assert second.completion_status == BuildCompletionStatus.COMPLETE
    assert all(stage.cache_reused for stage in second.stages if stage.requested)
    assert all(adapter.invocation_count == 1 for adapter in adapters.values())


def test_intermediate_target_stops_before_downstream_stages(tmp_path: Path) -> None:
    request = _request(tmp_path, target=BuildTarget.ASSEMBLE)
    orchestrator = PipelineOrchestrator(_adapters())
    report = orchestrator.build_storyforge_project(request)

    assert report.completion_status == BuildCompletionStatus.COMPLETE
    assert [stage.stage for stage in report.stages] == [
        BuildStage.PLAN,
        BuildStage.APPLY_EDITS,
        BuildStage.MANIFEST,
        BuildStage.RENDER,
        BuildStage.ASSEMBLE,
    ]
    assert all(stage.status != StageStatus.NOT_REQUESTED for stage in report.stages)
    assert report.final_artifact_ref is not None
    assert report.final_artifact_ref.stage == BuildStage.ASSEMBLE


def test_dry_run_skips_mutation_and_execution(tmp_path: Path) -> None:
    request = _request(tmp_path, target=BuildTarget.MANIFEST, dry_run=True)
    adapters = _adapters()
    orchestrator = PipelineOrchestrator(adapters)
    report = orchestrator.build_storyforge_project(request)

    assert report.completion_status == BuildCompletionStatus.DRY_RUN
    assert all(stage.status == StageStatus.DRY_RUN for stage in report.stages)
    assert all(adapter.invocation_count == 0 for adapter in adapters.values())
    assert not (tmp_path / request.project_id / request.book_id / report.build_id / "build_report.json").exists()


def test_manifest_blocked_skips_render_and_downstream(tmp_path: Path) -> None:
    adapters = _adapters()
    adapters[BuildStage.MANIFEST] = FakeStageAdapter(
        BuildStage.MANIFEST,
        dependencies=(BuildStage.APPLY_EDITS,),
        identity_fields=("manifest_config.manifest_rev",),
        upstream_identity_sources=(BuildStage.APPLY_EDITS,),
        output_seed="manifest",
        execution_failure=BuildFailure(BuildFailureType.STAGE_INVOCATION_FAILURE, "manifest blocked", stage=BuildStage.MANIFEST),
    )
    report = PipelineOrchestrator(adapters).build_storyforge_project(_request(tmp_path))

    assert report.stages[2].status == StageStatus.FAILED
    assert all(stage.status == StageStatus.SKIPPED_DUE_TO_DEPENDENCY for stage in report.stages[3:])
    assert report.completion_status == BuildCompletionStatus.BLOCKED


def test_unsafe_workspace_is_rejected_before_execution(tmp_path: Path) -> None:
    request = _request(tmp_path / "../unsafe")
    with pytest.raises(BuildRequestValidationError):
        PipelineOrchestrator(_adapters()).build_storyforge_project(request)
