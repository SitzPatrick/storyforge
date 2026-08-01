from __future__ import annotations

import json
from pathlib import Path

import pytest

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
