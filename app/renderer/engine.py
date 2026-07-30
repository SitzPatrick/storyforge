from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from app.voice_planner import SynthesisManifest, RenderUnit, load_synthesis_manifest
from app.voice_planner.models import dataclass_to_dict

from .audio_validation import RenderedAudioValidationError, validate_rendered_audio
from .cache import build_render_cache_key, cache_entry_matches, load_render_sidecar
from .models import (
    ProviderRenderRequest,
    ProviderRenderResult,
    ProviderRenderSession,
    RenderContext,
    RenderFailure,
    RenderFailureType,
    RenderReport,
    RenderUnitResult,
    TTSProviderAdapter,
)

RENDERER_VERSION = "segment-renderer-1"


class SegmentRendererError(RuntimeError):
    pass


@dataclass(frozen=True)
class _UnitOutcome:
    result: RenderUnitResult
    rendered: bool = False
    cache_hit: bool = False
    skipped: bool = False
    blocked: bool = False
    failed: bool = False
    retryable_failure: bool = False
    permanent_failure: bool = False
    retryable_failures: int = 0
    permanent_failures: int = 0
    bytes_written: int = 0
    duration_seconds: float = 0.0
    provider_used: str | None = None
    warning_messages: tuple[str, ...] = ()
    error_messages: tuple[str, ...] = ()


