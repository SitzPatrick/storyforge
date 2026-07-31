"""Production stage adapters for the StoryForge pipeline.

This module is deliberately thin: the adapters translate the orchestrator's
request/context contract into calls to the existing planner, manifest,
renderer, assembler, mastering, and packaging engines. They do not contain
an alternative implementation of any stage.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any

from .adapters import StageAdapter, StageContext
from .models import (
    ArtifactRef,
    BuildFailure,
    BuildFailureType,
    BuildRequest,
    BuildStage,
    StageAction,
    StageInspection,
    StageResult,
    StageStatus,
)
from .serialization import canonical_json


class ProductionAdapterError(RuntimeError):
    """Raised when a production stage lacks a safe engine input contract."""


def _config(cls: type, value: Mapping[str, Any] | None, **overrides: Any) -> Any:
    payload = dict(value or {})
    names = {field.name for field in fields(cls)}
    payload = {key: item for key, item in payload.items() if key in names}
    payload.update({key: item for key, item in overrides.items() if key in names})
    return cls(**payload)


def _write_json(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = canonical_json(value) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return {field.name: _plain(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if hasattr(value, "value") and not isinstance(value, (str, bytes)):
        return value.value
    if hasattr(value, "provider_id"):
        return {
            "provider_id": str(value.provider_id),
            "adapter_version": str(getattr(value, "adapter_version", "")),
            "model_version": str(getattr(value, "model_version", "")),
        }
    return str(value)


class ProductionStageAdapter:
    """Thin adapter around one real engine operation."""

    def __init__(self, stage: BuildStage, dependencies: tuple[BuildStage, ...] = ()) -> None:
        self.stage = stage
        self.dependencies = dependencies
        self._cache: dict[str, StageResult] = {}

    def input_identity(self, request: BuildRequest, context: StageContext) -> str:
        payload = {
            "stage": self.stage.value,
            "project_id": request.project_id,
            "book_id": request.book_id,
            "story_input": _plain(request.story_input),
            "editable_plan": _plain(request.editable_plan),
            "configs": [
                _plain(getattr(request, name))
                for name in (
                    "voice_planning_config",
                    "manifest_config",
                    "renderer_config",
                    "assembler_config",
                    "mastering_config",
                    "packaging_config",
                )
            ],
        }
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    def inspect(
        self,
        request: BuildRequest,
        context: StageContext,
        *,
        expected_input_identity: str,
        force_rebuild: bool = False,
    ) -> StageInspection:
        cached = self._cache.get(expected_input_identity)
        if cached is None:
            artifact_path = context.stage_root / f"{self.stage.value}.json"
            report_path = context.project_root / f"{self.stage.value}.adapter.report.json"
            if artifact_path.is_file() and report_path.is_file():
                try:
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    valid = (
                        report.get("input_identity") == expected_input_identity
                        and report.get("output")
                        == hashlib.sha256(artifact_path.read_bytes()).hexdigest()
                    )
                    if valid:
                        identity = str(report["output"])
                        ref = ArtifactRef(
                            stage=self.stage,
                            relative_path=str(artifact_path.relative_to(context.project_root)),
                            content_hash=identity,
                            identity=identity,
                            report_relative_path=str(report_path.relative_to(context.project_root)),
                        )
                        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                        engine_refs = _payload_artifact_refs(self.stage, payload, context)
                        cached = StageResult(
                            stage=self.stage,
                            status=StageStatus.COMPLETED,
                            action=StageAction.REUSE,
                            started=False,
                            completed=True,
                            cache_reused=True,
                            new_artifacts=False,
                            artifact_refs=(
                                engine_refs + (ref,)
                                if self.stage == BuildStage.PACKAGE
                                else (ref,) + engine_refs
                            ),
                            stage_report_ref=str(report_path.relative_to(context.project_root)),
                            input_identity=expected_input_identity,
                            output_identity=identity,
                            requested=True,
                            reason="validated persisted production artifact cache reusable",
                        )
                except (OSError, ValueError, json.JSONDecodeError):
                    cached = None
        reusable = cached is not None and cached.completed and not force_rebuild
        return StageInspection(
            stage=self.stage,
            requested=True,
            dependency_status=StageStatus.PENDING,
            input_identity=expected_input_identity,
            output_identity=cached.output_identity if cached else "",
            prior_artifact_state="present" if cached else "missing",
            cache_reusable=reusable,
            action=(
                StageAction.REUSE
                if reusable
                else (StageAction.FORCE_REBUILD if force_rebuild else StageAction.EXECUTE)
            ),
            reason=(
                "validated production adapter cache reusable"
                if reusable
                else "production engine execution required"
            ),
            artifact_refs=cached.artifact_refs if cached else (),
            report_ref=cached.stage_report_ref if cached else None,
        )

    def execute(
        self,
        request: BuildRequest,
        context: StageContext,
        *,
        expected_input_identity: str,
        force_rebuild: bool = False,
        dry_run: bool = False,
    ) -> StageResult:
        if dry_run:
            return StageResult(
                stage=self.stage,
                status=StageStatus.DRY_RUN,
                action=StageAction.DRY_RUN_ONLY,
                started=False,
                completed=False,
                cache_reused=False,
                new_artifacts=False,
                input_identity=expected_input_identity,
                output_identity=expected_input_identity,
                requested=True,
                reason="dry run",
            )
        try:
            payload = self._run_engine(request, context)
            if self.stage == BuildStage.ASSEMBLE:
                _materialize_mastering_sidecars(payload)
            if not isinstance(payload, Mapping):
                raise ProductionAdapterError(
                    f"{self.stage.value} engine returned non-mapping result"
                )
            _validate_engine_payload(self.stage, payload, context)
            artifact_path = context.stage_root / f"{self.stage.value}.json"
            output_identity = _write_json(artifact_path, payload)
            report_path = context.project_root / f"{self.stage.value}.adapter.report.json"
            _write_json(
                report_path,
                {
                    "stage": self.stage.value,
                    "input_identity": expected_input_identity,
                    "output": output_identity,
                },
            )
            ref = ArtifactRef(
                stage=self.stage,
                relative_path=str(artifact_path.relative_to(context.project_root)),
                content_hash=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                identity=output_identity,
                report_relative_path=str(report_path.relative_to(context.project_root)),
            )
            result = StageResult(
                stage=self.stage,
                status=StageStatus.COMPLETED,
                action=StageAction.FORCE_REBUILD if force_rebuild else StageAction.EXECUTE,
                started=True,
                completed=True,
                cache_reused=False,
                new_artifacts=True,
                artifact_refs=(ref,),
                stage_report_ref=str(report_path.relative_to(context.project_root)),
                input_identity=expected_input_identity,
                output_identity=output_identity,
                requested=True,
                reason="executed",
            )
            engine_refs = _payload_artifact_refs(self.stage, payload, context)
            result = result.__class__(
                **{
                    **result.__dict__,
                    "artifact_refs": (
                        engine_refs + (ref,)
                        if self.stage == BuildStage.PACKAGE
                        else (ref,) + engine_refs
                    ),
                }
            )
            self._cache[expected_input_identity] = result
            return result
        except ProductionAdapterError as exc:
            failure = BuildFailure(
                BuildFailureType.DEPENDENCY_UNAVAILABLE,
                str(exc),
                stage=self.stage,
                details={"adapter": type(self).__name__},
            )
            return StageResult(
                stage=self.stage,
                status=StageStatus.BLOCKED,
                action=StageAction.EXECUTE,
                started=True,
                completed=False,
                cache_reused=False,
                new_artifacts=False,
                input_identity=expected_input_identity,
                failures=(failure,),
                blocking=True,
                requested=True,
                dependency_status=StageStatus.BLOCKED,
                reason=str(exc),
            )

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        raise NotImplementedError


def _upstream_payload(context: StageContext, stage: BuildStage) -> Mapping[str, Any] | None:
    result = context.upstream_stage_results.get(stage)
    if result is None or not result.artifact_refs:
        return None
    path = context.project_root / result.artifact_refs[0].relative_path
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _upstream_root(context: StageContext, stage: BuildStage) -> Path:
    result = context.upstream_stage_results.get(stage)
    if result and result.artifact_refs:
        return (context.project_root / result.artifact_refs[0].relative_path).parent
    return context.workspace_root / stage.value


def _project_path(value: Any, context: StageContext, label: str) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = context.project_root / path
    path = path.resolve(strict=False)
    project_root = context.project_root.resolve(strict=False)
    try:
        path.relative_to(project_root)
    except ValueError as exc:
        raise ProductionAdapterError(f"{label} escapes project workspace: {path}") from exc
    return path


def _materialize_mastering_sidecars(payload: Mapping[str, Any]) -> None:
    """Bridge assembler's audio-suffixed sidecars to mastering's canonical name."""
    for record in payload.get("chapter_results", ()) or ():
        sidecar_value = record.get("sidecar_path") if isinstance(record, Mapping) else None
        audio_value = record.get("output_artifact_path") if isinstance(record, Mapping) else None
        if not sidecar_value or not audio_value:
            continue
        source = Path(str(sidecar_value))
        target = source.parent / "chapter_sidecar.json"
        if source.is_file() and not target.exists():
            shutil.copyfile(source, target)


def _require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ProductionAdapterError(f"{label} missing or empty: {path}")


def _validate_engine_payload(
    stage: BuildStage, payload: Mapping[str, Any], context: StageContext
) -> None:
    """Reject success-shaped engine results whose durable outputs are absent."""
    if stage in {BuildStage.PLAN, BuildStage.APPLY_EDITS, BuildStage.MANIFEST}:
        return
    raw_status = payload.get("completion_status", payload.get("status", ""))
    status = str(getattr(raw_status, "value", raw_status)).lower()
    if status not in {"complete", "complete-with-warnings", "completed", "passed"}:
        errors = payload.get("errors") or []
        raise ProductionAdapterError(
            f"{stage.value} engine did not complete: {status or 'missing status'}"
            + (f"; {errors[0]}" if errors else "")
        )

    if stage == BuildStage.RENDER:
        report_path = context.report_root / "render.report.json"
        _require_file(report_path, "render report")
        units = payload.get("unit_results") or []
        if not units:
            raise ProductionAdapterError("render produced no render units")
        for unit in units:
            unit_status = str(unit.get("status", "")).lower()
            if unit_status in {"skipped", "omitted"}:
                continue
            audio = Path(str(unit.get("output_path", "")))
            sidecar = Path(str(unit.get("sidecar_path", "")))
            audio = _project_path(audio, context, "render audio")
            sidecar = _project_path(sidecar, context, "render sidecar")
            _require_file(audio, "render segment audio")
            _require_file(sidecar, "render segment sidecar")
    elif stage == BuildStage.ASSEMBLE:
        report_path = context.stage_root / "chapter_assembly_report.json"
        _require_file(report_path, "assembly report")
        chapters = payload.get("chapter_results") or []
        if not chapters:
            raise ProductionAdapterError("assembly produced no chapters")
        for chapter in chapters:
            if str(chapter.get("status", "")).lower() in {"blocked", "failed", "missing"}:
                raise ProductionAdapterError(
                    f"assembly chapter failed: {chapter.get('chapter_id', '<unknown>')}"
                )
            _require_file(
                _project_path(chapter.get("output_artifact_path", ""), context, "chapter audio"),
                "chapter audio",
            )
            _require_file(
                _project_path(chapter.get("sidecar_path", ""), context, "chapter sidecar"),
                "chapter sidecar",
            )
    elif stage == BuildStage.MASTER:
        chapters = payload.get("chapter_results") or []
        if not chapters:
            raise ProductionAdapterError("mastering produced no chapters")
        for chapter in chapters:
            if str(chapter.get("status", "")).lower() in {"blocked", "failed", "missing"}:
                raise ProductionAdapterError(
                    f"mastering chapter failed: {chapter.get('chapter_id', '<unknown>')}"
                )
            _require_file(
                _project_path(chapter.get("output_artifact_path", ""), context, "mastered audio"),
                "mastered audio",
            )
            _require_file(
                _project_path(chapter.get("sidecar_path", ""), context, "mastering sidecar"),
                "mastering sidecar",
            )
    elif stage == BuildStage.PACKAGE:
        output = _project_path(payload.get("output_artifact_path", ""), context, "final M4B")
        sidecar = _project_path(payload.get("sidecar_path", ""), context, "package sidecar")
        report = _project_path(payload.get("report_path", ""), context, "packaging report")
        for path, label in (
            (output, "final M4B"),
            (sidecar, "package sidecar"),
            (report, "packaging report"),
        ):
            _require_file(path, label)
        if output.suffix.lower() != ".m4b":
            raise ProductionAdapterError(f"packaging output is not an M4B: {output}")


def _payload_artifact_refs(
    stage: BuildStage, payload: Mapping[str, Any], context: StageContext
) -> tuple[ArtifactRef, ...]:
    """Return canonical project-relative refs for engine outputs plus the adapter artifact."""
    refs: list[ArtifactRef] = []

    def add(path_value: Any, report: str | None = None) -> None:
        if not path_value:
            return
        path = _project_path(path_value, context, f"{stage.value} artifact")
        if not path.is_file():
            return
        refs.append(
            ArtifactRef(
                stage=stage,
                relative_path=str(path.relative_to(context.project_root)),
                content_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                identity=hashlib.sha256(path.read_bytes()).hexdigest(),
                report_relative_path=report,
            )
        )

    if stage == BuildStage.PACKAGE:
        add(
            payload.get("output_artifact_path"),
            str(Path(str(payload.get("report_path"))).relative_to(context.project_root)),
        )
        add(payload.get("sidecar_path"))
        add(payload.get("report_path"))
    elif stage == BuildStage.RENDER:
        add(context.report_root / "render.report.json")
        for unit in payload.get("unit_results", ()):
            add(unit.get("output_path"))
            add(unit.get("sidecar_path"))
    elif stage == BuildStage.ASSEMBLE:
        add(context.stage_root / "chapter_assembly_report.json")
        for chapter in payload.get("chapter_results", ()):
            add(
                chapter.get("output_artifact_path"),
                str(
                    Path(str(context.stage_root / "chapter_assembly_report.json")).relative_to(
                        context.project_root
                    )
                ),
            )
            add(chapter.get("sidecar_path"))
    elif stage == BuildStage.MASTER:
        for chapter in payload.get("chapter_results", ()):
            add(chapter.get("output_artifact_path"))
            add(chapter.get("sidecar_path"))
    return tuple(refs)


class PlanStageAdapter(ProductionStageAdapter):
    def __init__(self) -> None:
        super().__init__(BuildStage.PLAN)

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        if request.editable_plan is None:
            raise ProductionAdapterError("voice planning input is missing; provide editable_plan")
        return _plain(request.editable_plan)


class ApplyEditsStageAdapter(ProductionStageAdapter):
    def __init__(self) -> None:
        super().__init__(BuildStage.APPLY_EDITS, (BuildStage.PLAN,))

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        if request.editable_plan is None:
            raise ProductionAdapterError("editable voice plan is missing")
        return _plain(request.editable_plan)


class ManifestStageAdapter(ProductionStageAdapter):
    def __init__(self) -> None:
        super().__init__(BuildStage.MANIFEST, (BuildStage.APPLY_EDITS,))

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        from app.voice_planner import build_synthesis_manifest, serialize_synthesis_manifest

        story = request.story_input if isinstance(request.story_input, Mapping) else None
        registry = request.manifest_config.get(
            "voice_registry"
        ) or request.voice_planning_config.get("voice_registry")
        if story is None or not registry or request.editable_plan is None:
            raise ProductionAdapterError(
                "manifest requires story_input, editable_plan, and manifest_config.voice_registry"
            )
        result = build_synthesis_manifest(
            story, request.editable_plan, registry, request.manifest_config
        )
        return json.loads(serialize_synthesis_manifest(result.manifest))


class RenderStageAdapter(ProductionStageAdapter):
    def __init__(self) -> None:
        super().__init__(BuildStage.RENDER, (BuildStage.MANIFEST,))

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        from app.renderer import SegmentRenderer
        from app.renderer.models import RenderContext

        manifest = request.renderer_config.get("manifest") or request.story_input
        providers = request.renderer_config.get("provider_adapters") or {}
        if not providers:
            raise ProductionAdapterError("render requires renderer_config.provider_adapters")
        cfg = _config(
            RenderContext,
            request.renderer_config,
            render_root=context.stage_root,
            report_path=context.report_root / "render.report.json",
        )
        report = SegmentRenderer(cfg).render(
            manifest,
            adapters=providers,
            allow_ready_with_warnings=request.renderer_config.get("allow_ready_with_warnings"),
        )
        return _plain(report)


class EngineReportStageAdapter(ProductionStageAdapter):
    """Base for stages whose engine input is produced by the prior report."""

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        raise ProductionAdapterError(
            f"{self.stage.value} adapter requires an explicit engine input mapping"
        )


class AssembleStageAdapter(EngineReportStageAdapter):
    def __init__(self) -> None:
        super().__init__(BuildStage.ASSEMBLE, (BuildStage.RENDER,))

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        from app.assembler import ChapterAssembler
        from app.assembler.models import ChapterAssemblyConfig

        manifest = (
            request.assembler_config.get("manifest")
            or request.manifest_config.get("manifest")
            or _upstream_payload(context, BuildStage.MANIFEST)
        )
        if manifest is None:
            raise ProductionAdapterError("assemble requires assembler_config.manifest")
        cfg = _config(
            ChapterAssemblyConfig,
            request.assembler_config,
            assembly_root=context.stage_root,
            segment_root=_upstream_root(context, BuildStage.RENDER),
        )
        chapter_structure = request.canonical_chapter_structure or request.assembler_config.get(
            "chapter_structure"
        )
        if not chapter_structure:
            raise ProductionAdapterError("assemble requires canonical_chapter_structure")
        return _plain(
            ChapterAssembler(
                manifest, chapter_structure_source={"chapters": list(chapter_structure)}, config=cfg
            ).assemble()
        )


class MasterStageAdapter(EngineReportStageAdapter):
    def __init__(self) -> None:
        super().__init__(BuildStage.MASTER, (BuildStage.ASSEMBLE,))

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        from app.mastering import MasteringEngine
        from app.mastering.models import MasteringConfig

        records = request.mastering_config.get("chapter_records")
        if records is None:
            assembly = _upstream_payload(context, BuildStage.ASSEMBLE)
            records = assembly.get("chapter_results") if assembly else None
        if records is None:
            raise ProductionAdapterError(
                "master requires assembled chapter records from the assemble stage"
            )
        cfg = _config(
            MasteringConfig,
            request.mastering_config,
            mastering_root=context.stage_root,
            source_root=_upstream_root(context, BuildStage.ASSEMBLE),
        )
        return _plain(MasteringEngine(cfg).master_chapters(records))


class PackageStageAdapter(EngineReportStageAdapter):
    def __init__(self) -> None:
        super().__init__(BuildStage.PACKAGE, (BuildStage.MASTER,))

    def _run_engine(self, request: BuildRequest, context: StageContext) -> Any:
        from app.packaging import FFmpegPackagingBackend, package_audiobook
        from app.packaging.models import BookMetadata, PackagingConfig

        chapters = request.packaging_config.get("chapters")
        if chapters is None:
            mastering = _upstream_payload(context, BuildStage.MASTER)
            chapters = mastering.get("chapter_results") if mastering else None
        metadata = request.packaging_config.get("metadata")
        if chapters is None or metadata is None:
            raise ProductionAdapterError(
                "package requires mastered chapter inputs and packaging metadata"
            )
        chapters = tuple(_coerce_packaging_chapter(chapter, context) for chapter in chapters)
        cfg = _config(
            PackagingConfig,
            request.packaging_config,
            package_root=context.stage_root,
            mastered_root=context.workspace_root / "master",
        )
        if isinstance(metadata, BookMetadata):
            book_metadata = metadata
        else:
            book_metadata = BookMetadata(
                **{k: v for k, v in metadata.items() if k in {f.name for f in fields(BookMetadata)}}
            )
        return _plain(
            package_audiobook(
                chapters,
                metadata=book_metadata,
                config=cfg,
                backend=request.packaging_config.get("backend") or FFmpegPackagingBackend(),
                cover_art=request.packaging_config.get("cover_art"),
            )
        )


def _coerce_packaging_chapter(chapter: Mapping[str, Any], context: StageContext) -> dict[str, Any]:
    """Translate a mastering result into the packaging engine's typed input."""
    if "mastered_audio_path" in chapter:
        return dict(chapter)
    audio_path = Path(str(chapter.get("output_artifact_path", "")))
    sidecar_path = Path(str(chapter.get("sidecar_path", "")))
    if not audio_path.is_absolute():
        audio_path = context.project_root / audio_path
    if not sidecar_path.is_absolute():
        sidecar_path = context.project_root / sidecar_path
    return {
        "book_id": str(chapter.get("book_id", "unknown-book")),
        "chapter_id": str(chapter["chapter_id"]),
        "chapter_order": int(chapter["chapter_order"]),
        "chapter_title": chapter.get("chapter_title"),
        "mastered_chapter_id": str(chapter["mastered_chapter_id"]),
        "source_chapter_assembly_id": str(chapter["source_chapter_assembly_id"]),
        "mastered_chapter_input_hash": str(chapter.get("mastering_input_hash", "")),
        "mastered_audio_content_hash": str(chapter.get("mastered_audio_content_hash", "")),
        "output_artifact_relative_path": str(
            chapter.get("output_artifact_relative_path", audio_path.name)
        ),
        "mastered_audio_path": audio_path,
        "mastered_sidecar_path": sidecar_path,
        "duration_seconds": float(chapter.get("output_duration_seconds", 0.0)),
        "sample_rate_hz": int(chapter.get("sample_rate_hz", 24000)),
        "channel_count": int(chapter.get("channel_count", 1)),
        "sample_width_bytes": int(chapter.get("sample_width_bytes", 2)),
        "mastering_validation_result": str(chapter.get("status", "passed")),
        "source_chapter_output_relative_path": chapter.get("source_chapter_output_relative_path"),
    }


def build_production_adapters() -> dict[BuildStage, StageAdapter]:
    """Return the complete real adapter set used by web and CLI callers."""
    return {
        BuildStage.PLAN: PlanStageAdapter(),
        BuildStage.APPLY_EDITS: ApplyEditsStageAdapter(),
        BuildStage.MANIFEST: ManifestStageAdapter(),
        BuildStage.RENDER: RenderStageAdapter(),
        BuildStage.ASSEMBLE: AssembleStageAdapter(),
        BuildStage.MASTER: MasterStageAdapter(),
        BuildStage.PACKAGE: PackageStageAdapter(),
    }


__all__ = ["ProductionAdapterError", "ProductionStageAdapter", "build_production_adapters"]
