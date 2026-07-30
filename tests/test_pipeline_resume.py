from pathlib import Path

from app.pipeline import BuildCompletionStatus, BuildFailure, BuildFailureType, BuildStage, BuildTarget, FakeStageAdapter, PipelineOrchestrator, RebuildPolicy, StageStatus, build_request_identity
from app.pipeline.models import BuildRequest


def _request(tmp_path: Path) -> BuildRequest:
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
        packaging_config={"metadata": "meta-a", "cover_art": "cover-a", "bitrate": 128},
        canonical_chapter_structure=(),
        cover_art=None,
        target_stage=BuildTarget.PACKAGE,
        rebuild_policy=RebuildPolicy.NORMAL,
        dry_run=False,
        failure_policy="stop-on-blocking-failure",
        workspace_root=tmp_path,
        pipeline_contract_version=1,
        orchestrator_version="milestone-15",
    )


def _adapters(*, master_failure: bool = False) -> dict[BuildStage, FakeStageAdapter]:
    return {
        BuildStage.PLAN: FakeStageAdapter(BuildStage.PLAN, identity_fields=("story_input.story_revision", "voice_planning_config.voice_mode"), output_seed="plan"),
        BuildStage.APPLY_EDITS: FakeStageAdapter(BuildStage.APPLY_EDITS, dependencies=(BuildStage.PLAN,), identity_fields=("editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.PLAN,), output_seed="edits"),
        BuildStage.MANIFEST: FakeStageAdapter(BuildStage.MANIFEST, dependencies=(BuildStage.APPLY_EDITS,), identity_fields=("manifest_config.manifest_rev",), upstream_identity_sources=(BuildStage.APPLY_EDITS,), output_seed="manifest"),
        BuildStage.RENDER: FakeStageAdapter(BuildStage.RENDER, dependencies=(BuildStage.MANIFEST,), identity_fields=("renderer_config.renderer_rev",), upstream_identity_sources=(BuildStage.MANIFEST,), output_seed="render"),
        BuildStage.ASSEMBLE: FakeStageAdapter(BuildStage.ASSEMBLE, dependencies=(BuildStage.RENDER,), identity_fields=("assembler_config.spacing",), upstream_identity_sources=(BuildStage.RENDER,), output_seed="assemble"),
        BuildStage.MASTER: FakeStageAdapter(
            BuildStage.MASTER,
            dependencies=(BuildStage.ASSEMBLE,),
            identity_fields=("mastering_config.master_rev",),
            upstream_identity_sources=(BuildStage.ASSEMBLE,),
            output_seed="master",
            execution_failure=BuildFailure(BuildFailureType.STAGE_INVOCATION_FAILURE, "mastering interrupted", stage=BuildStage.MASTER) if master_failure else None,
        ),
        BuildStage.PACKAGE: FakeStageAdapter(BuildStage.PACKAGE, dependencies=(BuildStage.MASTER,), identity_fields=("packaging_config.metadata", "packaging_config.cover_art", "packaging_config.bitrate", "mastering_config.master_rev", "assembler_config.spacing", "editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.MASTER,), output_seed="package"),

    }


def test_resume_build_reuses_completed_stages_after_interruption(tmp_path: Path) -> None:
    request = _request(tmp_path)
    adapters = _adapters(master_failure=True)
    orchestrator = PipelineOrchestrator(adapters)

    first = orchestrator.build_storyforge_project(request)
    assert first.completion_status == BuildCompletionStatus.BLOCKED
    assert first.stages[5].status == StageStatus.FAILED
    assert first.stages[6].status == StageStatus.SKIPPED_DUE_TO_DEPENDENCY

    report_path = tmp_path / request.project_id / request.book_id / build_request_identity(request) / "build_report.json"
    assert report_path.exists()
    report_path.unlink()

    adapters[BuildStage.MASTER].execution_failure = None
    second = orchestrator.build_storyforge_project(request)
    assert second.completion_status == BuildCompletionStatus.COMPLETE
    assert [stage.status for stage in second.stages[:5]] == [StageStatus.REUSED] * 5
    assert second.stages[5].status == StageStatus.COMPLETED
    assert second.stages[6].status == StageStatus.COMPLETED


def test_corrupt_master_cache_forces_rebuild_from_master_on_rerun(tmp_path: Path) -> None:
    request = _request(tmp_path)
    adapters = _adapters()
    orchestrator = PipelineOrchestrator(adapters)
    first = orchestrator.build_storyforge_project(request)
    assert first.completion_status == BuildCompletionStatus.COMPLETE

    adapters[BuildStage.MASTER].artifact_exists = False
    adapters[BuildStage.PACKAGE].artifact_exists = False
    second = orchestrator.build_storyforge_project(request)
    assert second.stages[0].status == StageStatus.REUSED
    assert second.stages[1].status == StageStatus.REUSED
    assert second.stages[2].status == StageStatus.REUSED
    assert second.stages[3].status == StageStatus.REUSED
    assert second.stages[4].status == StageStatus.REUSED
    assert second.stages[5].status == StageStatus.COMPLETED
    assert second.stages[6].status == StageStatus.COMPLETED
