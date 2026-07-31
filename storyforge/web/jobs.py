from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass

from .config import WebSettings
from .models import JobStatus, ProjectRecord, now_iso
from .projects import ProjectManager


@dataclass
class ActiveJob:
    job_id: str
    project_slug: str
    action: str
    process: subprocess.Popen[str]
    status: JobStatus
    thread: threading.Thread


class JobBusyError(RuntimeError):
    pass


class JobManager:
    def __init__(self, settings: WebSettings, projects: ProjectManager) -> None:
        self.settings = settings
        self.projects = projects
        self._lock = threading.Lock()
        self._active: ActiveJob | None = None

    def active_status(self) -> JobStatus | None:
        with self._lock:
            if self._active is None:
                return None
            return self._active.status

    def recover_stale_jobs(self) -> list[JobStatus]:
        recovered: list[JobStatus] = []
        with self._lock:
            if self._active is not None:
                return recovered
            for project in self.projects.list_projects():
                status = self.projects.load_status(project.project_slug)
                if status.status not in {"running", "cancelling", "queued"} and not status.active:
                    continue
                now = now_iso()
                status.status = "failed"
                status.stage = "failed"
                status.message = "stale build state cleared after restart"
                status.return_code = None
                status.pid = None
                status.active = False
                status.finished_at = now
                status.updated_at = now
                status.build_log_tail = self.projects.read_build_log_tail(project.project_slug)
                self.projects.save_status(status)
                project.state = status.status
                project.updated_at = now
                self.projects.save_project(project)
                recovered.append(status)
        return recovered

    def start(self, project: ProjectRecord, action: str) -> JobStatus:
        with self._lock:
            self._prune_locked()
            if self._active is not None:
                raise JobBusyError("Another build is already running")
            status = self.projects.load_status(project.project_slug)
            job_id = uuid.uuid4().hex
            now = now_iso()
            status.job_id = job_id
            status.action = action
            status.status = "running"
            status.stage = "starting"
            status.message = f"starting {action}"
            status.started_at = now
            status.updated_at = now
            status.finished_at = ""
            status.return_code = None
            status.pid = None
            status.active = True
            status.source_book = project.source_book
            status.build_log_tail = []
            project.state = "running"
            project.last_build_id = job_id
            project.updated_at = now
            self.projects.save_status(status)
            self.projects.save_project(project)
            command = [
                sys.executable,
                "-m",
                "storyforge.web.worker",
                action,
                "--project-dir",
                str(self.projects.project_paths(project.project_slug).root),
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(self.projects.project_paths(project.project_slug).root),
                env=self._worker_env(),
                start_new_session=True,
            )
            status.pid = process.pid
            status.message = f"process started (pid {process.pid})"
            self.projects.save_status(status)
            thread = threading.Thread(
                target=self._collect_output,
                args=(project.project_slug, job_id, action, process),
                daemon=True,
            )
            self._active = ActiveJob(
                job_id=job_id,
                project_slug=project.project_slug,
                action=action,
                process=process,
                status=status,
                thread=thread,
            )
            thread.start()
            return status

    def cancel(self, project_slug: str | None = None) -> JobStatus:
        with self._lock:
            if self._active is None:
                raise JobBusyError("No active build")
            if project_slug and self._active.project_slug != project_slug:
                raise JobBusyError("Active build belongs to a different project")
            active = self._active
            active.status.status = "cancelling"
            active.status.stage = "cancelling"
            active.status.message = "cancelling build"
            active.status.updated_at = now_iso()
            self.projects.save_status(active.status)
            try:
                active.process.terminate()
            except Exception:
                pass
        try:
            active.process.wait(timeout=5)
        except Exception:
            try:
                active.process.kill()
            except Exception:
                pass
        return active.status

    def _prune_locked(self) -> None:
        if self._active is None:
            return
        if self._active.process.poll() is None:
            return
        self._active = None

    def _collect_output(
        self, project_slug: str, job_id: str, action: str, process: subprocess.Popen[str]
    ) -> None:
        build_log = self.projects.project_paths(project_slug).build_log
        build_log.parent.mkdir(parents=True, exist_ok=True)
        with build_log.open("a", encoding="utf-8") as log_handle:
            stdout = process.stdout or []
            for raw_line in stdout:
                line = raw_line.rstrip("\n")
                log_handle.write(raw_line)
                log_handle.flush()
                self._update_from_line(project_slug, job_id, action, line)
            return_code = process.wait()
        with self._lock:
            status = self.projects.load_status(project_slug)
            if status.job_id != job_id:
                return
            status.pid = process.pid
            status.return_code = return_code
            status.active = False
            status.updated_at = now_iso()
            status.finished_at = now_iso()
            if status.status == "cancelling":
                status.status = "cancelled"
                status.stage = "cancelled"
                status.message = "build cancelled"
            elif return_code == 0:
                status.status = "completed"
                status.stage = "completed"
                status.message = f"{action} completed"
            else:
                status.status = "failed"
                status.stage = "failed"
                status.message = f"{action} failed (exit {return_code})"
            status.build_log_tail = self.projects.read_build_log_tail(project_slug)
            self.projects.save_status(status)
            project = self.projects.load_project(project_slug)
            project.updated_at = now_iso()
            project.state = status.status
            self.projects.save_project(project)
            self._active = None

    def _update_from_line(self, project_slug: str, job_id: str, action: str, line: str) -> None:
        with self._lock:
            status = self.projects.load_status(project_slug)
            if status.job_id != job_id:
                return
            status.updated_at = now_iso()
            status.build_log_tail = self.projects.read_build_log_tail(project_slug)
            status.message = line[:500]
            if action == "build":
                if match := re.search(r"^Chapters total:\s*(\d+)", line):
                    status.total_chapters = int(match.group(1))
                    status.stage = "building"
                elif match := re.search(r"^Chapter start:\s*(\d+)\s+(.*)$", line):
                    status.current_chapter = int(match.group(1))
                    status.stage = "building"
                elif match := re.search(r"^Chapter complete:\s*(\d+)/(\d+)", line):
                    status.current_chapter = int(match.group(1))
                    status.total_chapters = int(match.group(2))
                    status.stage = "building"
                elif line.startswith("M4B creation start:"):
                    status.stage = "packaging"
            elif action == "analyze":
                if (
                    line.startswith("Analyzing:")
                    or line.startswith("Title:")
                    or line.startswith("Provider:")
                ):
                    status.stage = "analyzing"
            self.projects.save_status(status)

    def _worker_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "STORYFORGE_HOST": self.settings.host,
                "STORYFORGE_PORT": str(self.settings.port),
                "STORYFORGE_CONFIG_DIR": str(self.settings.config_dir),
                "STORYFORGE_DATA_DIR": str(self.settings.data_dir),
                "STORYFORGE_BOOKS_DIR": str(self.settings.books_dir),
                "STORYFORGE_PROJECTS_DIR": str(self.settings.projects_dir),
                "STORYFORGE_CACHE_DIR": str(self.settings.cache_dir),
                "STORYFORGE_OUTPUT_DIR": str(self.settings.output_dir),
                "STORYFORGE_LOG_DIR": str(self.settings.log_dir),
                "KOKORO_URL": self.settings.kokoro_url,
            }
        )
        return env
