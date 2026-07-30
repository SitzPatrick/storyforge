from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.assembler.cache import CHAPTER_SIDECAR_FILENAME, ChapterSidecarError, load_chapter_sidecar
from app.voice_planner.models import dataclass_to_dict
from app.voice_planner.schema import canonical_json_dumps

from .cache import (
    MASTERING_SIDECAR_FILENAME,
    build_mastered_chapter_id,
    build_mastering_input_hash,
    load_mastering_sidecar,
    mastering_cache_entry_matches,
    save_mastering_sidecar,
)
from .measure import MasteringMeasurementError, measure_audio, silence_frame_count
from .models import (
    AudioMeasurements,
    MasteringConfig,
    MasteringFailure,
    MasteringFailureType,
    MasteringReport,
    MasteringResult,
    MasteringSidecar,
)
from .process import MasteringProcessingError, process_mastered_audio, validate_mastered_audio


class MasteringEngineError(RuntimeError):
    pass


def _stringify(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_stringify(item) for item in value]
    if isinstance(value, list):
        return [_stringify(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _stringify(item) for key, item in value.items()}
    return value


def _safe_identifier(value: str) -> str:
    if not value or value.strip() != value:
        raise MasteringEngineError(f"unsafe output path component: {value!r}")
    if any(part in value for part in ("..", "/", "\\")):
        raise MasteringEngineError(f"unsafe output path component: {value!r}")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not set(value) <= allowed:
        raise MasteringEngineError(f"unsafe output path component: {value!r}")
    return value


def _source_paths(config: MasteringConfig, record: Mapping[str, Any]) -> tuple[Path, Path]:
    relative_path = str(record["output_artifact_relative_path"])
    source_audio_path = config.source_root / relative_path
    source_sidecar_path = source_audio_path.parent / CHAPTER_SIDECAR_FILENAME
    return source_audio_path, source_sidecar_path


def _mastered_paths(config: MasteringConfig, *, book_id: str, record: Mapping[str, Any]) -> tuple[str, Path, Path, str]:
    chapter_id = _safe_identifier(str(record["chapter_id"]))
    chapter_assembly_id = str(record["chapter_assembly_id"])
    mastered_chapter_id = build_mastered_chapter_id(
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_assembly_id=chapter_assembly_id,
        mastering_contract_version=config.mastering_contract_version,
    )
    relative_path = f"mastered/{chapter_id}/{mastered_chapter_id}.wav"
    output_path = config.mastering_root / relative_path
    sidecar_path = output_path.with_name(MASTERING_SIDECAR_FILENAME)
    return mastered_chapter_id, output_path, sidecar_path, relative_path


def _book_id_from_record(record: Mapping[str, Any]) -> str:
    value = record.get("book_id")
    if not value:
        raise MasteringEngineError("book_id missing from chapter record")
    return str(value)


def _validate_source_chapter(
    record: Mapping[str, Any],
    config: MasteringConfig,
    source_audio_path: Path,
    source_sidecar_path: Path,
) -> tuple[AudioMeasurements | None, Any | None, list[str], MasteringFailure | None]:
    if not source_audio_path.exists():
        failure = MasteringFailure(MasteringFailureType.SOURCE_CHAPTER_MISSING, f"source chapter missing: {source_audio_path}", chapter_id=str(record.get("chapter_id")))
        return None, None, [], failure
    if not source_sidecar_path.exists():
        failure = MasteringFailure(MasteringFailureType.SOURCE_SIDECAR_MISSING, f"source sidecar missing: {source_sidecar_path}", chapter_id=str(record.get("chapter_id")))
        return None, None, [], failure
    try:
        source_sidecar = load_chapter_sidecar(source_sidecar_path)
    except ChapterSidecarError as exc:
        failure = MasteringFailure(MasteringFailureType.SOURCE_SIDECAR_CORRUPT, str(exc), chapter_id=str(record.get("chapter_id")))
        return None, None, [], failure

    record_output_relative_path = str(record["output_artifact_relative_path"])
    if source_sidecar.chapter_id != str(record["chapter_id"]):
        return None, source_sidecar, [], MasteringFailure(MasteringFailureType.SOURCE_HASH_MISMATCH, "chapter id mismatch", chapter_id=str(record.get("chapter_id")))
    if source_sidecar.chapter_assembly_id != str(record["chapter_assembly_id"]):
        return None, source_sidecar, [], MasteringFailure(MasteringFailureType.SOURCE_HASH_MISMATCH, "chapter assembly id mismatch", chapter_id=str(record.get("chapter_id")))
    if source_sidecar.chapter_input_hash != str(record["chapter_input_hash"]):
        return None, source_sidecar, [], MasteringFailure(MasteringFailureType.SOURCE_HASH_MISMATCH, "chapter input hash mismatch", chapter_id=str(record.get("chapter_id")))
    if source_sidecar.output_artifact_relative_path != record_output_relative_path:
        return None, source_sidecar, [], MasteringFailure(MasteringFailureType.SOURCE_HASH_MISMATCH, "output artifact relative path mismatch", chapter_id=str(record.get("chapter_id")))
    try:
        current_measurements = measure_audio(
            source_audio_path,
            target_sample_rate_hz=config.sample_rate_hz,
            target_channel_count=config.channel_count,
            target_sample_width_bytes=config.sample_width_bytes,
            silence_detection_threshold_dbfs=config.silence_detection_threshold_dbfs,
        )
    except Exception as exc:  # noqa: BLE001
        failure = MasteringFailure(MasteringFailureType.INVALID_SOURCE_AUDIO, str(exc), chapter_id=str(record.get("chapter_id")))
        return None, source_sidecar, [], failure
    if current_measurements.audio_content_hash != source_sidecar.audio_content_hash:
        failure = MasteringFailure(MasteringFailureType.SOURCE_HASH_MISMATCH, "source audio hash mismatch", chapter_id=str(record.get("chapter_id")))
        return None, source_sidecar, [], failure
    if source_sidecar.validation_result != "passed":
        failure = MasteringFailure(MasteringFailureType.INVALID_SOURCE_AUDIO, "source chapter validation did not pass", chapter_id=str(record.get("chapter_id")))
        return None, source_sidecar, [], failure
    warnings: list[str] = []
    if source_sidecar.sample_rate_hz != config.sample_rate_hz:
        return None, source_sidecar, [], MasteringFailure(MasteringFailureType.UNSUPPORTED_SOURCE_FORMAT, "unexpected source sample rate", chapter_id=str(record.get("chapter_id")))
    if source_sidecar.channel_count != config.channel_count:
        return None, source_sidecar, [], MasteringFailure(MasteringFailureType.UNSUPPORTED_SOURCE_FORMAT, "unexpected source channel count", chapter_id=str(record.get("chapter_id")))
    if source_sidecar.sample_width_bytes != config.sample_width_bytes:
        return None, source_sidecar, [], MasteringFailure(MasteringFailureType.UNSUPPORTED_SOURCE_FORMAT, "unexpected source sample width", chapter_id=str(record.get("chapter_id")))
    return current_measurements, source_sidecar, warnings, None


def _cache_hit_result(
    *,
    sidecar: MasteringSidecar,
    output_path: Path,
    config: MasteringConfig,
    mastered_chapter_id: str,
    expected_mastering_input_hash: str,
) -> MasteringResult | None:
    if not output_path.exists():
        return None
    try:
        output_measurements, _ = validate_mastered_audio(output_path, config)
    except Exception:
        return None
    if output_measurements.audio_content_hash != sidecar.mastered_audio_content_hash:
        return None
    if not mastering_cache_entry_matches(
        sidecar,
        expected_mastered_chapter_id=mastered_chapter_id,
        expected_mastering_input_hash=expected_mastering_input_hash,
        expected_output_artifact_relative_path=sidecar.output_artifact_relative_path,
        expected_chapter_id=sidecar.chapter_id,
        expected_chapter_assembly_id=sidecar.source_chapter_assembly_id,
        expected_source_chapter_audio_content_hash=sidecar.source_chapter_audio_content_hash,
        expected_output_format=config.output_format,
        expected_sample_rate_hz=config.sample_rate_hz,
        expected_channel_count=config.channel_count,
        expected_sample_width_bytes=config.sample_width_bytes,
        expected_mastering_contract_version=config.mastering_contract_version,
        expected_mastering_processor_version=config.processor_version,
        expected_backend_name=config.backend_name,
        expected_backend_version=config.backend_version,
    ):
        return None
    return MasteringResult(
        chapter_id=sidecar.chapter_id,
        chapter_order=sidecar.chapter_order,
        chapter_title=sidecar.chapter_title,
        book_id=sidecar.book_id,
        source_chapter_assembly_id=sidecar.source_chapter_assembly_id,
        mastered_chapter_id=sidecar.mastered_chapter_id,
        mastering_input_hash=sidecar.mastering_input_hash,
        output_artifact_relative_path=sidecar.output_artifact_relative_path,
        output_artifact_path=str(output_path),
        sidecar_path=str(output_path.with_name(MASTERING_SIDECAR_FILENAME)),
        status=sidecar.validation_result,
        cache_hit=True,
        warnings=sidecar.warnings,
        errors=sidecar.errors,
        bytes_written=0,
        input_frame_count=sidecar.input_frame_count,
        output_frame_count=sidecar.output_frame_count,
        input_duration_seconds=sidecar.input_duration_seconds,
        output_duration_seconds=sidecar.output_duration_seconds,
        input_integrated_loudness_dbfs=sidecar.input_integrated_loudness_dbfs,
        output_integrated_loudness_dbfs=sidecar.output_integrated_loudness_dbfs,
        input_sample_peak_dbfs=sidecar.input_sample_peak_dbfs,
        output_sample_peak_dbfs=sidecar.output_sample_peak_dbfs,
        requested_gain_db=sidecar.requested_gain_db,
        applied_gain_db=sidecar.applied_gain_db,
        gain_constrained=sidecar.gain_constrained,
        limiter_activated=sidecar.limiter_activated,
        limiter_amount_db=sidecar.limiter_amount_db,
        original_leading_silence_frames=sidecar.original_leading_silence_frames,
        original_trailing_silence_frames=sidecar.original_trailing_silence_frames,
        trimmed_leading_silence_frames=sidecar.trimmed_leading_silence_frames,
        trimmed_trailing_silence_frames=sidecar.trimmed_trailing_silence_frames,
        final_leading_silence_frames=sidecar.final_leading_silence_frames,
        final_trailing_silence_frames=sidecar.final_trailing_silence_frames,
        fade_in_frames=sidecar.fade_in_frames,
        fade_out_frames=sidecar.fade_out_frames,
        mastered_audio_content_hash=output_measurements.audio_content_hash,
    )


def _write_atomic_pair(output_path: Path, sidecar_path: Path, audio_bytes: bytes, sidecar_payload: Mapping[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    backup_audio = output_path.read_bytes() if output_path.exists() else None
    backup_sidecar = sidecar_path.read_text(encoding="utf-8") if sidecar_path.exists() else None
    temp_audio_fd, temp_audio_name = tempfile.mkstemp(suffix=".wav", dir=str(output_path.parent))
    os.close(temp_audio_fd)
    temp_audio_path = Path(temp_audio_name)
    temp_sidecar_fd, temp_sidecar_name = tempfile.mkstemp(suffix=".json", dir=str(sidecar_path.parent))
    os.close(temp_sidecar_fd)
    temp_sidecar_path = Path(temp_sidecar_name)
    try:
        temp_audio_path.write_bytes(audio_bytes)
        os.replace(temp_audio_path, output_path)
        save_mastering_sidecar(temp_sidecar_path, sidecar_payload)
        os.replace(temp_sidecar_path, sidecar_path)
    except Exception:
        if backup_audio is not None:
            output_path.write_bytes(backup_audio)
        else:
            if output_path.exists():
                output_path.unlink()
        if backup_sidecar is not None:
            sidecar_path.write_text(backup_sidecar, encoding="utf-8")
        else:
            if sidecar_path.exists():
                sidecar_path.unlink()
        if temp_audio_path.exists():
            temp_audio_path.unlink()
        if temp_sidecar_path.exists():
            temp_sidecar_path.unlink()
        raise
    finally:
        if temp_audio_path.exists():
            temp_audio_path.unlink()
        if temp_sidecar_path.exists():
            temp_sidecar_path.unlink()


@dataclass(frozen=True)
class MasteringEngine:
    config: MasteringConfig

    def master_chapters(self, chapter_records: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...]) -> MasteringReport:
        ordered_records = sorted((dict(record) for record in chapter_records), key=lambda item: (int(item["chapter_order"]), str(item["chapter_id"])))
        results: list[MasteringResult] = []
        warnings: list[str] = []
        errors: list[str] = []
        total_input_duration_seconds = 0.0
        total_output_duration_seconds = 0.0
        total_frames_trimmed = 0
        bytes_written = 0
        cache_hit_chapters = 0
        newly_processed_chapters = 0
        blocked_chapters = 0
        warning_chapters = 0
        failed_chapters = 0
        chapters_constrained_by_peak_headroom = 0
        chapters_using_limiting = 0

        for record in ordered_records:
            chapter_id = str(record["chapter_id"])
            source_audio_path, source_sidecar_path = _source_paths(self.config, record)
            source_measurements, source_sidecar, source_warnings, failure = _validate_source_chapter(record, self.config, source_audio_path, source_sidecar_path)
            if failure is not None:
                blocked_chapters += 1
                errors.append(failure.message)
                result = MasteringResult(
                    chapter_id=chapter_id,
                    chapter_order=int(record["chapter_order"]),
                    chapter_title=record.get("chapter_title"),
                    book_id=source_sidecar.book_id if source_sidecar is not None and getattr(source_sidecar, "book_id", None) else str(record.get("book_id", "")),
                    source_chapter_assembly_id=str(record["chapter_assembly_id"]),
                    mastered_chapter_id="",
                    mastering_input_hash="",
                    output_artifact_relative_path="",
                    output_artifact_path="",
                    sidecar_path="",
                    status="blocked",
                    cache_hit=False,
                    failure=failure,
                    errors=(failure.message,),
                )
                results.append(result)
                continue

            assert source_measurements is not None
            assert source_sidecar is not None
            book_id = source_sidecar.book_id
            mastered_chapter_id, output_path, output_sidecar_path, output_relative_path = _mastered_paths(self.config, book_id=book_id, record=record)
            mastering_input_hash = build_mastering_input_hash(
                mastering_contract_version=self.config.mastering_contract_version,
                processor_version=self.config.processor_version,
                backend_name=self.config.backend_name,
                backend_version=self.config.backend_version,
                book_id=book_id,
                chapter_id=chapter_id,
                chapter_order=int(record["chapter_order"]),
                chapter_assembly_id=str(record["chapter_assembly_id"]),
                source_chapter_input_hash=str(record["chapter_input_hash"]),
                source_chapter_audio_content_hash=source_measurements.audio_content_hash,
                target_integrated_loudness_dbfs=self.config.target_integrated_loudness_dbfs,
                max_gain_increase_db=self.config.max_gain_increase_db,
                max_gain_reduction_db=self.config.max_gain_reduction_db,
                max_sample_peak_dbfs=self.config.max_sample_peak_dbfs,
                trim_leading_silence_enabled=self.config.trim_leading_silence_enabled,
                trim_trailing_silence_enabled=self.config.trim_trailing_silence_enabled,
                leading_silence_target_ms=self.config.leading_silence_target_ms,
                trailing_silence_target_ms=self.config.trailing_silence_target_ms,
                silence_detection_threshold_dbfs=self.config.silence_detection_threshold_dbfs,
                minimum_silence_duration_ms=self.config.minimum_silence_duration_ms,
                fade_in_ms=self.config.fade_in_ms,
                fade_out_ms=self.config.fade_out_ms,
                limiter_enabled=self.config.limiter_enabled,
                limiter_ceiling_dbfs=self.config.limiter_ceiling_dbfs,
                output_format=self.config.output_format,
                sample_rate_hz=self.config.sample_rate_hz,
                channel_count=self.config.channel_count,
                sample_width_bytes=self.config.sample_width_bytes,
                source_chapter_assembler_version=self.config.source_assembler_version,
                source_chapter_audio_format="wav",
                source_chapter_sample_rate_hz=source_measurements.sample_rate_hz,
                source_chapter_channel_count=source_measurements.channel_count,
                source_chapter_sample_width_bytes=source_measurements.sample_width_bytes,
            )
            if output_path.exists() and output_sidecar_path.exists():
                try:
                    cached_sidecar = load_mastering_sidecar(output_sidecar_path)
                except Exception:
                    cached_sidecar = None
                else:
                    cache_result = _cache_hit_result(
                        sidecar=cached_sidecar,
                        output_path=output_path,
                        config=self.config,
                        mastered_chapter_id=mastered_chapter_id,
                        expected_mastering_input_hash=mastering_input_hash,
                    )
                    if cache_result is not None:
                        results.append(cache_result)
                        cache_hit_chapters += 1
                        total_input_duration_seconds += cache_result.input_duration_seconds
                        total_output_duration_seconds += cache_result.output_duration_seconds
                        total_frames_trimmed += cache_result.trimmed_leading_silence_frames + cache_result.trimmed_trailing_silence_frames
                        continue

            try:
                process_result = process_mastered_audio(source_audio_path, self.config)
            except MasteringProcessingError as exc:
                failed_chapters += 1
                failure = MasteringFailure(MasteringFailureType.PROCESSING_FAILURE, str(exc), chapter_id=chapter_id)
                result = MasteringResult(
                    chapter_id=chapter_id,
                    chapter_order=int(record["chapter_order"]),
                    chapter_title=record.get("chapter_title"),
                    book_id=book_id,
                    source_chapter_assembly_id=str(record["chapter_assembly_id"]),
                    mastered_chapter_id=mastered_chapter_id,
                    mastering_input_hash=mastering_input_hash,
                    output_artifact_relative_path=output_relative_path,
                    output_artifact_path=str(output_path),
                    sidecar_path=str(output_sidecar_path),
                    status="failed",
                    cache_hit=False,
                    failure=failure,
                    errors=(str(exc),),
                )
                results.append(result)
                errors.append(str(exc))
                continue

            # Validation runs against the temp file before final placement.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            temp_audio_path = output_path.with_suffix(".validate.wav")
            temp_audio_path.write_bytes(process_result.audio_bytes)
            try:
                mastered_audio_measurements, validation_warnings = validate_mastered_audio(temp_audio_path, self.config)
            finally:
                if temp_audio_path.exists():
                    temp_audio_path.unlink()
            final_warnings = tuple(process_result.warnings + validation_warnings)
            status = "passed-with-warnings" if final_warnings else "passed"
            if process_result.gain_constrained:
                chapters_constrained_by_peak_headroom += 1
            if process_result.limiter_activated:
                chapters_using_limiting += 1
            if final_warnings:
                warning_chapters += 1
                warnings.extend(final_warnings)

            sidecar_payload = _build_mastering_sidecar_payload(
                record=record,
                book_id=book_id,
                config=self.config,
                mastered_chapter_id=mastered_chapter_id,
                mastering_input_hash=mastering_input_hash,
                output_relative_path=output_relative_path,
                source_measurements=source_measurements,
                mastered_measurements=mastered_audio_measurements,
                process_result=process_result,
            )
            try:
                _write_atomic_pair(output_path, output_sidecar_path, process_result.audio_bytes, sidecar_payload)
            except Exception as exc:
                failed_chapters += 1
                failure = MasteringFailure(MasteringFailureType.SIDECAR_WRITE_FAILURE, str(exc), chapter_id=chapter_id, mastered_chapter_id=mastered_chapter_id)
                result = MasteringResult(
                    chapter_id=chapter_id,
                    chapter_order=int(record["chapter_order"]),
                    chapter_title=record.get("chapter_title"),
                    book_id=book_id,
                    source_chapter_assembly_id=str(record["chapter_assembly_id"]),
                    mastered_chapter_id=mastered_chapter_id,
                    mastering_input_hash=mastering_input_hash,
                    output_artifact_relative_path=output_relative_path,
                    output_artifact_path=str(output_path),
                    sidecar_path=str(output_sidecar_path),
                    status="failed",
                    cache_hit=False,
                    failure=failure,
                    errors=(str(exc),),
                )
                results.append(result)
                errors.append(str(exc))
                continue

            bytes_written += len(process_result.audio_bytes)
            newly_processed_chapters += 1
            total_input_duration_seconds += source_measurements.duration_seconds
            total_output_duration_seconds += mastered_audio_measurements.duration_seconds
            total_frames_trimmed += process_result.trimmed_leading_silence_frames + process_result.trimmed_trailing_silence_frames
            result = MasteringResult(
                chapter_id=chapter_id,
                chapter_order=int(record["chapter_order"]),
                chapter_title=record.get("chapter_title"),
                book_id=book_id,
                source_chapter_assembly_id=str(record["chapter_assembly_id"]),
                mastered_chapter_id=mastered_chapter_id,
                mastering_input_hash=mastering_input_hash,
                output_artifact_relative_path=output_relative_path,
                output_artifact_path=str(output_path),
                sidecar_path=str(output_sidecar_path),
                status=status,
                cache_hit=False,
                warnings=final_warnings,
                errors=(),
                bytes_written=len(process_result.audio_bytes),
                input_frame_count=source_measurements.frame_count,
                output_frame_count=mastered_audio_measurements.frame_count,
                input_duration_seconds=source_measurements.duration_seconds,
                output_duration_seconds=mastered_audio_measurements.duration_seconds,
                input_integrated_loudness_dbfs=source_measurements.integrated_loudness_dbfs,
                output_integrated_loudness_dbfs=mastered_audio_measurements.integrated_loudness_dbfs,
                input_sample_peak_dbfs=source_measurements.sample_peak_dbfs,
                output_sample_peak_dbfs=mastered_audio_measurements.sample_peak_dbfs,
                requested_gain_db=process_result.requested_gain_db,
                applied_gain_db=process_result.applied_gain_db,
                gain_constrained=process_result.gain_constrained,
                limiter_activated=process_result.limiter_activated,
                limiter_amount_db=process_result.limiter_amount_db,
                original_leading_silence_frames=process_result.original_leading_silence_frames,
                original_trailing_silence_frames=process_result.original_trailing_silence_frames,
                trimmed_leading_silence_frames=process_result.trimmed_leading_silence_frames,
                trimmed_trailing_silence_frames=process_result.trimmed_trailing_silence_frames,
                final_leading_silence_frames=process_result.final_leading_silence_frames,
                final_trailing_silence_frames=process_result.final_trailing_silence_frames,
                fade_in_frames=process_result.fade_in_frames,
                fade_out_frames=process_result.fade_out_frames,
                mastered_audio_content_hash=mastered_audio_measurements.audio_content_hash,
            )
            results.append(result)

        completed_chapters = sum(1 for item in results if item.status in {"passed", "passed-with-warnings"} or item.cache_hit)
        mastered_chapters = completed_chapters
        completion_status = "complete"
        if failed_chapters:
            completion_status = "failed"
        elif blocked_chapters:
            completion_status = "blocked" if not completed_chapters else "partial"
        elif warning_chapters:
            completion_status = "complete-with-warnings"
        book_id = next((result.book_id for result in results if result.book_id), "")
        return MasteringReport(
            book_id=book_id,
            mastering_contract_version=self.config.mastering_contract_version,
            processor_version=self.config.processor_version,
            backend_name=self.config.backend_name,
            backend_version=self.config.backend_version,
            total_chapters=len(ordered_records),
            mastered_chapters=mastered_chapters,
            cache_hit_chapters=cache_hit_chapters,
            newly_processed_chapters=newly_processed_chapters,
            blocked_chapters=blocked_chapters,
            warning_chapters=warning_chapters,
            failed_chapters=failed_chapters,
            total_input_duration_seconds=round(total_input_duration_seconds, 6),
            total_output_duration_seconds=round(total_output_duration_seconds, 6),
            total_frames_trimmed=total_frames_trimmed,
            aggregate_loudness_statistics={
                "input_mean_dbfs": round(sum((r.input_integrated_loudness_dbfs for r in results if r.input_frame_count), 0.0) / max(1, sum(1 for r in results if r.input_frame_count)), 6),
                "output_mean_dbfs": round(sum((r.output_integrated_loudness_dbfs for r in results if r.output_frame_count), 0.0) / max(1, sum(1 for r in results if r.output_frame_count)), 6),
            },
            aggregate_peak_statistics={
                "input_max_dbfs": max((r.input_sample_peak_dbfs for r in results if r.input_frame_count), default=float("-inf")),
                "output_max_dbfs": max((r.output_sample_peak_dbfs for r in results if r.output_frame_count), default=float("-inf")),
            },
            chapters_constrained_by_peak_headroom=chapters_constrained_by_peak_headroom,
            chapters_using_limiting=chapters_using_limiting,
            bytes_written=bytes_written,
            warnings=tuple(warnings),
            errors=tuple(errors),
            completion_status=completion_status,
            chapter_results=tuple(results),
        )


def _build_mastering_sidecar_payload(
    *,
    record: Mapping[str, Any],
    book_id: str,
    config: MasteringConfig,
    mastered_chapter_id: str,
    mastering_input_hash: str,
    output_relative_path: str,
    source_measurements: AudioMeasurements,
    mastered_measurements: AudioMeasurements,
    process_result,
) -> dict[str, Any]:
    return {
        "mastered_chapter_id": mastered_chapter_id,
        "chapter_id": str(record["chapter_id"]),
        "chapter_order": int(record["chapter_order"]),
        "chapter_title": record.get("chapter_title"),
        "book_id": book_id,
        "source_chapter_assembly_id": str(record["chapter_assembly_id"]),
        "source_chapter_input_hash": str(record["chapter_input_hash"]),
        "source_chapter_audio_content_hash": source_measurements.audio_content_hash,
        "mastering_contract_version": config.mastering_contract_version,
        "mastering_processor_version": config.processor_version,
        "processing_backend": config.backend_name,
        "processing_backend_version": config.backend_version,
        "mastering_input_hash": mastering_input_hash,
        "output_artifact_relative_path": output_relative_path,
        "output_format": config.output_format,
        "sample_rate_hz": config.sample_rate_hz,
        "channel_count": config.channel_count,
        "sample_width_bytes": config.sample_width_bytes,
        "input_frame_count": source_measurements.frame_count,
        "output_frame_count": mastered_measurements.frame_count,
        "input_duration_seconds": round(source_measurements.duration_seconds, 6),
        "output_duration_seconds": round(mastered_measurements.duration_seconds, 6),
        "input_integrated_loudness_dbfs": source_measurements.integrated_loudness_dbfs,
        "output_integrated_loudness_dbfs": mastered_measurements.integrated_loudness_dbfs,
        "input_sample_peak_dbfs": source_measurements.sample_peak_dbfs,
        "output_sample_peak_dbfs": mastered_measurements.sample_peak_dbfs,
        "true_peak_dbfs": None,
        "requested_gain_db": process_result.requested_gain_db,
        "applied_gain_db": process_result.applied_gain_db,
        "gain_constrained": process_result.gain_constrained,
        "limiter_activated": process_result.limiter_activated,
        "limiter_amount_db": process_result.limiter_amount_db,
        "original_leading_silence_frames": process_result.original_leading_silence_frames,
        "original_trailing_silence_frames": process_result.original_trailing_silence_frames,
        "trimmed_leading_silence_frames": process_result.trimmed_leading_silence_frames,
        "trimmed_trailing_silence_frames": process_result.trimmed_trailing_silence_frames,
        "final_leading_silence_frames": process_result.final_leading_silence_frames,
        "final_trailing_silence_frames": process_result.final_trailing_silence_frames,
        "fade_in_frames": process_result.fade_in_frames,
        "fade_out_frames": process_result.fade_out_frames,
        "mastered_audio_content_hash": mastered_measurements.audio_content_hash,
        "validation_result": "passed-with-warnings" if process_result.warnings else "passed",
        "warnings": list(process_result.warnings),
        "errors": [],
        "source_chapter_output_relative_path": str(record["output_artifact_relative_path"]),
        "source_chapter_source": _stringify(dict(record)),
    }


def master_chapters(chapter_records: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...], *, config: MasteringConfig) -> MasteringReport:
    return MasteringEngine(config=config).master_chapters(chapter_records)
