from __future__ import annotations

from pathlib import Path

from app.pipeline import BuildRequest, BuildStage, BuildTarget, FakeStageAdapter, PipelineOrchestrator, RebuildPolicy


def _base_request(tmp_path: Path, *, editable_plan: dict, assembler_config: dict | None = None, mastering_config: dict | None = None, packaging_config: dict | None = None) -> BuildRequest:
    return BuildRequest(
        project_id="project-1",
        book_id="book-1",
        story_input={"story_revision": "rev-a"},
        voice_planning_config={"voice_mode": "narration"},
        editable_plan=editable_plan,
        manifest_config={"manifest_rev": "manifest-a"},
        renderer_config={"renderer_rev": "renderer-a"},
        assembler_config=assembler_config or {"spacing": "spacing-a"},
        mastering_config=mastering_config or {"master_rev": "master-a"},
        packaging_config=packaging_config or {"metadata": "meta-a", "cover_art": "cover-a", "bitrate": 128},
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


def _adapters() -> dict[BuildStage, FakeStageAdapter]:
    return {
        BuildStage.PLAN: FakeStageAdapter(BuildStage.PLAN, identity_fields=("story_input.story_revision", "voice_planning_config.voice_mode"), output_seed="plan"),
        BuildStage.APPLY_EDITS: FakeStageAdapter(BuildStage.APPLY_EDITS, dependencies=(BuildStage.PLAN,), identity_fields=("editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.PLAN,), output_seed="edits"),
        BuildStage.MANIFEST: FakeStageAdapter(BuildStage.MANIFEST, dependencies=(BuildStage.APPLY_EDITS,), identity_fields=("manifest_config.manifest_rev", "editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.APPLY_EDITS,), output_seed="manifest"),
        BuildStage.RENDER: FakeStageAdapter(BuildStage.RENDER, dependencies=(BuildStage.MANIFEST,), identity_fields=("renderer_config.renderer_rev", "editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.MANIFEST,), output_seed="render"),
        BuildStage.ASSEMBLE: FakeStageAdapter(BuildStage.ASSEMBLE, dependencies=(BuildStage.RENDER,), identity_fields=("assembler_config.spacing", "editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.RENDER,), output_seed="assemble"),
        BuildStage.MASTER: FakeStageAdapter(BuildStage.MASTER, dependencies=(BuildStage.ASSEMBLE,), identity_fields=("mastering_config.master_rev", "assembler_config.spacing", "editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.ASSEMBLE,), output_seed="master"),
        BuildStage.PACKAGE: FakeStageAdapter(BuildStage.PACKAGE, dependencies=(BuildStage.MASTER,), identity_fields=("packaging_config.metadata", "packaging_config.cover_art", "packaging_config.bitrate", "mastering_config.master_rev", "assembler_config.spacing", "editable_plan.pronunciation", "editable_plan.voice_assignments"), upstream_identity_sources=(BuildStage.MASTER,), output_seed="package"),

    }


def test_notes_only_edit_does_not_trigger_audio_rebuild(tmp_path: Path) -> None:
    adapters = _adapters()
    orchestrator = PipelineOrchestrator(adapters)
    orchestrator.build_storyforge_project(_base_request(tmp_path, editable_plan={"notes": "n1", "pronunciation": "p1", "voice_assignments": "va1"}))
    orchestrator.build_storyforge_project(_base_request(tmp_path, editable_plan={"notes": "n2", "pronunciation": "p1", "voice_assignments": "va1"}))

    assert all(adapter.invocation_count == 1 for adapter in adapters.values())


def test_pronunciation_edit_rebuilds_downstream_pipeline(tmp_path: Path) -> None:
    adapters = _adapters()
    orchestrator = PipelineOrchestrator(adapters)
    orchestrator.build_storyforge_project(_base_request(tmp_path, editable_plan={"notes": "n1", "pronunciation": "p1", "voice_assignments": "va1"}))
    orchestrator.build_storyforge_project(_base_request(tmp_path, editable_plan={"notes": "n1", "pronunciation": "p2", "voice_assignments": "va1"}))

    assert adapters[BuildStage.PLAN].invocation_count == 1
    assert adapters[BuildStage.APPLY_EDITS].invocation_count == 2
    assert adapters[BuildStage.MANIFEST].invocation_count == 2
    assert adapters[BuildStage.RENDER].invocation_count == 2
    assert adapters[BuildStage.ASSEMBLE].invocation_count == 2
    assert adapters[BuildStage.MASTER].invocation_count == 2
    assert adapters[BuildStage.PACKAGE].invocation_count == 2


def test_spacing_and_packaging_metadata_changes_rebuild_only_affected_stages(tmp_path: Path) -> None:
    adapters = _adapters()
    orchestrator = PipelineOrchestrator(adapters)
    orchestrator.build_storyforge_project(_base_request(tmp_path, editable_plan={"notes": "n1", "pronunciation": "p1", "voice_assignments": "va1"}))
    orchestrator.build_storyforge_project(_base_request(tmp_path, editable_plan={"notes": "n1", "pronunciation": "p1", "voice_assignments": "va1"}, assembler_config={"spacing": "spacing-b"}))
    assert adapters[BuildStage.RENDER].invocation_count == 1
    assert adapters[BuildStage.ASSEMBLE].invocation_count == 2
    assert adapters[BuildStage.MASTER].invocation_count == 2
    assert adapters[BuildStage.PACKAGE].invocation_count == 2

    orchestrator.build_storyforge_project(_base_request(tmp_path, editable_plan={"notes": "n1", "pronunciation": "p1", "voice_assignments": "va1"}, packaging_config={"metadata": "meta-b", "cover_art": "cover-a", "bitrate": 128}))
    assert adapters[BuildStage.MASTER].invocation_count == 2
    assert adapters[BuildStage.PACKAGE].invocation_count == 3
