from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_renderer_engine import FakeProviderAdapter, _manifest  # noqa: E402

from app.pipeline import BuildRequest, BuildStage, StageContext, build_production_adapters
from app.pipeline.production_adapters import ProductionStageAdapter


def test_web_production_factory_registers_every_real_stage_without_fake_adapters():
    adapters = build_production_adapters()
    assert set(adapters) == set(BuildStage)
    assert all(type(adapter).__name__ != "FakeStageAdapter" for adapter in adapters.values())
    assert all(adapter.stage == stage for stage, adapter in adapters.items())


def test_render_adapter_runs_segment_renderer_with_synthetic_manifest_and_provider(tmp_path: Path):
    manifest = _manifest(tmp_path)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")
    request = BuildRequest(
        project_id="project-1",
        book_id="book-9",
        story_input=manifest,
        voice_planning_config={},
        editable_plan=None,
        manifest_config={},
        renderer_config={
            "manifest": manifest,
            "provider_adapters": {"alpha": provider, "beta": beta},
            "render_root": tmp_path / "renders",
            "report_path": tmp_path / "render.report.json",
            "sample_rate_hz": 24000,
            "channel_count": 1,
            "sample_width_bytes": 2,
        },
        assembler_config={},
        mastering_config={},
        packaging_config={},
        workspace_root=tmp_path,
    )
    context = StageContext(
        build_id="build-1",
        workspace_root=tmp_path,
        project_root=tmp_path,
        stage_root=tmp_path / "render-stage",
        report_root=tmp_path,
        target_stage=BuildStage.RENDER,
    )
    render_adapter = build_production_adapters()[BuildStage.RENDER]
    assert isinstance(render_adapter, ProductionStageAdapter)
    report = render_adapter._run_engine(request, context)
    assert report["completion_status"] == "complete"
    assert report["successfully_rendered_units"] == 4
    assert provider.render_requests
    assert (tmp_path / "render.report.json").exists()


def test_real_production_adapters_complete_character_aware_pipeline(tmp_path: Path):
    from test_renderer_engine import _plan, _registry, _story

    from app.packaging.backends.fake import FakePackagingBackend
    from app.pipeline import BuildTarget, PipelineOrchestrator
    from app.voice_planner import serialize_synthesis_manifest, serialize_voice_plan

    base_plan = _plan()
    narrator = replace(
        base_plan.narrator,
        assignment=replace(
            base_plan.narrator.assignment,
            voice_id="beta.v3",
            provider_voice_id="v3",
        ),
    )
    # Keep the fixture deterministic while exercising three distinct provider voice IDs.
    ben = replace(
        base_plan.characters[1],
        assignment=replace(
            base_plan.characters[1].assignment,
            voice_id="alpha.v2",
            provider="alpha",
            provider_voice_id="v2",
        ),
    )
    plan = replace(base_plan, narrator=narrator, characters=[base_plan.characters[0], ben])
    from app.voice_planner import build_synthesis_manifest

    story = _story()
    registry = _registry()
    registry["voices"].append(
        {**registry["voices"][-1], "voice_id": "beta.v3", "provider_voice_id": "v3"}
    )
    manifest = build_synthesis_manifest(story, plan, registry, {}).manifest
    alpha = FakeProviderAdapter("alpha", supported_voices={"v1", "v2"})
    beta = FakeProviderAdapter("beta", supported_voices={"v1", "v2", "v3"})
    request = BuildRequest(
        project_id="project-1",
        book_id="book-9",
        story_input=story,
        voice_planning_config={},
        editable_plan=json.loads(serialize_voice_plan(plan)),
        manifest_config={
            "manifest": json.loads(serialize_synthesis_manifest(manifest)),
            "voice_registry": registry,
        },
        renderer_config={
            "manifest": manifest,
            "provider_adapters": {"alpha": alpha, "beta": beta},
        },
        assembler_config={"manifest": manifest},
        mastering_config={},
        packaging_config={
            "metadata": {"title": "Test", "author": "Author", "identifier": "book-9"},
            "backend": FakePackagingBackend(),
        },
        canonical_chapter_structure=(
            {
                "chapter_id": "chapter-1",
                "chapter_order": 1,
                "chapter_title": "Chapter 1",
                "scene_ids": ["scene-1", "scene-2"],
                "render_unit_ids": [unit.render_unit_id for unit in manifest.render_units],
            },
        ),
        target_stage=BuildTarget.PACKAGE,
        workspace_root=tmp_path,
    )
    report = PipelineOrchestrator(build_production_adapters()).build_storyforge_project(request)

    assert report.completion_status.value == "complete"
    assert report.final_artifact_ref is not None
    assert {
        request.provider_voice_id for request in alpha.render_requests + beta.render_requests
    } == {"v1", "v2", "v3"}
    assert [stage.status.value for stage in report.stages] == ["completed"] * 7


def test_production_adapter_persisted_cache_and_voice_change_rebuild(tmp_path: Path):
    manifest = _manifest(tmp_path)
    provider = FakeProviderAdapter("alpha")
    request = BuildRequest(
        project_id="project-1",
        book_id="book-9",
        story_input=manifest,
        voice_planning_config={},
        editable_plan=None,
        manifest_config={},
        renderer_config={"manifest": manifest, "provider_adapters": {"alpha": provider}},
        assembler_config={},
        mastering_config={},
        packaging_config={},
        workspace_root=tmp_path,
    )
    context = StageContext(
        build_id="build-1",
        workspace_root=tmp_path,
        project_root=tmp_path,
        stage_root=tmp_path / "render-stage",
        report_root=tmp_path,
        target_stage=BuildStage.RENDER,
    )
    adapter = build_production_adapters()[BuildStage.RENDER]
    identity = adapter.input_identity(request, context)
    first = adapter.execute(request, context, expected_input_identity=identity)
    assert first.completed and not first.cache_reused

    fresh_adapter = build_production_adapters()[BuildStage.RENDER]
    inspection = fresh_adapter.inspect(request, context, expected_input_identity=identity)
    assert inspection.cache_reusable
    assert inspection.action.value == "reuse"

    changed_request = replace(
        request,
        renderer_config={
            **request.renderer_config,
            "manifest": replace(manifest.render_units[0], assigned_provider_voice_id="v2"),
        },
    )
    changed_identity = fresh_adapter.input_identity(changed_request, context)
    assert changed_identity != identity
    assert not fresh_adapter.inspect(
        changed_request, context, expected_input_identity=changed_identity
    ).cache_reusable
