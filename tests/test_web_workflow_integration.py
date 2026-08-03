from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import json

import pytest
from app.voice_planner.synthesis_manifest import ManifestValidationReport
from storyforge.web.application import WebApplicationError, WebApplicationService
from storyforge.web.config import WebSettings
from storyforge.web.models import ProjectRecord
from storyforge.web.projects import ProjectManager


@pytest.fixture()
def web_settings(tmp_path: Path) -> WebSettings:
    data = tmp_path / "data"
    paths = [data / name for name in ("books", "projects", "cache", "output", "logs", "config")]
    for path in paths:
        path.mkdir(parents=True)
    return WebSettings(
        host="127.0.0.1",
        port=8787,
        config_dir=data / "config",
        data_dir=data,
        books_dir=data / "books",
        projects_dir=data / "projects",
        cache_dir=data / "cache",
        output_dir=data / "output",
        log_dir=data / "logs",
        kokoro_url="http://unused",
        max_upload_bytes=100,
    )


def test_old_project_metadata_migrates_with_character_aware_defaults():
    project = ProjectRecord.from_dict({"project_slug": "old", "project_name": "Old"})
    assert project.build_mode == "character-aware"
    assert project.analysis_path == ""
    assert project.voice_plan_path == ""
    assert project.manifest_path == ""
    assert project.pipeline_contract_version == 1


def test_build_blocks_before_orchestrator_when_required_artifact_is_missing(web_settings):
    manager = ProjectManager(web_settings)
    project = manager.create_project(
        project_name="Book", project_slug="book", source_filename="book.epub", source_bytes=b"epub"
    )
    with pytest.raises(WebApplicationError, match="missing required analysis artifact"):
        WebApplicationService(web_settings, manager).build(project)


def test_build_uses_orchestrator_and_reports_missing_production_adapters(web_settings, monkeypatch):
    manager = ProjectManager(web_settings)
    project = manager.create_project(
        project_name="Book", project_slug="book", source_filename="book.epub", source_bytes=b"epub"
    )
    root = manager.project_paths(project.project_slug).root
    normalized = root / "work" / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "normalized_story.json").write_text(
        json.dumps({"book_id": "book"}), encoding="utf-8"
    )
    voice_plan = root / "work" / "voice_plan.json"
    voice_plan.write_text("{}", encoding="utf-8")
    manifest = root / "work" / "synthesis_manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    project.analysis_path = "work/analysis"
    (root / project.analysis_path).mkdir()
    project.normalized_path = "work/normalized"
    project.voice_plan_path = "work/voice_plan.json"
    project.manifest_path = "work/synthesis_manifest.json"
    manager.save_project(project)

    with pytest.raises(
        WebApplicationError, match="application pipeline|production pipeline stage adapters"
    ):
        WebApplicationService(web_settings, manager).build(project)


def test_manifest_uses_am_adam_fallback_for_single_voice_projects(web_settings, monkeypatch):
    manager = ProjectManager(web_settings)
    project = manager.create_project(
        project_name="Book", project_slug="book", source_filename="book.epub", source_bytes=b"epub"
    )
    root = manager.project_paths(project.project_slug).root
    normalized = root / "work" / "normalized"
    normalized.mkdir(parents=True)
    (normalized / "normalized_story.json").write_text(json.dumps({"book_id": "book", "segments": []}), encoding="utf-8")
    (normalized / "normalized_dialogue.json").write_text(json.dumps({"dialogue": []}), encoding="utf-8")
    project.analysis_path = "work/analysis"
    (root / project.analysis_path).mkdir()
    project.normalized_path = "work/normalized"
    project.voice_plan_path = "work/voice_plan.json"
    (root / project.voice_plan_path).write_text("{}", encoding="utf-8")
    project.build_mode = "single-voice"
    project.artifact_map.update({"normalized_analysis": project.normalized_path})
    manager.save_project(project)

    captured = {}

    def fake_voice_registry(self, project, root):
        return {"voices": [{"provider": "kokoro", "provider_voice_id": "am_adam", "voice_id": "kokoro.am_adam"}]}

    def fake_load_editable_voice_plan(payload, registry=None):
        return object()

    def fake_build_synthesis_manifest(story, plan, registry_payload, config, **kwargs):
        captured["config"] = config
        captured["kwargs"] = kwargs
        report = ManifestValidationReport(
            total_source_segments=0,
            total_render_units=0,
            narration_units=0,
            dialogue_units=0,
            skipped_units=0,
            blocked_units=0,
            unresolved_speakers=0,
            unavailable_voices=0,
            unsupported_controls=0,
            duplicate_ids=0,
            warnings=[],
            errors=[],
            ready_state="ready",
        )
        manifest = SimpleNamespace(render_units=[], validation_report=report)
        return SimpleNamespace(manifest=manifest)

    monkeypatch.setattr("storyforge.web.application.WebApplicationService._voice_registry", fake_voice_registry)
    monkeypatch.setattr("storyforge.web.application.load_editable_voice_plan", fake_load_editable_voice_plan)
    monkeypatch.setattr("app.voice_planner.build_synthesis_manifest", fake_build_synthesis_manifest)
    monkeypatch.setattr("app.voice_planner.save_synthesis_manifest_atomic", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.voice_planner.serialize_synthesis_manifest", lambda manifest: "{}")

    service = WebApplicationService(web_settings, manager)
    result = service.manifest(project)

    assert result["ready_state"] == "ready"
    assert captured["config"]["voice_planner"]["default_unresolved_speaker_policy"] == "fallback"
    assert captured["kwargs"]["unresolved_speaker_policy"] == "fallback"
    assert captured["kwargs"]["unresolved_fallback_voice"]["provider_voice_id"] == "am_adam"
