from pathlib import Path

from app.pipeline import BuildRequest, BuildStage, BuildTarget, FakeStageAdapter, RebuildPolicy, StageAction, plan_build


def _request(tmp_path: Path, *, target: BuildTarget = BuildTarget.PACKAGE, rebuild_policy: RebuildPolicy = RebuildPolicy.NORMAL, dry_run: bool = False) -> BuildRequest:
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
        BuildStage.PACKAGE: FakeStageAdapter(BuildStage.PACKAGE, dependencies=(BuildStage.MASTER,), identity_fields=("packaging_config.metadata", "packaging_config.cover_art", "packaging_config.bitrate"), upstream_identity_sources=(BuildStage.MASTER,), output_seed="package"),
    }


def test_plan_build_prefers_cache_check_and_rebuild_current_stage(tmp_path: Path) -> None:
    request = _request(tmp_path, rebuild_policy=RebuildPolicy.REBUILD_CURRENT_STAGE)
    adapters = _adapters()
    plan = plan_build(request, adapters)

    assert [stage.stage for stage in plan.stages] == [
        BuildStage.PLAN,
        BuildStage.APPLY_EDITS,
        BuildStage.MANIFEST,
        BuildStage.RENDER,
        BuildStage.ASSEMBLE,
        BuildStage.MASTER,
        BuildStage.PACKAGE,
    ]
    assert [stage.intended_action for stage in plan.stages[:-1]] == [
        StageAction.CACHE_CHECK,
        StageAction.CACHE_CHECK,
        StageAction.CACHE_CHECK,
        StageAction.CACHE_CHECK,
        StageAction.CACHE_CHECK,
        StageAction.CACHE_CHECK,
    ]
    assert plan.stages[-1].intended_action == StageAction.FORCE_REBUILD


def test_plan_build_dry_run_marks_requested_stages(tmp_path: Path) -> None:
    request = _request(tmp_path, target=BuildTarget.MANIFEST, dry_run=True)
    plan = plan_build(request, _adapters())

    assert [stage.stage for stage in plan.stages] == [BuildStage.PLAN, BuildStage.APPLY_EDITS, BuildStage.MANIFEST]
    assert all(stage.requested for stage in plan.stages)
    assert all(stage.intended_action == StageAction.DRY_RUN_ONLY for stage in plan.stages)
