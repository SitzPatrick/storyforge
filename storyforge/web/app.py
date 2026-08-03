from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any, Protocol

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .application import ManualOverride, WebApplicationError
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
    def dashboard(
        request: Request,
        q: str = Query(""),
        status: str = Query(""),
        sort: str = Query("updated"),
    ):
        projects = services.projects.list_projects()
        needle = q.strip().lower()
        if needle:
            projects = [
                project
                for project in projects
                if needle in project.project_name.lower()
                or needle in project.project_slug.lower()
                or needle in project.source_book.lower()
            ]
        if status:
            projects = [project for project in projects if project.state == status]
        if sort == "name":
            projects.sort(key=lambda project: project.project_name.lower())
        elif sort == "status":
            projects.sort(key=lambda project: (project.state, project.project_name.lower()))
        return render(
            "index.html",
            request,
            projects=projects,
            settings=settings,
            active=services.jobs.active_status(),
            query=q,
            selected_status=status,
            selected_sort=sort,
            statuses=sorted({project.state for project in services.projects.list_projects()}),
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
        analyze_after_create: str = Form(""),
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
        if analyze_after_create == "on":
            try:
                services.jobs.start(record, "analyze")
            except JobBusyError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
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

    @app.get("/projects/{slug}/voice-plan")
    def voice_plan_page(request: Request, slug: str):
        project = load_project_or_404(slug)
        application = getattr(services, "application", None)
        plan = None
        error = None
        if application is not None:
            try:
                plan = application.load_voice_plan(project)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
        return render(
            "voice_plan.html", request, project=project, plan=plan, error=error, settings=settings
        )

    @app.get("/settings")
    def settings_page(request: Request):
        return render("settings.html", request, settings=settings, saved=False)

    @app.post("/settings")
    async def save_settings(request: Request):
        await request.form()
        return render("settings.html", request, settings=settings, saved=True)

    @app.get("/projects/{slug}/characters/{character_id}")
    def character_page(request: Request, slug: str, character_id: str):
        project = load_project_or_404(slug)
        return render(
            "character.html",
            request,
            project=project,
            character_id=character_id,
            settings=settings,
        )

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

    @app.post("/projects/{slug}/normalize")
    def normalize_project(slug: str):
        return _start_workflow(slug, "normalize")

    @app.post("/projects/{slug}/plan")
    def plan_project(slug: str, build_mode: str = Form("character-aware")):
        return _start_workflow(slug, "plan", build_mode=build_mode)

    @app.post("/projects/{slug}/manifest")
    def manifest_project(slug: str):
        return _start_workflow(slug, "manifest")

    def _start_workflow(slug: str, action: str, build_mode: str = "character-aware"):
        project = load_project_or_404(slug)
        if action in {"plan", "build"}:
            project.build_mode = (
                "single-voice" if str(build_mode).strip() == "single-voice" else "character-aware"
            )
            services.projects.save_project(project)
        try:
            services.jobs.start(project, action)
        except JobBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return RedirectResponse(url=f"/projects/{project.project_slug}/build", status_code=303)

    @app.post("/projects/{slug}/build")
    def build_project(slug: str, build_mode: str = Form("character-aware")):
        return _start_workflow(slug, "build", build_mode=build_mode)

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
        return _start_workflow(slug, "build", build_mode=project.build_mode)

    @app.get("/projects/{slug}/voice-plan.json")
    def voice_plan_json(slug: str):
        project = load_project_or_404(slug)
        application = getattr(services, "application", None)
        if application is None:
            raise HTTPException(status_code=503, detail="application service unavailable")
        try:
            from app.voice_planner import serialize_editable_voice_plan

            return __import__("json").loads(
                serialize_editable_voice_plan(application.load_voice_plan(project))
            )
        except WebApplicationError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None

    @app.post("/projects/{slug}/voice-plan")
    async def save_voice_plan(slug: str, request: Request):
        project = load_project_or_404(slug)
        application = getattr(services, "application", None)
        if application is None:
            raise HTTPException(status_code=503, detail="application service unavailable")
        try:
            payload = await request.json()
            updated = application.save_voice_plan(project, payload)
            from app.voice_planner import serialize_editable_voice_plan

            return __import__("json").loads(serialize_editable_voice_plan(updated))
        except (WebApplicationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.post("/projects/{slug}/voice-plan/edit")
    async def edit_voice_plan(request: Request, slug: str):
        project = load_project_or_404(slug)
        application = getattr(services, "application", None)
        if application is None:
            raise HTTPException(status_code=503, detail="application service unavailable")
        payload = await request.json()
        try:
            override = ManualOverride(
                target_kind=str(payload.get("target_kind", "character")),
                canonical_character_id=payload.get("canonical_character_id"),
                requested_provider=payload.get("requested_provider"),
                requested_provider_voice_id=payload.get("requested_provider_voice_id"),
                locked=payload.get("locked"),
                manual_override=payload.get("manual_override"),
                notes=payload.get("notes"),
                override_reason=payload.get("override_reason"),
            )
            updated = application.edit_voice_plan(project, override)
            from app.voice_planner import serialize_editable_voice_plan

            return __import__("json").loads(serialize_editable_voice_plan(updated))
        except (WebApplicationError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    @app.get("/api/voices")
    def voices_api():
        from app.kokoro_client import KokoroClient

        client = KokoroClient(settings.kokoro_url)
        voices = sorted(set(client.list_voices()) | {"af_heart"})
        return {"voices": [{"id": voice, "provider": "kokoro"} for voice in voices]}

    @app.get("/api/projects/{slug}/voice-editor")
    def voice_editor_api(slug: str):
        project = load_project_or_404(slug)
        application = getattr(services, "application", None)
        if application is None:
            raise HTTPException(status_code=503, detail="application service unavailable")
        from app.voice_planner import serialize_editable_voice_plan

        try:
            plan = json.loads(serialize_editable_voice_plan(application.load_voice_plan(project)))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=404, detail=str(exc)) from None
        return {"plan": plan}

    @app.get("/projects/{slug}/voice-preview/{voice_id}")
    def voice_preview(slug: str, voice_id: str):
        load_project_or_404(slug)
        if not voice_id.replace("_", "").isalnum():
            raise HTTPException(status_code=400, detail="invalid voice id")
        from app.kokoro_client import KokoroClient

        preview_root = settings.cache_dir / "voice-previews"
        preview_path = preview_root / f"{voice_id}.wav"
        kokoro_url = settings.kokoro_url.rstrip("/")
        if not kokoro_url.endswith("/v1"):
            kokoro_url += "/v1"
        if not preview_path.exists():
            KokoroClient(kokoro_url, voice=voice_id).synthesize(
                "This is a StoryForge voice preview.", preview_path
            )
        return FileResponse(str(preview_path), media_type="audio/wav", filename=preview_path.name)

    @app.post("/projects/{slug}/delete")
    def delete_project(slug: str):
        project = load_project_or_404(slug)
        services.projects.delete_project(project.project_slug)
        return RedirectResponse(url="/", status_code=303)

    @app.get("/projects/{slug}/download-key/{key}")
    def download_key(slug: str, key: str):
        project = load_project_or_404(slug)
        root = services.projects.project_paths(project.project_slug).root.resolve()
        known = dict(project.artifact_map)
        known.update(
            {
                "voice_plan": project.voice_plan_path,
                "assignment_report": project.voice_assignment_report_path,
                "manifest": project.manifest_path,
            }
        )
        relative = known.get(key, "")
        if not relative:
            raise HTTPException(status_code=404, detail="artifact not available")
        target = (root / relative).resolve()
        try:
            ensure_within_root(root, target)
        except SecurityError:
            raise HTTPException(status_code=403, detail="invalid artifact path") from None
        if not target.is_file():
            raise HTTPException(status_code=404, detail="artifact not available")
        return FileResponse(str(target), filename=target.name)

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
