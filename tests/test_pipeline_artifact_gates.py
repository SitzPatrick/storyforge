from __future__ import annotations

import json
from pathlib import Path

from app.pipeline import BuildRequest, BuildStage, StageContext
from app.pipeline.production_adapters import ProductionStageAdapter


class PayloadAdapter(ProductionStageAdapter):
    def __init__(self, stage: BuildStage, payload_factory):
        super().__init__(stage)
        self.payload_factory = payload_factory

    def _run_engine(self, request, context):
        return self.payload_factory(context)


def _request(tmp_path: Path) -> BuildRequest:
    return BuildRequest(
        project_id="project-1",
        book_id="book-1",
        story_input={},
        voice_planning_config={},
        editable_plan=None,
        manifest_config={},
        renderer_config={},
        assembler_config={},
        mastering_config={},
        packaging_config={},
        workspace_root=tmp_path,
    )


def _context(tmp_path: Path, stage: BuildStage) -> StageContext:
    return StageContext(
        build_id="build-1",
        workspace_root=tmp_path,
        project_root=tmp_path,
        stage_root=tmp_path / stage.value,
        report_root=tmp_path,
        target_stage=stage,
    )


def test_failed_engine_result_cannot_become_completed_stage(tmp_path: Path):
    adapter = PayloadAdapter(
        BuildStage.RENDER, lambda _: {"completion_status": "failed", "errors": ["bad audio"]}
    )
    result = adapter.execute(
        _request(tmp_path), _context(tmp_path, BuildStage.RENDER), expected_input_identity="i"
    )
    assert result.status.value == "blocked"
    assert not result.completed
    assert "did not complete" in result.reason


def test_missing_render_report_blocks_stage(tmp_path: Path):
    def payload(context):
        audio = context.stage_root / "segment.audio"
        sidecar = context.stage_root / "segment.audio.json"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"audio")
        sidecar.write_text("{}", encoding="utf-8")
        return {
            "completion_status": "complete",
            "unit_results": [
                {"status": "rendered", "output_path": str(audio), "sidecar_path": str(sidecar)}
            ],
        }

    result = PayloadAdapter(BuildStage.RENDER, payload).execute(
        _request(tmp_path), _context(tmp_path, BuildStage.RENDER), expected_input_identity="i"
    )
    assert result.status.value == "blocked"
    assert "render report" in result.reason


def test_package_json_can_never_be_final_artifact(tmp_path: Path):
    def payload(context):
        package = context.stage_root / "package.json"
        sidecar = context.stage_root / "package_sidecar.json"
        report = context.stage_root / "packaging_report.json"
        context.stage_root.mkdir(parents=True, exist_ok=True)
        for path in (package, sidecar, report):
            path.write_text("{}", encoding="utf-8")
        return {
            "completion_status": "complete",
            "output_artifact_path": str(package),
            "sidecar_path": str(sidecar),
            "report_path": str(report),
        }

    result = PayloadAdapter(BuildStage.PACKAGE, payload).execute(
        _request(tmp_path), _context(tmp_path, BuildStage.PACKAGE), expected_input_identity="i"
    )
    assert result.status.value == "blocked"
    assert "not an M4B" in result.reason


def test_package_requires_real_m4b_and_reports(tmp_path: Path):
    def payload(context):
        package = context.stage_root / "packages" / "book.m4b"
        sidecar = package.with_name("package_sidecar.json")
        report = package.with_name("packaging_report.json")
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(b"m4b")
        sidecar.write_text("{}", encoding="utf-8")
        report.write_text("{}", encoding="utf-8")
        return {
            "completion_status": "complete",
            "output_artifact_path": str(package),
            "sidecar_path": str(sidecar),
            "report_path": str(report),
        }

    result = PayloadAdapter(BuildStage.PACKAGE, payload).execute(
        _request(tmp_path), _context(tmp_path, BuildStage.PACKAGE), expected_input_identity="i"
    )
    assert result.completed
    assert any(ref.relative_path.endswith(".m4b") for ref in result.artifact_refs)
    assert all(not ref.relative_path.endswith("package.json") for ref in result.artifact_refs[:1])


def test_unmapped_absolute_output_is_rejected(tmp_path: Path):
    def payload(context):
        return {
            "completion_status": "complete",
            "output_artifact_path": "/tmp/escape.m4b",
            "sidecar_path": str(context.stage_root / "package_sidecar.json"),
            "report_path": str(context.stage_root / "packaging_report.json"),
        }

    result = PayloadAdapter(BuildStage.PACKAGE, payload).execute(
        _request(tmp_path), _context(tmp_path, BuildStage.PACKAGE), expected_input_identity="i"
    )
    assert result.status.value == "blocked"
    assert "escapes project workspace" in result.reason


def test_adapter_report_does_not_overwrite_engine_render_report(tmp_path: Path):
    def payload(context):
        report = context.report_root / "render.report.json"
        report.write_text(
            json.dumps({"completion_status": "complete", "engine": True}), encoding="utf-8"
        )
        audio = context.stage_root / "segment.audio"
        sidecar = context.stage_root / "segment.audio.json"
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes(b"audio")
        sidecar.write_text("{}", encoding="utf-8")
        return {
            "completion_status": "complete",
            "unit_results": [
                {"status": "rendered", "output_path": str(audio), "sidecar_path": str(sidecar)}
            ],
        }

    result = PayloadAdapter(BuildStage.RENDER, payload).execute(
        _request(tmp_path), _context(tmp_path, BuildStage.RENDER), expected_input_identity="i"
    )
    assert result.completed
    assert json.loads((tmp_path / "render.report.json").read_text())["engine"] is True
    assert (tmp_path / "render.adapter.report.json").exists()
