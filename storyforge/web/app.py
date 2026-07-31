from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import WebSettings, load_web_settings
from .jobs import JobBusyError
from .projects import ProjectError
from .security import SecurityError, ensure_within_root, secure_filename, validate_slug
from .services import WebServices

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


class _ServicesLike(Protocol):
    projects: Any
    jobs: Any

    def diagnostics(self) -> dict[str, Any]: ...


def create_app(
    settings: WebSettings | None = None, services: _ServicesLike | None = None
) -> FastAPI:
    settings = settings or load_web_settings()
    services = services or WebServices.create(settings)
    app = FastAPI(title="StoryForge Web Controller", version=__import__("storyforge").__version__)
    app.state.web_settings = settings
    app.state.services = services
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    @app.on_event("startup")
    def _recover_stale_jobs() -> None:
        recover = getattr(services.jobs, "recover_stale_jobs", None)
        if recover is not None:
            recover()

    def render(template: str, request: Request, *, status_code: int = 200, **context):
        body = templates.get_template(template).render(**context)
        return HTMLResponse(body, status_code=status_code)

    def load_project_or_404(slug: str):
        try:
            clean_slug = validate_slug(slug)
            return services.projects.load_project(clean_slug)
        except (SecurityError, ProjectError):
            raise HTTPException(status_code=404, detail="project not found") from None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    def dashboard(request: Request):
        return render(
            "index.html",
            request,
            projects=services.projects.list_projects(),
            settings=settings,
            active=services.jobs.active_status(),
        )

    @app.get("/projects/new")
    def new_project(request: Request):
        books = [path for path in sorted(settings.books_dir.rglob("*.epub")) if path.is_file()]
        return render("project_new.html", request, books=books, settings=settings, error=None)

    @app.post("/projects")
    async def create_project(
        request: Request,
        project_name: str = Form(...),
        project_slug: str = Form(""),
        source_mode: str = Form("upload"),
        existing_book: str = Form(""),
        epub_file: Annotated[UploadFile | None, File()] = None,
    ):
        try:
            slug = project_slug.strip() or None
            if source_mode == "existing":
                if not existing_book:
                    raise ProjectError("Choose an EPUB from /data/books")
                source_path = (settings.books_dir / existing_book).resolve()
                ensure_within_root(settings.books_dir, source_path)
                if source_path.suffix.lower() != ".epub":
                    raise ProjectError("Selected file must be an EPUB")
                record = services.projects.create_project_from_existing(
                    project_name=project_name, project_slug=slug, existing_book_path=source_path
                )
            else:
                if epub_file is None:
                    raise ProjectError("Upload an EPUB file")
                filename = secure_filename(epub_file.filename or "book.epub")
                if not filename.lower().endswith(".epub"):
                    raise ProjectError("Uploaded file must be an EPUB")
                data = await _read_limited_upload(epub_file, settings.max_upload_bytes)
                record = services.projects.create_project(
                    project_name=project_name,
                    project_slug=slug,
                    source_filename=filename,
                    source_bytes=data,
                )
        except (SecurityError, ProjectError) as exc:
            books = [path for path in sorted(settings.books_dir.rglob("*.epub")) if path.is_file()]
            return render(
                "project_new.html",
                request,
                status_code=400,
                books=books,
                settings=settings,
                error=str(exc),
            )
        return RedirectResponse(url=f"/projects/{record.project_slug}", status_code=303)

    @app.get("/projects/{slug}")
    def project_page(request: Request, slug: str):
        project = load_project_or_404(slug)
        status = services.projects.load_status(project.project_slug)
        return render("project.html", request, project=project, status=status, settings=settings)

    @app.get("/projects/{slug}/build")
    def build_page(request: Request, slug: str):
        project = load_project_or_404(slug)
        status = services.projects.load_status(project.project_slug)
        return render("build.html", request, project=project, status=status, settings=settings)

    @app.get("/projects/{slug}/artifacts")
    def artifacts_page(request: Request, slug: str):
        project = load_project_or_404(slug)
        artifacts = services.projects.list_artifacts(project.project_slug)
        project_root = services.projects.project_paths(project.project_slug).root
        return render(
            "artifacts.html",
            request,
            project=project,
            artifacts=artifacts,
            project_root=project_root,
            settings=settings,
        )

    @app.get("/diagnostics")
    def diagnostics_page(request: Request):
        return render(
            "diagnostics.html", request, diagnostics=services.diagnostics(), settings=settings
        )

    @app.get("/projects/{slug}/status")
    def project_status(slug: str):
        project = load_project_or_404(slug)
        return services.projects.load_status(project.project_slug).to_dict()

    @app.get("/projects/{slug}/log")
    def project_log(slug: str):
        project = load_project_or_404(slug)
        status = services.projects.load_status(project.project_slug)
        tail = services.projects.read_build_log_tail(project.project_slug, limit=200)
        return {"status": status.status, "lines": tail}

    @app.post("/projects/{slug}/analyze")
    def analyze_project(slug: str):
        project = load_project_or_404(slug)
        try:
            services.jobs.start(project, "analyze")
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return RedirectResponse(url=f"/projects/{project.project_slug}/build", status_code=303)

    @app.post("/projects/{slug}/build")
    def build_project(slug: str):
        project = load_project_or_404(slug)
        try:
            services.jobs.start(project, "build")
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return RedirectResponse(url=f"/projects/{project.project_slug}/build", status_code=303)

    @app.post("/projects/{slug}/cancel")
    def cancel_project(slug: str):
        project = load_project_or_404(slug)
        try:
            services.jobs.cancel(project.project_slug)
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return RedirectResponse(url=f"/projects/{slug}/build", status_code=303)

    @app.post("/projects/{slug}/resume")
    def resume_project(slug: str):
        project = load_project_or_404(slug)
        try:
            services.jobs.start(project, "build")
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return RedirectResponse(url=f"/projects/{project.project_slug}/build", status_code=303)

    @app.get("/projects/{slug}/download/{relative_path:path}")
    def download_artifact(slug: str, relative_path: str):
        project = load_project_or_404(slug)
        project_root = services.projects.project_paths(project.project_slug).root
        artifacts_root = services.projects.project_paths(
            project.project_slug
        ).artifacts_dir.resolve()
        try:
            target = (project_root / relative_path).resolve()
            ensure_within_root(artifacts_root, target)
        except SecurityError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        if not target.is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        if target.suffix.lower() not in {".m4b", ".wav", ".json", ".txt", ".log"}:
            raise HTTPException(status_code=403, detail="artifact type not allowed")
        return FileResponse(str(target), filename=target.name)

    return app


async def _read_limited_upload(upload: UploadFile, limit: int) -> bytes:
    data = bytearray()
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > limit:
            raise ProjectError(f"Upload exceeds maximum size of {limit} bytes")
    return bytes(data)


def _drop_privileges() -> None:
    uid = os.getenv("PUID")
    gid = os.getenv("PGID")
    umask_value = os.getenv("UMASK")
    if umask_value:
        try:
            os.umask(int(umask_value, 8))
        except Exception:
            pass
    if os.geteuid() != 0:
        return
    try:
        if gid:
            os.setgid(int(gid))
        if uid:
            os.setuid(int(uid))
    except Exception:
        return


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Run the StoryForge Unraid web controller.")
    parser.add_argument("--host", default=load_web_settings().host)
    parser.add_argument("--port", default=load_web_settings().port, type=int)
    args = parser.parse_args(argv)
    _drop_privileges()
    import uvicorn

    uvicorn.run("storyforge.web.app:create_app", host=args.host, port=args.port, factory=True)
    return 0
