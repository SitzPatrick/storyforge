from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from storyforge.web.app import create_app
from storyforge.web.config import WebSettings
from storyforge.web.jobs import JobBusyError
from storyforge.web.models import JobStatus, now_iso
from storyforge.web.projects import ProjectManager
from storyforge.web.security import SecurityError, secure_filename, validate_slug
from storyforge.web.services import WebServices


@pytest.fixture()
def web_settings(tmp_path: Path) -> WebSettings:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    books_dir = data_dir / "books"
    projects_dir = data_dir / "projects"
    cache_dir = data_dir / "cache"
    output_dir = data_dir / "output"
    log_dir = data_dir / "logs"
    for path in (config_dir, books_dir, projects_dir, cache_dir, output_dir, log_dir):
        path.mkdir(parents=True, exist_ok=True)
    return WebSettings(
        host="0.0.0.0",
        port=8787,
        config_dir=config_dir,
        data_dir=data_dir,
        books_dir=books_dir,
        projects_dir=projects_dir,
        cache_dir=cache_dir,
        output_dir=output_dir,
        log_dir=log_dir,
        kokoro_url="http://kokoro:8880",
        max_upload_bytes=32,
    )


class FakeJobManager:
    def __init__(self, projects: ProjectManager) -> None:
        self.projects = projects
        self._active = None

    def active_status(self):
        return self._active

    def start(self, project, action: str):
        if self._active is not None and self._active.active:
            raise JobBusyError("Another build is already running")
        status = self.projects.load_status(project.project_slug)
        job_id = uuid.uuid4().hex
        now = now_iso()
        status.job_id = job_id
        status.action = action
        status.status = "running"
        status.stage = "analyzing" if action == "analyze" else "building"
        status.message = f"{action} started"
        status.started_at = now
        status.updated_at = now
        status.finished_at = ""
        status.return_code = None
        status.pid = 4242
        status.active = True
        status.source_book = project.source_book
        status.build_log_tail = []
        self.projects.append_build_log(project.project_slug, f"{action} started")
        self.projects.save_status(status)
        project.state = "running"
        project.last_build_id = job_id
        project.updated_at = now
        self.projects.save_project(project)
        self._active = status
        return status

    def finish(self, project_slug: str, success: bool = True):
        status = self.projects.load_status(project_slug)
        status.active = False
        status.status = "completed" if success else "failed"
        status.stage = "completed" if success else "failed"
        status.message = "build completed" if success else "build failed"
        status.return_code = 0 if success else 1
        status.finished_at = now_iso()
        status.updated_at = status.finished_at
        self.projects.append_build_log(project_slug, status.message)
        status.build_log_tail = self.projects.read_build_log_tail(project_slug)
        self.projects.save_status(status)
        project = self.projects.load_project(project_slug)
        project.state = status.status
        project.updated_at = now_iso()
        self.projects.save_project(project)
        self._active = None
        return status

    def cancel(self, project_slug: str):
        if self._active is None or self._active.project_slug != project_slug:
            raise JobBusyError("No active build")
        status = self.projects.load_status(project_slug)
        status.active = False
        status.status = "cancelled"
        status.stage = "cancelled"
        status.message = "build cancelled"
        status.return_code = None
        status.finished_at = now_iso()
        status.updated_at = status.finished_at
        self.projects.append_build_log(project_slug, status.message)
        status.build_log_tail = self.projects.read_build_log_tail(project_slug)
        self.projects.save_status(status)
        project = self.projects.load_project(project_slug)
        project.state = status.status
        project.updated_at = now_iso()
        self.projects.save_project(project)
        self._active = None
        return status


@dataclass
class FakeServices:
    settings: WebSettings
    projects: ProjectManager
    jobs: FakeJobManager

    def diagnostics(self):
        return {
            "status": "ok",
            "checks": [{"name": "StoryForge version", "status": "ok", "value": "0.1.0a1"}],
        }


@pytest.fixture()
def services_and_client(web_settings: WebSettings):
    projects = ProjectManager(web_settings)
    jobs = FakeJobManager(projects)
    services = FakeServices(settings=web_settings, projects=projects, jobs=jobs)
    app = create_app(web_settings, services=services)
    return services, TestClient(app)


