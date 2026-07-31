from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.analyze import StoryAnalyzer
from app.config import load_settings
from app.normalization import normalize_analysis
from app.pipeline import BuildRequest, BuildTarget, PipelineOrchestrator
from app.pipeline.production_adapters import build_production_adapters
from app.voice_planner import (
    ManualOverride,
    apply_manual_override,
    load_editable_voice_plan,
    load_voice_registry,
    merge_voice_plans,
    save_voice_plan_atomic,
    serialize_assignment_report,
    serialize_character_profiles,
    serialize_voice_plan,
)

from .config import WebSettings
from .models import ProjectRecord
from .projects import ProjectManager


class WebApplicationError(RuntimeError):
    """A user-facing, blocking application-layer workflow error."""


class WebApplicationService:
    """Translate web actions into existing app-layer operations.

    The web controller intentionally does not implement a second pipeline.  It
    registers the real application-engine adapters from app.pipeline and lets
    the orchestrator report any missing data/backend contract explicitly.
    """

    def __init__(self, settings: WebSettings, projects: ProjectManager) -> None:
        self.settings = settings
        self.projects = projects
        # Web builds use the real engine adapters; FakeStageAdapter is reserved
        # for isolated orchestrator tests and is never registered here.
        self.pipeline_adapters = build_production_adapters()

    def run(self, project: ProjectRecord, action: str) -> dict[str, Any]:
        if action == "analyze":
            return self.analyze(project)
        if action == "normalize":
            return self.normalize(project)
        if action == "plan":
            return self.plan(project)
        if action == "manifest":
            return self.manifest(project)
        if action == "build":
            return self.build(project)
        raise WebApplicationError(f"unsupported web workflow action: {action}")

    def analyze(self, project: ProjectRecord) -> dict[str, Any]:
        settings = load_settings()
        root = self.projects.project_paths(project.project_slug)
        settings.paths.output_dir = root.work_dir / "analysis"
        settings.paths.temp_dir = root.work_dir / "analysis-temp"
        settings.paths.log_dir = root.logs_dir
        result = StoryAnalyzer(settings).analyze(self.projects.project_source_path(project))
        project.analysis_path = str(result.analysis_dir.relative_to(root.root))
        project.analysis_state = "completed"
        project.analysis_status = "completed"
        self.projects.save_project(project)
        return result.to_dict()

    def normalize(self, project: ProjectRecord) -> dict[str, Any]:
        root = self.projects.project_paths(project.project_slug)
        analysis_dir = self._analysis_dir(project, root)
        output_dir = root.work_dir / "normalized"
        result = normalize_analysis(analysis_dir, output_dir)
        project.normalized_path = str(output_dir.relative_to(root.root))
        project.normalized_analysis_path = project.normalized_path
        project.normalization_status = "completed"
        story = self._json(output_dir / "normalized_story.json")
        project.series_id = str(story.get("series_id", ""))
        project.artifact_map.update(
            {
                "analysis": project.analysis_path,
                "normalized_analysis": project.normalized_path,
            }
        )
        self.projects.save_project(project)
        return result

    def plan(self, project: ProjectRecord) -> dict[str, Any]:
        root = self.projects.project_paths(project.project_slug)
        normalized = self._require_dir(project, root, "normalized_path", "normalized analysis")
        from app.voice_planner import (
            AssignmentContext,
            BudgetContext,
            SceneConflictContext,
            ScoreContext,
            analyze_scene_conflicts,
            assign_voices,
            calculate_voice_budget,
            load_character_profiles,
            rank_voice_candidates,
        )

        bundle = load_character_profiles(normalized)
        registry = self._voice_registry(project, root)
        bindings = self._series_bindings(project, root)
        voices = tuple(registry.get("voices", ()))
        candidates: dict[str, tuple[Any, ...]] = {}
        for profile in bundle.profiles:
            scores = rank_voice_candidates(
                voices,
                ScoreContext(role="character", character_profile=profile, series_bindings=bindings),
            )
            candidates[profile.canonical_character_id] = tuple(scores)
        narrator_scores = tuple(
            rank_voice_candidates(voices, ScoreContext(role="narrator", series_bindings=bindings))
        )
        all_scores = (
            tuple(score for values in candidates.values() for score in values) + narrator_scores
        )
        budget = calculate_voice_budget(
            BudgetContext(
                tuple(bundle.profiles),
                all_scores,
                bindings,
                True,
                load_settings().voice_planner.budget,
            )
        )
        story = self._json(normalized / "normalized_story.json")
        scenes = tuple(self._json(normalized / "normalized_scenes.json").get("scenes", ()))
        dialogue = tuple(self._json(normalized / "normalized_dialogue.json").get("dialogue", ()))
        conflicts = analyze_scene_conflicts(
            SceneConflictContext(
                tuple(bundle.profiles),
                scenes,
                dialogue,
                candidates,
                budget,
                bindings,
                registry,
                load_settings().voice_planner.conflicts,
            )
        )
        context = AssignmentContext(
            book_id=str(story.get("book_id") or project.project_slug),
            series_id=str(story.get("series_id") or project.series_id or project.project_slug),
            source_analysis_path=str(story.get("source_analysis_path") or project.analysis_path),
            source_analysis_hash=str(
                story.get("source_analysis_hash")
                or hashlib.sha256((normalized / "normalized_story.json").read_bytes()).hexdigest()
            ),
            source_voice_registry_hash=hashlib.sha256(
                json.dumps(
                    registry,
                    sort_keys=True,
                    default=lambda value: (
                        asdict(value) if hasattr(value, "__dataclass_fields__") else str(value)
                    ),
                ).encode()
            ).hexdigest(),
            source_series_bindings_hash=None,
            character_profiles=tuple(bundle.profiles),
            registry=registry,
            series_bindings=bindings,
            candidate_scores_by_character=candidates,
            narrator_candidates=narrator_scores,
            voice_budget=budget,
            conflict_report=conflicts,
            config=load_settings().voice_planner,
            generated_by="storyforge.web",
        )
        result = assign_voices(context)
        generated = root.work_dir / "voice_plan.generated.json"
        assignment_report = root.work_dir / "voice_assignment_report.json"
        generated.write_text(serialize_voice_plan(result.voice_plan) + "\n", encoding="utf-8")
        assignment_report.write_text(
            serialize_assignment_report(result.assignment_report) + "\n", encoding="utf-8"
        )
        existing = root.work_dir / "voice_plan.json"
        previous = (
            load_editable_voice_plan(existing, registry=registry) if existing.exists() else None
        )
        editable = merge_voice_plans(previous, result.voice_plan, registry=registry).editable_plan
        save_voice_plan_atomic(existing, editable)
        profiles_path = root.work_dir / "character_profiles.json"
        profiles_path.write_text(serialize_character_profiles(bundle) + "\n", encoding="utf-8")
        project.character_profiles_path = str(profiles_path.relative_to(root.root))
        project.voice_plan_path = str(existing.relative_to(root.root))
        project.voice_assignment_report_path = str(assignment_report.relative_to(root.root))
        project.voice_plan_status = "completed"
        project.analysis_state = "planned"
        project.artifact_map.update(
            {
                "generated_voice_plan": str(generated.relative_to(root.root)),
                "editable_voice_plan": project.voice_plan_path,
                "assignment_report": project.voice_assignment_report_path,
            }
        )
        self.projects.save_project(project)
        return {
            "generated_plan": str(generated),
            "editable_plan": str(existing),
            "assignment_report": str(assignment_report),
            "assignments": asdict(result.voice_plan),
        }

    def manifest(self, project: ProjectRecord) -> dict[str, Any]:
        root = self.projects.project_paths(project.project_slug)
        normalized_dir = self._require_dir(project, root, "normalized_path", "normalized analysis")
        plan_path = self._require_artifact(project, "voice_plan_path", "editable voice plan")
        from app.voice_planner import (
            build_synthesis_manifest,
            save_synthesis_manifest_atomic,
            serialize_synthesis_manifest,
        )

        story = self._json(normalized_dir / "normalized_story.json")
        registry = self._voice_registry(project, root)
        plan = load_editable_voice_plan(plan_path, registry=registry)
        registry_payload = {
            **registry,
            "voices": [
                asdict(voice) if hasattr(voice, "__dataclass_fields__") else voice
                for voice in registry.get("voices", ())
            ],
        }
        config = {
            "voice_planner": {
                "renderer_contract_version": 1,
                "manifest_filename": "synthesis_manifest.json",
            }
        }
        result = build_synthesis_manifest(
            story,
            plan,
            registry_payload,
            config,
            source_artifacts=project.artifact_map,
            created_by="storyforge.web",
        )
        manifest_path = root.work_dir / "synthesis_manifest.json"
        save_synthesis_manifest_atomic(manifest_path, result.manifest)
        report_path = root.work_dir / "manifest.report.json"
        report_path.write_text(
            json.dumps(asdict(result.manifest.validation_report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        project.manifest_path = str(manifest_path.relative_to(root.root))
        project.synthesis_manifest_path = project.manifest_path
        project.synthesis_manifest_status = "completed"
        project.artifact_map.update({"synthesis_manifest": project.manifest_path})
        self.projects.save_project(project)
        return {
            "manifest_path": str(manifest_path),
            "report_path": str(report_path),
            "render_units": len(result.manifest.render_units),
            "ready_state": result.manifest.validation_report.ready_state,
            "manifest": json.loads(serialize_synthesis_manifest(result.manifest)),
        }

    def build(self, project: ProjectRecord) -> dict[str, Any]:
        root = self.projects.project_paths(project.project_slug)
        for field, label in (
            ("analysis_path", "analysis"),
            ("normalized_path", "normalized analysis"),
            ("voice_plan_path", "editable voice plan"),
            ("manifest_path", "synthesis manifest"),
        ):
            self._require_artifact(project, field, label)
        normalized = self._json(root.root / project.normalized_path / "normalized_story.json")
        editable = self._json(root.root / project.voice_plan_path)
        manifest = self._json(root.root / project.manifest_path)
        settings = load_settings()
        registry = self._voice_registry(project, root)
        registry_payload = {
            **registry,
            "voices": [
                asdict(voice) if hasattr(voice, "__dataclass_fields__") else voice
                for voice in registry.get("voices", ())
            ],
        }
        from app.renderer.providers import KokoroProviderAdapter

        provider_adapters: dict[str, Any] = {}
        for provider in {
            str(v.provider if hasattr(v, "provider") else v.get("provider"))
            for v in registry.get("voices", ())
            if (v.provider if hasattr(v, "provider") else v.get("provider"))
        }:
            provider_adapters[provider] = KokoroProviderAdapter(
                api_url=settings.kokoro.api_url,
                api_key=settings.kokoro.api_key,
                model=settings.kokoro.model,
                allowed_voice_ids={
                    str(
                        v.provider_voice_id
                        if hasattr(v, "provider_voice_id")
                        else v.get("provider_voice_id")
                    )
                    for v in registry.get("voices", ())
                    if (v.provider if hasattr(v, "provider") else v.get("provider")) == provider
                },
            )
        chapters = self._chapter_structure(normalized, manifest)
        metadata = {
            "title": normalized.get("title") or project.project_name,
            "author": normalized.get("author"),
            "series": normalized.get("series"),
            "language": normalized.get("language", "en"),
            "identifier": normalized.get("book_id", project.project_slug),
        }
        request = BuildRequest(
            project_id=project.project_id,
            book_id=str(normalized.get("book_id") or project.project_slug),
            story_input=normalized,
            voice_planning_config={"voice_registry": registry_payload, "editable_plan": editable},
            editable_plan=editable,
            manifest_config={"voice_registry": registry_payload, "manifest": manifest},
            renderer_config={
                "manifest": manifest,
                "provider_adapters": provider_adapters,
                "render_root": root.work_dir / "render",
                "output_format": "wav",
                "sample_rate_hz": 24000,
                "channel_count": 1,
                "sample_width_bytes": 2,
                "renderer_contract_version": 1,
                "report_path": root.work_dir / "render.report.json",
            },
            assembler_config={
                "manifest": manifest,
                "assembly_root": root.work_dir / "assemble",
                "segment_root": root.work_dir / "render",
                "assembly_contract_version": 1,
            },
            mastering_config={
                "mastering_root": root.work_dir / "master",
                "source_root": root.work_dir / "assemble",
                "mastering_contract_version": 1,
            },
            packaging_config={
                "metadata": metadata,
                "package_root": root.work_dir / "package",
                "mastered_root": root.work_dir / "master",
                "backend_name": "ffmpeg",
                "container_format": "m4b",
                "audio_codec": "aac",
            },
            canonical_chapter_structure=tuple(chapters),
            target_stage=BuildTarget.PACKAGE,
            workspace_root=root.root,
            pipeline_contract_version=project.pipeline_contract_version,
            orchestrator_version=project.orchestrator_version,
        )
        try:
            report = PipelineOrchestrator(self.pipeline_adapters).build_storyforge_project(request)
        except Exception as exc:  # noqa: BLE001
            self.projects.append_build_log(
                project.project_slug, f"build failure: {type(exc).__name__}: {exc}"
            )
            raise WebApplicationError(
                f"build is blocked by the application pipeline: {exc}"
            ) from exc
        if report.completion_status.value not in {"complete", "complete-with-warnings"}:
            message = "; ".join(report.errors) or "pipeline produced a blocking result"
            self.projects.append_build_log(project.project_slug, message)
            raise WebApplicationError(f"build blocked: {message}")
        project.last_pipeline_build_id = report.build_id
        project.last_build_id = report.build_id
        if report.final_artifact_ref:
            project.artifact_map["final_m4b"] = report.final_artifact_ref.relative_path
        project.artifact_map["build_report"] = (
            str(report.report_path.relative_to(root.root)) if report.report_path else ""
        )
        self.projects.save_project(project)
        return {
            "build_id": report.build_id,
            "completion_status": report.completion_status.value,
            "final_artifact": project.artifact_map.get("final_m4b", ""),
            "report": asdict(report),
        }

    def load_voice_plan(self, project: ProjectRecord):
        root = self.projects.project_paths(project.project_slug)
        path = self._require_artifact(project, "voice_plan_path", "editable voice plan")
        return load_editable_voice_plan(root.root / path)

    def save_voice_plan(self, project: ProjectRecord, payload: Mapping[str, Any]):
        root = self.projects.project_paths(project.project_slug)
        path = (
            root.root / project.voice_plan_path
            if project.voice_plan_path
            else root.work_dir / "voice_plan.json"
        )
        plan = load_editable_voice_plan(payload)
        save_voice_plan_atomic(path, plan)
        project.voice_plan_path = str(path.relative_to(root.root))
        project.voice_plan_status = "completed"
        self.projects.save_project(project)
        return plan

    def edit_voice_plan(self, project: ProjectRecord, override: ManualOverride):
        plan = self.load_voice_plan(project)
        updated = apply_manual_override(plan, override)
        root = self.projects.project_paths(project.project_slug)
        save_voice_plan_atomic(root.root / project.voice_plan_path, updated)
        self.projects.save_project(project)
        return updated

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WebApplicationError(f"invalid required JSON artifact {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise WebApplicationError(f"required JSON artifact is not an object: {path.name}")
        return value

    def _voice_registry(self, project: ProjectRecord, root) -> dict[str, Any]:
        settings = load_settings()
        configured = Path(settings.voice_planner.registry_path).expanduser()
        candidates = (
            root.root / configured,
            root.work_dir / configured,
            configured,
            self.settings.config_dir / configured,
        )
        for path in candidates:
            if path.is_file():
                try:
                    return load_voice_registry(path)
                except Exception as exc:  # noqa: BLE001
                    raise WebApplicationError(f"voice registry is invalid: {path}: {exc}") from exc
        raise WebApplicationError(
            "application pipeline blocked: missing required voice registry artifact; "
            "configure voice_planner.registry_path with a validated registry JSON"
        )

    def _series_bindings(self, project: ProjectRecord, root):
        from app.voice_planner import empty_series_bindings, load_series_bindings

        series_id = project.series_id or project.project_slug
        candidates = (
            root.work_dir / "series" / series_id / "bindings.json",
            root.root / "series" / series_id / "bindings.json",
        )
        for path in candidates:
            if path.is_file():
                return load_series_bindings(path, series_id=series_id)
        return empty_series_bindings(series_id)

    @staticmethod
    def _chapter_structure(
        story: Mapping[str, Any], manifest: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        chapters = story.get("chapters") or story.get("sections")
        if chapters:
            return [
                dict(chapter)
                for chapter in sorted(
                    chapters,
                    key=lambda item: (
                        int(item.get("chapter_order", item.get("order", 0))),
                        str(item.get("chapter_id", item.get("id", ""))),
                    ),
                )
            ]
        by_order: dict[int, dict[str, Any]] = {}
        for unit in manifest.get("render_units", ()):
            order = int((unit.get("source_order") or [1])[0])
            entry = by_order.setdefault(
                order,
                {
                    "chapter_id": f"chapter-{order}",
                    "chapter_order": order,
                    "chapter_title": f"Chapter {order}",
                    "render_unit_ids": [],
                    "scene_ids": [],
                },
            )
            entry["render_unit_ids"].append(unit.get("render_unit_id"))
            if unit.get("scene_id") not in entry["scene_ids"]:
                entry["scene_ids"].append(unit.get("scene_id"))
        return [by_order[key] for key in sorted(by_order)]

    def _analysis_dir(self, project: ProjectRecord, root) -> Path:
        if project.analysis_path:
            return self._require_dir(project, root, "analysis_path", "analysis")
        candidate = root.work_dir / "analysis"
        if candidate.exists():
            return candidate
        raise WebApplicationError("missing required analysis artifact: run analyze first")

    def _require_dir(self, project: ProjectRecord, root, field: str, label: str) -> Path:
        value = getattr(project, field, "")
        if not value:
            raise WebApplicationError(
                f"missing required {label} artifact: run the prior workflow action first"
            )
        path = (root.root / value).resolve()
        if not path.is_dir():
            raise WebApplicationError(f"missing required {label} artifact: {value}")
        return path

    def _require_artifact(self, project: ProjectRecord, field: str, label: str) -> Path:
        root = self.projects.project_paths(project.project_slug)
        value = getattr(project, field, "")
        if not value:
            raise WebApplicationError(
                f"missing required {label} artifact: run the prior workflow action first"
            )
        path = (root.root / value).resolve()
        if not path.exists():
            raise WebApplicationError(f"missing required {label} artifact: {value}")
        return path