class SegmentRenderer:
    def __init__(self, context: RenderContext) -> None:
        self.context = context

    def resolve_artifact_path(self, artifact_key: str) -> Path:
        if not artifact_key or str(artifact_key).strip() == "":
            raise ValueError("artifact key is required")
        key_path = Path(str(artifact_key))
        if key_path.is_absolute() or any(part == ".." for part in key_path.parts):
            raise ValueError(f"path traversal or absolute paths are not allowed in artifact keys: {artifact_key}")
        root = self.context.render_root.resolve(strict=False)
        candidate = root / key_path
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except Exception as exc:
            raise ValueError(f"path traversal is not allowed for artifact key: {artifact_key}") from exc
        return resolved

    def render(
        self,
        manifest_source: SynthesisManifest | Mapping[str, Any] | str | Path,
        *,
        adapters: Mapping[str, TTSProviderAdapter],
        unit_ids: list[str] | None = None,
        allow_ready_with_warnings: bool | None = None,
    ) -> RenderReport:
        manifest = manifest_source if isinstance(manifest_source, SynthesisManifest) else load_synthesis_manifest(manifest_source)
        allow_warnings = self.context.allow_ready_with_warnings if allow_ready_with_warnings is None else allow_ready_with_warnings
        if manifest.validation_report.ready_state == "blocked":
            return self._blocked_report(manifest, "manifest blocked")
        if manifest.validation_report.ready_state == "ready-with-warnings" and not allow_warnings:
            return self._blocked_report(manifest, "manifest ready-with-warnings not explicitly allowed")

        units = self._select_units(manifest.render_units, unit_ids)
        provider_sessions: dict[str, ProviderRenderSession] = {}
        unit_results: list[RenderUnitResult] = []
        provider_ids_used: set[str] = set()
        warnings: list[str] = list(manifest.validation_report.warnings)
        errors: list[str] = []

        counts = {
            "attempted": 0,
            "rendered": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "skipped": 0,
            "blocked": 0,
            "failed": 0,
            "retryable_failures": 0,
            "permanent_failures": 0,
            "bytes_written": 0,
            "duration_seconds": 0.0,
        }

        for unit in units:
            outcome = self._render_unit(manifest, unit, adapters, provider_sessions)
            unit_results.append(outcome.result)
            if outcome.provider_used or unit.assigned_provider:
                provider_ids_used.add(outcome.provider_used or unit.assigned_provider or "")
            warnings.extend(outcome.warning_messages)
            errors.extend(outcome.error_messages)

            if outcome.skipped:
                counts["skipped"] += 1
                continue
            if outcome.blocked:
                counts["blocked"] += 1
                continue
            if outcome.cache_hit:
                counts["cache_hits"] += 1
                counts["duration_seconds"] += outcome.duration_seconds
                continue
            if outcome.failed:
                counts["attempted"] += 1
                counts["cache_misses"] += 1
                counts["failed"] += 1
                counts["retryable_failures"] += outcome.retryable_failures
                counts["permanent_failures"] += outcome.permanent_failures
                continue
            if outcome.rendered:
                counts["attempted"] += 1
                counts["cache_misses"] += 1
                counts["rendered"] += 1
                counts["retryable_failures"] += outcome.retryable_failures
                counts["permanent_failures"] += outcome.permanent_failures
                counts["bytes_written"] += outcome.bytes_written
                counts["duration_seconds"] += outcome.duration_seconds
                continue

        completion_status = self._completion_status(
            manifest=manifest,
            rendered=counts["rendered"],
            cache_hits=counts["cache_hits"],
            skipped=counts["skipped"],
            blocked=counts["blocked"],
            failed=counts["failed"],
        )
        report = RenderReport(
            manifest_content_hash=manifest.manifest_content_hash,
            renderer_version=RENDERER_VERSION,
            renderer_contract_version=self.context.renderer_contract_version,
            provider_adapters_used=sorted({p for p in provider_ids_used if p}),
            total_render_units=len(units),
            attempted_units=counts["attempted"],
            successfully_rendered_units=counts["rendered"],
            cache_hits=counts["cache_hits"],
            cache_misses=counts["cache_misses"],
            skipped_units=counts["skipped"],
            blocked_units=counts["blocked"],
            failed_units=counts["failed"],
            retryable_failures=counts["retryable_failures"],
            permanent_failures=counts["permanent_failures"],
            audio_duration_seconds=counts["duration_seconds"],
            bytes_written=counts["bytes_written"],
            warnings=sorted({*warnings}),
            errors=sorted({*errors}),
            completion_status=completion_status,
            unit_results=unit_results,
        )
        self._write_report(report)
        return report

    def _render_unit(
        self,
        manifest: SynthesisManifest,
        unit: RenderUnit,
        adapters: Mapping[str, TTSProviderAdapter],
        provider_sessions: dict[str, ProviderRenderSession],
    ) -> _UnitOutcome:
        if unit.validation_status == "skipped":
            result = RenderUnitResult(
                render_unit_id=unit.render_unit_id,
                canonical_segment_id=unit.canonical_segment_id,
                provider=unit.assigned_provider,
                provider_voice_id=unit.assigned_provider_voice_id,
                status="skipped",
                skipped_reason="manifest omission",
                warnings=list(unit.warnings),
            )
            return _UnitOutcome(result=result, skipped=True, warning_messages=tuple(unit.warnings))

        if unit.validation_status == "blocked":
            reason = unit.blocked_reason or "manifest blocked"
            result = RenderUnitResult(
                render_unit_id=unit.render_unit_id,
                canonical_segment_id=unit.canonical_segment_id,
                provider=unit.assigned_provider,
                provider_voice_id=unit.assigned_provider_voice_id,
                status="blocked",
                skipped_reason=reason,
                warnings=list(unit.warnings),
                errors=[reason],
                failure_type=RenderFailureType.MANIFEST_BLOCKED.value,
                failure_message=reason,
            )
            return _UnitOutcome(result=result, blocked=True, warning_messages=tuple(unit.warnings), error_messages=(reason,))

        if not unit.assigned_provider or not unit.assigned_provider_voice_id:
            failure = RenderFailure(
                RenderFailureType.UNSUPPORTED_PROVIDER,
                f"unsupported provider for unit {unit.render_unit_id}",
                retryable=False,
                render_unit_id=unit.render_unit_id,
            )
            return _failure_outcome(unit, failure)

        adapter = adapters.get(unit.assigned_provider)
        if adapter is None:
            failure = RenderFailure(
                RenderFailureType.UNSUPPORTED_PROVIDER,
                f"unsupported provider: {unit.assigned_provider}",
                retryable=False,
                provider=unit.assigned_provider,
                render_unit_id=unit.render_unit_id,
            )
            return _failure_outcome(unit, failure)

        provider_id = unit.assigned_provider
        artifact_path = self.resolve_artifact_path(unit.output_artifact_key)
        sidecar_path = Path(str(artifact_path) + ".json")
        cache_entry = None
        try:
            cache_entry = load_render_sidecar(sidecar_path)
        except ValueError:
            warnings = (f"sidecar corruption detected for {unit.render_unit_id}",)
            return self._rerender_unit(manifest, unit, adapter, artifact_path, sidecar_path, provider_sessions, warnings=warnings, cache_entry=None)

        expected_cache_key = build_render_cache_key(
            {
                "render_unit_id": unit.render_unit_id,
                "synthesis_input_hash": unit.synthesis_input_hash,
                "renderer_contract_version": self.context.renderer_contract_version,
                "provider": unit.assigned_provider,
                "provider_voice_id": unit.assigned_provider_voice_id,
                "provider_adapter_version": adapter.adapter_version,
                "model_version": adapter.model_version,
                "output_format": self.context.output_format,
                "sample_rate_hz": self.context.sample_rate_hz,
                "channel_count": self.context.channel_count,
                "sample_width_bytes": self.context.sample_width_bytes,
                "deterministic_seed": self.context.deterministic_seed,
            }
        )
        if cache_entry and cache_entry_matches(
            cache_entry,
            render_unit_id=unit.render_unit_id,
            synthesis_input_hash=unit.synthesis_input_hash,
            renderer_contract_version=self.context.renderer_contract_version,
            provider=unit.assigned_provider,
            provider_voice_id=unit.assigned_provider_voice_id,
            provider_adapter_version=adapter.adapter_version,
            model_version=adapter.model_version,
            cache_key=expected_cache_key,
            output_format=self.context.output_format,
            sample_rate_hz=self.context.sample_rate_hz,
            channel_count=self.context.channel_count,
            sample_width_bytes=self.context.sample_width_bytes,
            artifact_path=artifact_path,
            artifact_relative_path=unit.output_artifact_key,
        ):
            try:
                validation = validate_rendered_audio(
                    artifact_path,
                    expected_sample_rate=self.context.sample_rate_hz,
                    expected_channels=self.context.channel_count,
                    expected_sample_width=self.context.sample_width_bytes,
                    maximum_duration_seconds=self.context.maximum_duration_seconds,
                )
            except RenderedAudioValidationError:
                pass
            else:
                result = RenderUnitResult(
                    render_unit_id=unit.render_unit_id,
                    canonical_segment_id=unit.canonical_segment_id,
                    provider=unit.assigned_provider,
                    provider_voice_id=unit.assigned_provider_voice_id,
                    status="cache_hit",
                    cache_hit=True,
                    render_attempts=0,
                    output_path=str(artifact_path),
                    sidecar_path=str(sidecar_path),
                    bytes_written=0,
                    duration_seconds=validation.duration_seconds,
                    audio_content_hash=validation.audio_content_hash,
                    cache_key=cache_entry.cache_key,
                    validation_result="passed",
                    warnings=list(cache_entry.warnings),
                )
                return _UnitOutcome(result=result, cache_hit=True, duration_seconds=validation.duration_seconds, provider_used=provider_id, warning_messages=tuple(cache_entry.warnings))

        warnings = tuple(cache_entry.warnings) if cache_entry else ()
        return self._rerender_unit(manifest, unit, adapter, artifact_path, sidecar_path, provider_sessions, warnings=warnings, cache_entry=cache_entry)

    def _rerender_unit(
        self,
        manifest: SynthesisManifest,
        unit: RenderUnit,
        adapter: TTSProviderAdapter,
        artifact_path: Path,
        sidecar_path: Path,
        provider_sessions: dict[str, ProviderRenderSession],
        *,
        warnings: tuple[str, ...],
        cache_entry: Any,
    ) -> _UnitOutcome:
        try:
            adapter.validate_voice(unit.assigned_provider_voice_id or "")
        except RenderFailure as failure:
            return _failure_outcome(unit, failure)

        provider_session = provider_sessions.get(unit.assigned_provider or "")
        if provider_session is None:
            provider_session = adapter.open_session()
            provider_sessions[unit.assigned_provider or ""] = provider_session

        request = self._build_request(manifest, unit, adapter)
        attempts = 0
        retryable_failures = 0
        while attempts < self.context.max_attempts:
            attempts += 1
            temp_path = self._render_temp_path(artifact_path)
            backup_path = self._backup_existing_artifact(artifact_path)
            try:
                result = provider_session.render(request, temp_path)
                validation = validate_rendered_audio(
                    temp_path,
                    expected_sample_rate=self.context.sample_rate_hz,
                    expected_channels=self.context.channel_count,
                    expected_sample_width=self.context.sample_width_bytes,
                    maximum_duration_seconds=self.context.maximum_duration_seconds,
                )
                self._atomic_replace(temp_path, artifact_path)
                sidecar = self._build_sidecar(
                    manifest=manifest,
                    unit=unit,
                    adapter=adapter,
                    request=request,
                    artifact_path=artifact_path,
                    validation=validation,
                    result=result,
                    warnings=tuple(sorted({*warnings, *result.warnings})),
                    outcome="rendered",
                )
                self._write_sidecar_atomic(sidecar_path, sidecar)
                self._cleanup_backup(backup_path)
                result_obj = RenderUnitResult(
                    render_unit_id=unit.render_unit_id,
                    canonical_segment_id=unit.canonical_segment_id,
                    provider=unit.assigned_provider,
                    provider_voice_id=unit.assigned_provider_voice_id,
                    status="rendered",
                    cache_hit=False,
                    render_attempts=attempts,
                    output_path=str(artifact_path),
                    sidecar_path=str(sidecar_path),
                    bytes_written=validation.file_size,
                    duration_seconds=validation.duration_seconds,
                    audio_content_hash=validation.audio_content_hash,
                    cache_key=sidecar["cache_key"],
                    validation_result="passed",
                    warnings=sorted({*warnings, *result.warnings}),
                )
                return _UnitOutcome(result=result_obj, rendered=True, bytes_written=validation.file_size, duration_seconds=validation.duration_seconds, provider_used=unit.assigned_provider, warning_messages=tuple(sorted({*warnings, *result.warnings})), retryable_failures=retryable_failures)
            except RenderFailure as failure:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
                self._restore_backup(backup_path, artifact_path)
                if failure.retryable and failure.failure_type in self.context.retryable_failure_types and attempts < self.context.max_attempts:
                    retryable_failures += 1
                    if self.context.retry_delay_seconds > 0:
                        time.sleep(self.context.retry_delay_seconds)
                    continue
                return _failure_outcome(
                    unit,
                    failure,
                    attempts=attempts,
                    retryable_failures=retryable_failures + (1 if failure.retryable else 0),
                    permanent_failures=0 if failure.retryable else 1,
                )
            except RenderedAudioValidationError as exc:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
                self._restore_backup(backup_path, artifact_path)
                failure = RenderFailure(
                    RenderFailureType.INVALID_GENERATED_AUDIO,
                    str(exc),
                    retryable=False,
                    provider=unit.assigned_provider,
                    provider_adapter_version=adapter.adapter_version,
                    model_version=adapter.model_version,
                    render_unit_id=unit.render_unit_id,
                )
                return _failure_outcome(unit, failure, attempts=attempts)
            except Exception as exc:
                if temp_path.exists():
                    temp_path.unlink(missing_ok=True)
                self._restore_backup(backup_path, artifact_path)
                failure = RenderFailure(
                    RenderFailureType.UNKNOWN_FAILURE,
                    str(exc),
                    retryable=False,
                    provider=unit.assigned_provider,
                    provider_adapter_version=adapter.adapter_version,
                    model_version=adapter.model_version,
                    render_unit_id=unit.render_unit_id,
                )
                return _failure_outcome(unit, failure, attempts=attempts)

        failure = RenderFailure(
            RenderFailureType.SYNTHESIS_FAILURE,
            f"render attempts exhausted for {unit.render_unit_id}",
            retryable=True,
            provider=unit.assigned_provider,
            provider_adapter_version=adapter.adapter_version,
            model_version=adapter.model_version,
            render_unit_id=unit.render_unit_id,
        )
        return _failure_outcome(
            unit,
            failure,
            attempts=attempts,
            retryable_failures=retryable_failures + 1,
            permanent_failures=0,
        )

    def _build_request(self, manifest: SynthesisManifest, unit: RenderUnit, adapter: TTSProviderAdapter) -> ProviderRenderRequest:
        return ProviderRenderRequest(
            render_unit_id=unit.render_unit_id,
            canonical_segment_id=unit.canonical_segment_id,
            synthesis_input_hash=unit.synthesis_input_hash,
            synthesis_text=unit.synthesis_text,
            provider=unit.assigned_provider or adapter.provider_id,
            provider_voice_id=unit.assigned_provider_voice_id or "",
            language=unit.language,
            controls=dict(unit.effective_renderer_controls),
            output_format=self.context.output_format,
            sample_rate_hz=self.context.sample_rate_hz,
            channel_count=self.context.channel_count,
            sample_width_bytes=self.context.sample_width_bytes,
            pronunciation_notes=unit.pronunciation_notes,
            performance_notes=unit.performance_notes,
            pace_intent=unit.pace_intent,
            pause_intent=unit.pause_intent,
            emphasis_intent=unit.emphasis_intent,
            deterministic_seed=self.context.deterministic_seed,
            manifest_content_hash=manifest.manifest_content_hash,
            renderer_contract_version=self.context.renderer_contract_version,
        )

    def _build_sidecar(
        self,
        *,
        manifest: SynthesisManifest,
        unit: RenderUnit,
        adapter: TTSProviderAdapter,
        request: ProviderRenderRequest,
        artifact_path: Path,
        validation: Any,
        result: ProviderRenderResult,
        warnings: tuple[str, ...],
        outcome: str,
    ) -> dict[str, Any]:
        root = self.context.render_root.resolve(strict=False)
        return {
            "render_unit_id": unit.render_unit_id,
            "canonical_segment_id": unit.canonical_segment_id,
            "synthesis_input_hash": unit.synthesis_input_hash,
            "renderer_contract_version": self.context.renderer_contract_version,
            "provider": unit.assigned_provider,
            "provider_voice_id": unit.assigned_provider_voice_id,
            "provider_adapter_version": result.provider_adapter_version,
            "model_version": result.model_version,
            "output_format": self.context.output_format,
            "sample_rate_hz": self.context.sample_rate_hz,
            "channel_count": self.context.channel_count,
            "sample_width_bytes": self.context.sample_width_bytes,
            "manifest_content_hash": manifest.manifest_content_hash,
            "artifact_relative_path": str(artifact_path.resolve(strict=False).relative_to(root)),
            "validation_result": "passed",
            "attempt_outcome": outcome,
            "warnings": sorted({*warnings}),
            "errors": [],
            "audio_content_hash": validation.audio_content_hash,
            "frame_count": validation.frame_count,
            "duration_seconds": validation.duration_seconds,
            "cache_key": build_render_cache_key(
                {
                    "render_unit_id": unit.render_unit_id,
                    "synthesis_input_hash": unit.synthesis_input_hash,
                    "renderer_contract_version": self.context.renderer_contract_version,
                    "provider": unit.assigned_provider,
                    "provider_voice_id": unit.assigned_provider_voice_id,
                    "provider_adapter_version": result.provider_adapter_version,
                    "model_version": result.model_version,
                    "output_format": self.context.output_format,
                    "sample_rate_hz": self.context.sample_rate_hz,
                    "channel_count": self.context.channel_count,
                    "sample_width_bytes": self.context.sample_width_bytes,
                    "deterministic_seed": self.context.deterministic_seed,
                }
            ),
        }

    def _render_temp_path(self, artifact_path: Path) -> Path:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        handle = NamedTemporaryFile("wb", dir=artifact_path.parent, prefix=f".{artifact_path.name}.", suffix=".tmp", delete=False)
        try:
            return Path(handle.name)
        finally:
            handle.close()

    def _atomic_replace(self, temp_path: Path, artifact_path: Path) -> None:
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, artifact_path)

    def _write_sidecar_atomic(self, sidecar_path: Path, payload: dict[str, Any]) -> None:
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        tmp: Path | None = None
        try:
            data = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
            with NamedTemporaryFile("w", encoding="utf-8", dir=sidecar_path.parent, prefix=f".{sidecar_path.name}.", suffix=".tmp", delete=False) as handle:
                tmp = Path(handle.name)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, sidecar_path)
        except Exception:
            if tmp is not None and tmp.exists():
                tmp.unlink(missing_ok=True)
            raise

    def _backup_existing_artifact(self, artifact_path: Path) -> Path | None:
        if not artifact_path.exists():
            return None
        with NamedTemporaryFile(
            "wb",
            dir=artifact_path.parent,
            prefix=f".{artifact_path.name}.backup.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            backup_path = Path(handle.name)
        backup_path.unlink(missing_ok=True)
        try:
            os.link(artifact_path, backup_path)
        except OSError:
            backup_path.write_bytes(artifact_path.read_bytes())
        return backup_path

    def _restore_backup(self, backup_path: Path | None, artifact_path: Path) -> None:
        if backup_path is None or not backup_path.exists():
            return
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(backup_path.read_bytes())
        backup_path.unlink(missing_ok=True)

    def _cleanup_backup(self, backup_path: Path | None) -> None:
        if backup_path is not None and backup_path.exists():
            backup_path.unlink(missing_ok=True)

    def _select_units(self, units: list[RenderUnit], unit_ids: list[str] | None) -> list[RenderUnit]:
        ordered = sorted(units, key=self._render_sort_key)
        if not unit_ids:
            return ordered
        wanted = set(unit_ids)
        return [unit for unit in ordered if unit.render_unit_id in wanted]

    def _render_sort_key(self, unit: RenderUnit) -> tuple[Any, ...]:
        return (*unit.source_order, unit.scene_id, unit.render_unit_id)

    def _blocked_report(self, manifest: SynthesisManifest, reason: str) -> RenderReport:
        result = RenderUnitResult(
            render_unit_id="manifest",
            canonical_segment_id="manifest",
            provider=None,
            provider_voice_id=None,
            status="blocked",
            skipped_reason=reason,
            warnings=list(manifest.validation_report.warnings),
            errors=[reason],
            failure_type=RenderFailureType.MANIFEST_BLOCKED.value,
            failure_message=reason,
        )
        report = RenderReport(
            manifest_content_hash=manifest.manifest_content_hash,
            renderer_version=RENDERER_VERSION,
            renderer_contract_version=self.context.renderer_contract_version,
            provider_adapters_used=[],
            total_render_units=0,
            attempted_units=0,
            successfully_rendered_units=0,
            cache_hits=0,
            cache_misses=0,
            skipped_units=0,
            blocked_units=len(manifest.render_units),
            failed_units=0,
            retryable_failures=0,
            permanent_failures=0,
            audio_duration_seconds=0.0,
            bytes_written=0,
            warnings=list(manifest.validation_report.warnings),
            errors=[reason],
            completion_status="blocked",
            unit_results=[result],
        )
        self._write_report(report)
        return report

    def _completion_status(self, *, manifest: SynthesisManifest, rendered: int, cache_hits: int, skipped: int, blocked: int, failed: int) -> str:
        if manifest.validation_report.ready_state == "blocked":
            return "blocked"
        if failed > 0:
            if rendered > 0 or cache_hits > 0 or skipped > 0 or blocked > 0:
                return "partial"
            return "failed"
        if blocked > 0:
            if rendered > 0 or cache_hits > 0:
                return "partial"
            return "blocked"
        if skipped > 0:
            return "complete-with-warnings"
        if manifest.validation_report.warnings:
            return "complete-with-warnings"
        return "complete"

    def _write_report(self, report: RenderReport) -> None:
        self.context.report_path.parent.mkdir(parents=True, exist_ok=True)
        payload = dataclass_to_dict(report)
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.context.report_path.write_text(data, encoding="utf-8")


def _failure_outcome(
    unit: RenderUnit,
    failure: RenderFailure,
    *,
    attempts: int = 0,
    retryable_failures: int = 0,
    permanent_failures: int = 0,
) -> _UnitOutcome:
    result = RenderUnitResult(
        render_unit_id=unit.render_unit_id,
        canonical_segment_id=unit.canonical_segment_id,
        provider=unit.assigned_provider,
        provider_voice_id=unit.assigned_provider_voice_id,
        status="failed",
        cache_hit=False,
        render_attempts=attempts,
        warnings=list(unit.warnings),
        errors=[failure.message],
        failure_type=failure.failure_type.value,
        failure_message=failure.message,
    )
    return _UnitOutcome(
        result=result,
        failed=True,
        retryable_failure=failure.retryable,
        permanent_failure=not failure.retryable,
        retryable_failures=retryable_failures,
        permanent_failures=permanent_failures,
        warning_messages=tuple(unit.warnings),
        error_messages=(failure.message,),
    )


def render_manifest(
    manifest_source: SynthesisManifest | Mapping[str, Any] | str | Path,
    *,
    context: RenderContext,
    adapters: Mapping[str, TTSProviderAdapter],
    unit_ids: list[str] | None = None,
    allow_ready_with_warnings: bool | None = None,
) -> RenderReport:
    return SegmentRenderer(context).render(
        manifest_source,
        adapters=adapters,
        unit_ids=unit_ids,
        allow_ready_with_warnings=allow_ready_with_warnings,
    )