def create_uploaded_project(
    client: TestClient,
    name: str = "Sample Book",
    slug: str = "sample-book",
    filename: str = "book.epub",
    content: bytes = b"fake epub",
):
    response = client.post(
        "/projects",
        data={"project_name": name, "project_slug": slug, "source_mode": "upload"},
        files={"epub_file": (filename, content, "application/epub+zip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def test_health_dashboard_create_page_and_missing_project(services_and_client):
    services, client = services_and_client
    create_uploaded_project(client)

    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    response = client.get("/")
    assert response.status_code == 200
    assert "Sample Book" in response.text

    response = client.get("/projects/new")
    assert response.status_code == 200
    assert "Create project" in response.text

    response = client.get("/projects/sample-book")
    assert response.status_code == 200
    assert "Sample Book" in response.text

    response = client.get("/projects/missing-project")
    assert response.status_code == 404


def test_project_creation_upload_duplicate_and_validation(services_and_client):
    services, client = services_and_client
    first = create_uploaded_project(client, filename="my-book.epub")
    assert first.status_code == 303
    second = client.post(
        "/projects",
        data={"project_name": "Sample Two", "project_slug": "sample-two", "source_mode": "upload"},
        files={"epub_file": ("my-book.epub", b"fake epub 2", "application/epub+zip")},
        follow_redirects=False,
    )
    assert second.status_code == 303
    project = services.projects.load_project("sample-two")
    assert project.source_book.startswith("input/")
    assert project.source_filename == "my-book.epub"
    input_dir = services.projects.project_paths("sample-two").input_dir
    extra = services.projects._write_unique_file(input_dir, "my-book.epub", b"duplicate")
    assert extra.name != "my-book.epub"
    assert extra.read_bytes() == b"duplicate"

    invalid_ext = client.post(
        "/projects",
        data={"project_name": "Bad File", "project_slug": "bad-file", "source_mode": "upload"},
        files={"epub_file": ("not-epub.txt", b"text", "text/plain")},
        follow_redirects=False,
    )
    assert invalid_ext.status_code == 400

    traversal = client.post(
        "/projects",
        data={"project_name": "Traversal", "project_slug": "traversal", "source_mode": "upload"},
        files={"epub_file": ("../evil.epub", b"data", "application/epub+zip")},
        follow_redirects=False,
    )
    assert traversal.status_code == 400

    oversized = client.post(
        "/projects",
        data={"project_name": "Too Big", "project_slug": "too-big", "source_mode": "upload"},
        files={"epub_file": ("big.epub", b"x" * 128, "application/epub+zip")},
        follow_redirects=False,
    )
    assert oversized.status_code == 400


def test_existing_book_selection_and_slug_validation(
    web_settings: WebSettings, services_and_client
):
    services, client = services_and_client
    existing = web_settings.books_dir / "library-book.epub"
    existing.write_bytes(b"fake epub")
    response = client.post(
        "/projects",
        data={
            "project_name": "Library Book",
            "project_slug": "library-book",
            "source_mode": "existing",
            "existing_book": "library-book.epub",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    project = services.projects.load_project("library-book")
    assert services.projects.project_source_path(project).read_bytes() == b"fake epub"

    with pytest.raises(SecurityError):
        validate_slug("Bad Slug")
    with pytest.raises(SecurityError):
        secure_filename("../evil.epub")


def test_atomic_project_and_status_writes_reload_and_invalid_slug(web_settings: WebSettings):
    manager = ProjectManager(web_settings)
    record = manager.create_project(
        project_name="Atomic",
        project_slug="atomic",
        source_filename="book.epub",
        source_bytes=b"abc",
    )
    project_file = manager.project_paths(record.project_slug).project_file
    status_file = manager.project_paths(record.project_slug).status_file
    assert json.loads(project_file.read_text(encoding="utf-8"))["project_slug"] == "atomic"
    assert json.loads(status_file.read_text(encoding="utf-8"))["status"] == "idle"
    loaded = manager.load_project("atomic")
    assert loaded.project_name == "Atomic"
    assert loaded.source_book == record.source_book
    with pytest.raises(SecurityError):
        manager.create_project(
            project_name="Bad",
            project_slug="bad slug",
            source_filename="book.epub",
            source_bytes=b"x",
        )


def test_artifacts_listing_download_and_path_escape(services_and_client, web_settings: WebSettings):
    services, client = services_and_client
    create_uploaded_project(client)
    artifacts_dir = services.projects.project_paths("sample-book").artifacts_dir
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    (artifacts_dir / "sample.m4b").write_bytes(b"audio")
    (artifacts_dir / "report.json").write_text("{}", encoding="utf-8")

    response = client.get("/projects/sample-book/artifacts")
    assert response.status_code == 200
    assert "sample.m4b" in response.text

    response = client.get("/projects/sample-book/download/artifacts/sample.m4b")
    assert response.status_code == 200
    assert response.content == b"audio"

    outside = web_settings.data_dir / "outside.m4b"
    outside.write_bytes(b"outside")
    escape = client.get("/projects/sample-book/download/artifacts/../outside.m4b")
    assert escape.status_code == 403

    symlink = artifacts_dir / "escape.m4b"
    symlink.symlink_to(outside)
    symlink_response = client.get("/projects/sample-book/download/artifacts/escape.m4b")
    assert symlink_response.status_code == 403


def test_fake_build_lifecycle_completion_failure_cancel_resume_and_single_active_build(
    services_and_client,
):
    services, client = services_and_client
    create_uploaded_project(client)

    first = client.post("/projects/sample-book/build", follow_redirects=False)
    assert first.status_code == 303
    running = client.get("/projects/sample-book/status").json()
    assert running["status"] == "running"
    assert running["stage"] == "building"

    second = client.post("/projects/sample-book/build", follow_redirects=False)
    assert second.status_code == 409

    services.jobs.finish("sample-book", success=True)
    completed = client.get("/projects/sample-book/status").json()
    assert completed["status"] == "completed"
    assert completed["return_code"] == 0
    assert "build completed" in client.get("/projects/sample-book/log").json()["lines"][-1]

    failure_start = client.post("/projects/sample-book/build", follow_redirects=False)
    assert failure_start.status_code == 303
    services.jobs.finish("sample-book", success=False)
    failed = client.get("/projects/sample-book/status").json()
    assert failed["status"] == "failed"
    assert failed["return_code"] == 1

    cancel_start = client.post("/projects/sample-book/build", follow_redirects=False)
    assert cancel_start.status_code == 303
    cancel = client.post("/projects/sample-book/cancel", follow_redirects=False)
    assert cancel.status_code == 303
    cancelled = client.get("/projects/sample-book/status").json()
    assert cancelled["status"] == "cancelled"
    resume = client.post("/projects/sample-book/resume", follow_redirects=False)
    assert resume.status_code == 303
    resumed = client.get("/projects/sample-book/status").json()
    assert resumed["status"] == "running"


def test_polished_workspace_routes_render_and_dashboard_filters(web_settings: WebSettings):
    services = WebServices.create(web_settings)
    services.projects.create_project(
        project_name="A Sample Book",
        project_slug="sample-book",
        source_filename="sample.epub",
        source_bytes=b"epub",
    )
    client = TestClient(create_app(web_settings, services))

    dashboard = client.get("/?q=sample&sort=name")
    assert dashboard.status_code == 200
    assert "Your projects" in dashboard.text
    assert "VOICE PLAN" in dashboard.text
    assert "Search projects" in dashboard.text
    assert "Delete" in dashboard.text

    project = client.get("/projects/sample-book")
    assert project.status_code == 200
    assert "Characters" in project.text
    assert "Downloads" in project.text
    assert "Build status" in project.text

    assert client.get("/settings").status_code == 200
    assert client.get("/projects/sample-book/voice-plan").status_code == 200
    assert client.get("/projects/sample-book/build").status_code == 200
    assert client.get("/projects/sample-book/characters/character-a").status_code == 200


def test_stale_running_job_is_marked_failed_on_startup(web_settings: WebSettings):
    services = WebServices.create(web_settings)
    project = services.projects.create_project(
        project_name="Stale",
        project_slug="stale-project",
        source_filename="book.epub",
        source_bytes=b"abc",
    )
    status = JobStatus(
        project_slug=project.project_slug,
        job_id="stale-job",
        action="build",
        status="running",
        stage="building",
        message="stuck",
        started_at=now_iso(),
        updated_at=now_iso(),
        pid=99999,
        log_path=str(services.projects.project_paths(project.project_slug).build_log),
        source_book=project.source_book,
        active=True,
    )
    services.projects.save_status(status)
    project.state = "running"
    services.projects.save_project(project)

    app = create_app(web_settings, services=services)
    with TestClient(app) as client:
        response = client.get("/projects/stale-project/status")
        assert response.status_code == 200
        recovered = response.json()
        assert recovered["status"] == "failed"
        assert recovered["message"] == "stale build state cleared after restart"
        assert recovered["active"] is False


def test_secure_filename_and_traversal_helpers():
    assert secure_filename("Book Name.epub") == "Book_Name.epub"
    with pytest.raises(SecurityError):
        secure_filename("folder/book.epub")
    with pytest.raises(SecurityError):
        validate_slug("../bad")
