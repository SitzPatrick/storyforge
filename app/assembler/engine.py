from __future__ import annotations

import copy
import json
import os
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

from app.renderer.cache import build_render_cache_key, load_render_sidecar
from app.renderer.audio_validation import validate_rendered_audio
from app.voice_planner import SynthesisManifest, canonical_json_dumps, load_synthesis_manifest
from app.voice_planner.models import dataclass_to_dict

from .audio import WavInspectionError, generate_silence_bytes, inspect_wav_file, silence_frame_count
from .cache import (
    ChapterSidecarError,
    build_chapter_assembly_id,
    build_chapter_input_hash,
    chapter_cache_entry_matches,
    chapter_sidecar_payload,
    load_chapter_sidecar,
    save_chapter_sidecar_payload,
)
from .models import (
    AssemblyFailure,
    AssemblyFailureType,
    ChapterAssemblyConfig,
    ChapterAssemblyReport,
    ChapterAssemblyResult,
    ChapterGroup,
    ChapterSegmentStatus,
    ChapterSidecar,
    ChapterSpacingConfig,
)

CHAPTER_ASSEMBLER_VERSION = "milestone-12"


class ChapterAssemblyError(RuntimeError):
    def __init__(self, failure: AssemblyFailure):
        super().__init__(failure.message)
        self.failure = failure


class ChapterAssembler:
    def __init__(
        self,
        manifest_source: SynthesisManifest | Mapping[str, Any] | str | Path,
        *,
        chapter_structure_source: Mapping[str, Any] | str | Path | None = None,
        config: ChapterAssemblyConfig,
    ) -> None:
        self.config = config
        self.manifest = self._load_manifest(manifest_source)
        self.chapter_structure = self._load_optional_source(chapter_structure_source)
        self._segment_root = Path(config.segment_root)
        self._assembly_root = Path(config.assembly_root)
        self._chapter_groups = self._build_chapter_groups()

    @property
    def chapter_groups(self) -> tuple[ChapterGroup, ...]:
        return self._chapter_groups

    def assemble(self) -> ChapterAssemblyReport:
        manifest = self.manifest
        if manifest.validation_report.ready_state != "ready":
            report = ChapterAssemblyReport(
                book_id=manifest.book_id,
                manifest_content_hash=manifest.manifest_content_hash,
                assembler_version=self.config.assembler_version,
                assembly_contract_version=self.config.assembly_contract_version,
                total_chapters=len(self._chapter_groups),
                completed_chapters=0,
                cache_hit_chapters=0,
                newly_assembled_chapters=0,
                blocked_chapters=len(self._chapter_groups),
                partial_chapters=0,
                failed_chapters=0,
                total_source_render_units=len(manifest.render_units),
                assembled_render_units=0,
                blocked_units=sum(
                    1 for unit in manifest.render_units if unit.validation_status == "blocked"
                ),
                omitted_units=sum(
                    1 for unit in manifest.render_units if unit.validation_status == "skipped"
                ),
                missing_artifacts=0,
                invalid_artifacts=0,
                total_speech_duration_seconds=0.0,
                total_inserted_silence_duration_seconds=0.0,
                total_chapter_duration_seconds=0.0,
                bytes_written=0,
                warnings=tuple(manifest.validation_report.warnings),
                errors=tuple(manifest.validation_report.errors),
                completion_status="blocked",
                chapter_results=(),
            )
            self._write_report_atomic(report)
            return report

        chapter_results: list[ChapterAssemblyResult] = []
        warnings: list[str] = []
        errors: list[str] = []
        completed_chapters = 0
        cache_hit_chapters = 0
        newly_assembled_chapters = 0
        blocked_chapters = 0
        partial_chapters = 0
        failed_chapters = 0
        assembled_render_units = 0
        blocked_units = 0
        omitted_units = 0
        missing_artifacts = 0
        invalid_artifacts = 0
        total_speech_duration_seconds = 0.0
        total_inserted_silence_duration_seconds = 0.0
        total_chapter_duration_seconds = 0.0
        bytes_written = 0

        for group in self._chapter_groups:
            result = self._assemble_chapter(group)
            chapter_results.append(result)
            warnings.extend(result.warnings)
            errors.extend(result.errors)
            if result.status == "cache-hit":
                completed_chapters += 1
                cache_hit_chapters += 1
                continue
            if result.status == "assembled":
                completed_chapters += 1
                newly_assembled_chapters += 1
                assembled_render_units += len(result.render_unit_ids)
                total_speech_duration_seconds += (
                    result.speech_frame_count / self.config.sample_rate_hz
                )
                total_inserted_silence_duration_seconds += (
                    result.silence_frame_count / self.config.sample_rate_hz
                )
                total_chapter_duration_seconds += result.frame_count / self.config.sample_rate_hz
                bytes_written += result.bytes_written
                continue
            if result.status == "blocked":
                blocked_chapters += 1
            elif result.status == "partial":
                partial_chapters += 1
                completed_chapters += 1
                newly_assembled_chapters += 1
                assembled_render_units += len(result.render_unit_ids)
                total_speech_duration_seconds += (
                    result.speech_frame_count / self.config.sample_rate_hz
                )
                total_inserted_silence_duration_seconds += (
                    result.silence_frame_count / self.config.sample_rate_hz
                )
                total_chapter_duration_seconds += result.frame_count / self.config.sample_rate_hz
                bytes_written += result.bytes_written
            else:
                failed_chapters += 1

            blocked_units += len(result.blocked_unit_ids)
            omitted_units += len(result.omitted_unit_ids)
            missing_artifacts += sum(
                1
                for status in result.segment_statuses
                if status.failure
                and status.failure.failure_type
                in {
                    AssemblyFailureType.SEGMENT_MISSING,
                    AssemblyFailureType.SEGMENT_SIDECAR_MISSING,
                }
            )
            invalid_artifacts += sum(
                1
                for status in result.segment_statuses
                if status.failure
                and status.failure.failure_type
                not in {
                    AssemblyFailureType.SEGMENT_MISSING,
                    AssemblyFailureType.SEGMENT_SIDECAR_MISSING,
                }
            )

        completion_status = self._completion_status(
            completed_chapters, blocked_chapters, partial_chapters, failed_chapters, warnings
        )
        report = ChapterAssemblyReport(
            book_id=manifest.book_id,
            manifest_content_hash=manifest.manifest_content_hash,
            assembler_version=self.config.assembler_version,
            assembly_contract_version=self.config.assembly_contract_version,
            total_chapters=len(self._chapter_groups),
            completed_chapters=completed_chapters,
            cache_hit_chapters=cache_hit_chapters,
            newly_assembled_chapters=newly_assembled_chapters,
            blocked_chapters=blocked_chapters,
            partial_chapters=partial_chapters,
            failed_chapters=failed_chapters,
            total_source_render_units=len(manifest.render_units),
            assembled_render_units=assembled_render_units,
            blocked_units=blocked_units,
            omitted_units=omitted_units,
            missing_artifacts=missing_artifacts,
            invalid_artifacts=invalid_artifacts,
            total_speech_duration_seconds=round(total_speech_duration_seconds, 6),
            total_inserted_silence_duration_seconds=round(
                total_inserted_silence_duration_seconds, 6
            ),
            total_chapter_duration_seconds=round(total_chapter_duration_seconds, 6),
            bytes_written=bytes_written,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(dict.fromkeys(errors)),
            completion_status=completion_status,
            chapter_results=tuple(chapter_results),
        )
        self._write_report_atomic(report)
        return report

    def _assemble_chapter(self, group: ChapterGroup) -> ChapterAssemblyResult:
        manifest = self.manifest
        ordered_units = self._resolve_group_units(group)
        if not ordered_units:
            if self.config.empty_chapter_policy == "omit":
                return ChapterAssemblyResult(
                    chapter_assembly_id=build_chapter_assembly_id(
                        book_id=manifest.book_id,
                        chapter_id=group.chapter_id,
                        chapter_order=group.chapter_order,
                        assembly_contract_version=self.config.assembly_contract_version,
                    ),
                    chapter_id=group.chapter_id,
                    chapter_order=group.chapter_order,
                    chapter_title=group.chapter_title,
                    source_section_id=group.source_section_id,
                    output_artifact_relative_path=self._chapter_relative_path(
                        group,
                        build_chapter_assembly_id(
                            book_id=manifest.book_id,
                            chapter_id=group.chapter_id,
                            chapter_order=group.chapter_order,
                            assembly_contract_version=self.config.assembly_contract_version,
                        ),
                    ),
                    output_artifact_path=str(
                        self._chapter_absolute_path(
                            group,
                            build_chapter_assembly_id(
                                book_id=manifest.book_id,
                                chapter_id=group.chapter_id,
                                chapter_order=group.chapter_order,
                                assembly_contract_version=self.config.assembly_contract_version,
                            ),
                        )
                    ),
                    sidecar_path=str(
                        self._chapter_sidecar_path(
                            group,
                            build_chapter_assembly_id(
                                book_id=manifest.book_id,
                                chapter_id=group.chapter_id,
                                chapter_order=group.chapter_order,
                                assembly_contract_version=self.config.assembly_contract_version,
                            ),
                        )
                    ),
                    chapter_input_hash="",
                    render_unit_ids=(),
                    status="blocked",
                    cache_hit=False,
                    speech_frame_count=0,
                    silence_frame_count=0,
                    frame_count=0,
                    duration_seconds=0.0,
                    bytes_written=0,
                    warnings=("empty chapter omitted by policy",),
                    errors=(),
                    blocked_unit_ids=(),
                    omitted_unit_ids=(),
                    missing_unit_ids=(),
                    invalid_unit_ids=(),
                    segment_statuses=(),
                )
            failure = AssemblyFailure(
                AssemblyFailureType.CHAPTER_MAPPING_MISSING,
                f"chapter {group.chapter_id} has no render units",
                chapter_id=group.chapter_id,
            )
            return self._blocked_result(group, [], [], [], [], warnings=("empty chapter blocked",))

        chapter_assembly_id = build_chapter_assembly_id(
            book_id=manifest.book_id,
            chapter_id=group.chapter_id,
            chapter_order=group.chapter_order,
            assembly_contract_version=self.config.assembly_contract_version,
        )
        output_relative_path = self._chapter_relative_path(group, chapter_assembly_id)
        output_path = self._chapter_absolute_path(group, chapter_assembly_id)
        sidecar_path = self._chapter_sidecar_path(group, chapter_assembly_id)
        if self._is_cache_hit(
            group,
            ordered_units,
            chapter_assembly_id,
            output_relative_path,
            output_path,
            sidecar_path,
        ):
            chapter_input_hash = self._chapter_input_hash(group, ordered_units, chapter_assembly_id)
            sidecar = load_chapter_sidecar(sidecar_path)
            return ChapterAssemblyResult(
                chapter_assembly_id=chapter_assembly_id,
                chapter_id=group.chapter_id,
                chapter_order=group.chapter_order,
                chapter_title=group.chapter_title,
                source_section_id=group.source_section_id,
                output_artifact_relative_path=output_relative_path,
                output_artifact_path=str(output_path),
                sidecar_path=str(sidecar_path),
                chapter_input_hash=chapter_input_hash,
                render_unit_ids=tuple(unit.render_unit_id for unit in ordered_units),
                status="cache-hit",
                cache_hit=True,
                speech_frame_count=sidecar.speech_frame_count,
                silence_frame_count=sidecar.silence_frame_count,
                frame_count=sidecar.frame_count,
                duration_seconds=sidecar.duration_seconds,
                bytes_written=0,
                audio_content_hash=sidecar.audio_content_hash,
                warnings=sidecar.warnings,
                errors=sidecar.errors,
                blocked_unit_ids=sidecar.blocked_unit_ids,
                omitted_unit_ids=sidecar.omitted_unit_ids,
                missing_unit_ids=sidecar.missing_unit_ids,
                invalid_unit_ids=sidecar.invalid_unit_ids,
                segment_statuses=tuple(),
            )

        segment_statuses: list[ChapterSegmentStatus] = []
        ready_units: list[tuple[Any, Any, Any, Any, Any]] = []
        blocked_unit_ids: list[str] = []
        omitted_unit_ids: list[str] = []
        missing_unit_ids: list[str] = []
        invalid_unit_ids: list[str] = []
        warnings: list[str] = []
        errors: list[str] = []
        for unit in ordered_units:
            status = self._validate_segment(unit)
            segment_statuses.append(status)
            if status.status == "omitted":
                omitted_unit_ids.append(unit.render_unit_id)
                continue
            if status.status == "blocked":
                blocked_unit_ids.append(unit.render_unit_id)
                errors.extend(status.warnings)
                continue
            if status.status != "ready":
                failure = status.failure
                if failure and failure.failure_type in {
                    AssemblyFailureType.SEGMENT_MISSING,
                    AssemblyFailureType.SEGMENT_SIDECAR_MISSING,
                }:
                    missing_unit_ids.append(unit.render_unit_id)
                else:
                    invalid_unit_ids.append(unit.render_unit_id)
                errors.extend(status.warnings)
                continue
            ready_units.append(
                (
                    unit,
                    status.artifact_path,
                    status.sidecar_path,
                    status.audio_content_hash,
                    status.synthesis_input_hash,
                    status.cache_key,
                    status.frame_count,
                    status.duration_seconds,
                )
            )

        if blocked_unit_ids:
            return self._blocked_result(
                group,
                blocked_unit_ids,
                omitted_unit_ids,
                missing_unit_ids,
                segment_statuses,
                warnings=tuple(warnings),
                errors=tuple(errors),
            )
        if ready_units and len(ready_units) != len(ordered_units) - len(omitted_unit_ids):
            return self._blocked_result(
                group,
                blocked_unit_ids,
                omitted_unit_ids,
                missing_unit_ids,
                segment_statuses,
                warnings=tuple(warnings),
                errors=tuple(errors),
            )
        if not ready_units and self.config.empty_chapter_policy != "silence":
            failure = AssemblyFailure(
                AssemblyFailureType.CHAPTER_MAPPING_MISSING,
                f"chapter {group.chapter_id} has no usable segments",
                chapter_id=group.chapter_id,
            )
            return self._blocked_result(
                group,
                blocked_unit_ids,
                omitted_unit_ids,
                missing_unit_ids,
                segment_statuses,
                warnings=tuple(warnings),
                errors=tuple(errors + [failure.message]),
            )

        chapter_input_hash = self._chapter_input_hash(group, ordered_units, chapter_assembly_id)
        if self._is_cache_hit(
            group,
            ordered_units,
            chapter_assembly_id,
            output_relative_path,
            output_path,
            sidecar_path,
            chapter_input_hash=chapter_input_hash,
        ):
            sidecar = load_chapter_sidecar(sidecar_path)
            return ChapterAssemblyResult(
                chapter_assembly_id=chapter_assembly_id,
                chapter_id=group.chapter_id,
                chapter_order=group.chapter_order,
                chapter_title=group.chapter_title,
                source_section_id=group.source_section_id,
                output_artifact_relative_path=output_relative_path,
                output_artifact_path=str(output_path),
                sidecar_path=str(sidecar_path),
                chapter_input_hash=chapter_input_hash,
                render_unit_ids=tuple(unit.render_unit_id for unit in ordered_units),
                status="cache-hit",
                cache_hit=True,
                speech_frame_count=sidecar.speech_frame_count,
                silence_frame_count=sidecar.silence_frame_count,
                frame_count=sidecar.frame_count,
                duration_seconds=sidecar.duration_seconds,
                bytes_written=0,
                audio_content_hash=sidecar.audio_content_hash,
                warnings=sidecar.warnings,
                errors=sidecar.errors,
                blocked_unit_ids=sidecar.blocked_unit_ids,
                omitted_unit_ids=sidecar.omitted_unit_ids,
                missing_unit_ids=sidecar.missing_unit_ids,
                invalid_unit_ids=sidecar.invalid_unit_ids,
                segment_statuses=tuple(segment_statuses),
            )

        chapter_result = self._render_chapter(
            group,
            ordered_units,
            segment_statuses,
            chapter_assembly_id,
            output_relative_path,
            output_path,
            sidecar_path,
            chapter_input_hash,
            warnings,
            errors,
            blocked_unit_ids,
            omitted_unit_ids,
            missing_unit_ids,
            invalid_unit_ids,
        )
        return chapter_result

    def _render_chapter(
        self,
        group: ChapterGroup,
        ordered_units: list[Any],
        segment_statuses: list[ChapterSegmentStatus],
        chapter_assembly_id: str,
        output_relative_path: str,
        output_path: Path,
        sidecar_path: Path,
        chapter_input_hash: str,
        warnings: list[str],
        errors: list[str],
        blocked_unit_ids: list[str],
        omitted_unit_ids: list[str],
        missing_unit_ids: list[str],
        invalid_unit_ids: list[str],
    ) -> ChapterAssemblyResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_tmp = self._render_temp_path(output_path)
        sidecar_tmp = self._render_temp_path(sidecar_path)
        previous_audio: Path | None = None
        previous_sidecar: Path | None = None
        try:
            previous_audio = self._backup_existing(output_path)
            previous_sidecar = self._backup_existing(sidecar_path)
            rendered = self._assemble_audio_frames(group, ordered_units)
            audio_tmp.write_bytes(rendered["audio_bytes"])
            validate_rendered_audio(
                audio_tmp,
                expected_sample_rate=self.config.sample_rate_hz,
                expected_channels=self.config.channel_count,
                expected_sample_width=self.config.sample_width_bytes,
                maximum_duration_seconds=max(1.0, rendered["duration_seconds"] + 1.0),
            )
            os.replace(audio_tmp, output_path)
            sidecar = ChapterSidecar(
                chapter_assembly_id=chapter_assembly_id,
                chapter_id=group.chapter_id,
                chapter_order=group.chapter_order,
                chapter_title=group.chapter_title,
                source_section_id=group.source_section_id,
                book_id=self.manifest.book_id,
                manifest_content_hash=self.manifest.manifest_content_hash,
                assembly_contract_version=self.config.assembly_contract_version,
                assembler_version=self.config.assembler_version,
                chapter_input_hash=chapter_input_hash,
                ordered_render_unit_ids=tuple(unit.render_unit_id for unit in ordered_units),
                ordered_segment_synthesis_input_hashes=tuple(
                    item.synthesis_input_hash for item in rendered["segment_status_items"]
                ),
                ordered_segment_audio_content_hashes=tuple(
                    item.audio_content_hash for item in rendered["segment_status_items"]
                ),
                ordered_segment_cache_keys=tuple(
                    item.cache_key for item in rendered["segment_status_items"]
                ),
                ordered_segment_artifact_relative_paths=tuple(
                    item.artifact_relative_path for item in rendered["segment_status_items"]
                ),
                output_artifact_relative_path=output_relative_path,
                output_format=self.config.output_format,
                sample_rate_hz=self.config.sample_rate_hz,
                channel_count=self.config.channel_count,
                sample_width_bytes=self.config.sample_width_bytes,
                frame_count=rendered["frame_count"],
                speech_frame_count=rendered["speech_frame_count"],
                silence_frame_count=rendered["silence_frame_count"],
                duration_seconds=rendered["duration_seconds"],
                audio_content_hash=rendered["audio_content_hash"],
                validation_result="passed",
                warnings=tuple(dict.fromkeys(warnings)),
                errors=tuple(dict.fromkeys(errors)),
                blocked_unit_ids=tuple(blocked_unit_ids),
                omitted_unit_ids=tuple(omitted_unit_ids),
                missing_unit_ids=tuple(missing_unit_ids),
                invalid_unit_ids=tuple(invalid_unit_ids),
                chapter_source=copy.deepcopy(self.chapter_structure),
            )
            save_chapter_sidecar_payload(sidecar_tmp, chapter_sidecar_payload(sidecar))
            os.replace(sidecar_tmp, sidecar_path)
            return ChapterAssemblyResult(
                chapter_assembly_id=chapter_assembly_id,
                chapter_id=group.chapter_id,
                chapter_order=group.chapter_order,
                chapter_title=group.chapter_title,
                source_section_id=group.source_section_id,
                output_artifact_relative_path=output_relative_path,
                output_artifact_path=str(output_path),
                sidecar_path=str(sidecar_path),
                chapter_input_hash=chapter_input_hash,
                render_unit_ids=tuple(unit.render_unit_id for unit in ordered_units),
                status="assembled",
                cache_hit=False,
                speech_frame_count=rendered["speech_frame_count"],
                silence_frame_count=rendered["silence_frame_count"],
                frame_count=rendered["frame_count"],
                duration_seconds=rendered["duration_seconds"],
                bytes_written=len(rendered["audio_bytes"]),
                audio_content_hash=rendered["audio_content_hash"],
                warnings=tuple(dict.fromkeys(warnings)),
                errors=tuple(dict.fromkeys(errors)),
                blocked_unit_ids=tuple(blocked_unit_ids),
                omitted_unit_ids=tuple(omitted_unit_ids),
                missing_unit_ids=tuple(missing_unit_ids),
                invalid_unit_ids=tuple(invalid_unit_ids),
                segment_statuses=tuple(segment_statuses),
            )
        except Exception as exc:
            if previous_audio is not None or previous_sidecar is not None:
                if previous_sidecar is None and previous_audio is not None:
                    if output_path.exists():
                        output_path.unlink()
                    os.replace(previous_audio, output_path)
                else:
                    self._restore_backups(
                        output_path, sidecar_path, previous_audio, previous_sidecar
                    )
            failure = AssemblyFailure(
                AssemblyFailureType.UNKNOWN_FAILURE,
                f"failed to assemble chapter {group.chapter_id}: {exc}",
                chapter_id=group.chapter_id,
                details={"exception": type(exc).__name__},
            )
            return ChapterAssemblyResult(
                chapter_assembly_id=chapter_assembly_id,
                chapter_id=group.chapter_id,
                chapter_order=group.chapter_order,
                chapter_title=group.chapter_title,
                source_section_id=group.source_section_id,
                output_artifact_relative_path=output_relative_path,
                output_artifact_path=str(output_path),
                sidecar_path=str(sidecar_path),
                chapter_input_hash=chapter_input_hash,
                render_unit_ids=tuple(unit.render_unit_id for unit in ordered_units),
                status="failed",
                cache_hit=False,
                speech_frame_count=0,
                silence_frame_count=0,
                frame_count=0,
                duration_seconds=0.0,
                bytes_written=0,
                audio_content_hash=None,
                warnings=tuple(dict.fromkeys(warnings)),
                errors=tuple(dict.fromkeys(errors + [failure.message])),
                blocked_unit_ids=tuple(blocked_unit_ids),
                omitted_unit_ids=tuple(omitted_unit_ids),
                missing_unit_ids=tuple(missing_unit_ids),
                invalid_unit_ids=tuple(invalid_unit_ids),
                segment_statuses=tuple(segment_statuses),
            )
        finally:
            for tmp in (audio_tmp, sidecar_tmp):
                if tmp.exists():
                    tmp.unlink()

    def _assemble_audio_frames(
        self, group: ChapterGroup, ordered_units: list[Any]
    ) -> dict[str, Any]:
        frames: list[bytes] = []
        segment_status_items: list[Any] = []
        speech_frame_count = 0
        silence_frame_count_total = 0
        previous_unit = None
        for index, unit in enumerate(ordered_units):
            inspection = self._load_segment_inspection(unit)
            segment_status_items.append(inspection)
            speech_frame_count += inspection.frame_count
            frames.append(inspection.pcm_frames)
            if index < len(ordered_units) - 1:
                next_unit = ordered_units[index + 1]
                silence_ms = self._silence_for_transition(unit, next_unit)
                silence_count = silence_frame_count(
                    self.config.sample_rate_hz, silence_ms, rounding=self.config.silence_rounding
                )
                silence_frame_count_total += silence_count
                if silence_count:
                    frames.append(
                        generate_silence_bytes(
                            silence_count, self.config.channel_count, self.config.sample_width_bytes
                        )
                    )
            previous_unit = unit
        prefix_silence = silence_frame_count(
            self.config.sample_rate_hz,
            self.config.spacing.chapter_start_ms,
            rounding=self.config.silence_rounding,
        )
        suffix_silence = silence_frame_count(
            self.config.sample_rate_hz,
            self.config.spacing.chapter_end_ms,
            rounding=self.config.silence_rounding,
        )
        if prefix_silence:
            silence_frame_count_total += prefix_silence
            frames.insert(
                0,
                generate_silence_bytes(
                    prefix_silence, self.config.channel_count, self.config.sample_width_bytes
                ),
            )
        if suffix_silence:
            silence_frame_count_total += suffix_silence
            frames.append(
                generate_silence_bytes(
                    suffix_silence, self.config.channel_count, self.config.sample_width_bytes
                )
            )
        audio_bytes = self._pack_wav_bytes(b"".join(frames))
        duration_seconds = len(audio_bytes)  # placeholder replaced below
        duration_seconds = (
            speech_frame_count + silence_frame_count_total
        ) / self.config.sample_rate_hz
        audio_content_hash = sha256(audio_bytes).hexdigest()
        return {
            "audio_bytes": audio_bytes,
            "speech_frame_count": speech_frame_count,
            "silence_frame_count": silence_frame_count_total,
            "frame_count": speech_frame_count + silence_frame_count_total,
            "duration_seconds": round(duration_seconds, 6),
            "audio_content_hash": audio_content_hash,
            "segment_status_items": segment_status_items,
        }

    def _load_segment_inspection(self, unit: Any) -> Any:
        artifact_path = self._segment_root / unit.output_artifact_key
        sidecar_path = Path(str(artifact_path) + ".json")
        if not artifact_path.exists():
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_MISSING,
                    f"missing segment audio: {artifact_path}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if not sidecar_path.exists():
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_SIDECAR_MISSING,
                    f"missing segment sidecar: {sidecar_path}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        try:
            entry = load_render_sidecar(sidecar_path)
        except Exception as exc:  # noqa: BLE001
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_SIDECAR_CORRUPT,
                    f"corrupt segment sidecar: {sidecar_path}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                    details={"exception": type(exc).__name__},
                )
            ) from exc
        if entry is None:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_SIDECAR_MISSING,
                    f"missing segment sidecar: {sidecar_path}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if entry.render_unit_id != unit.render_unit_id:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_HASH_MISMATCH,
                    f"segment render unit mismatch: {entry.render_unit_id} != {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if entry.synthesis_input_hash != unit.synthesis_input_hash:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_HASH_MISMATCH,
                    f"segment synthesis input mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if entry.artifact_relative_path != unit.output_artifact_key:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_HASH_MISMATCH,
                    f"segment artifact path mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if (
            entry.provider != unit.assigned_provider
            or entry.provider_voice_id != unit.assigned_provider_voice_id
        ):
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_HASH_MISMATCH,
                    f"segment provider mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if entry.renderer_contract_version != self.manifest.renderer_contract_version:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_HASH_MISMATCH,
                    f"segment renderer contract mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if entry.output_format != self.config.output_format:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.INCOMPATIBLE_SEGMENT_FORMAT,
                    f"segment output format mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        expected_cache_key = build_render_cache_key(
            {
                "render_unit_id": unit.render_unit_id,
                "synthesis_input_hash": unit.synthesis_input_hash,
                "renderer_contract_version": entry.renderer_contract_version,
                "provider": entry.provider,
                "provider_voice_id": entry.provider_voice_id,
                "provider_adapter_version": entry.provider_adapter_version,
                "model_version": entry.model_version,
                "output_format": entry.output_format,
                "sample_rate_hz": entry.sample_rate_hz,
                "channel_count": entry.channel_count,
                "sample_width_bytes": entry.sample_width_bytes,
                "deterministic_seed": entry.deterministic_seed,
            }
        )
        if entry.cache_key != expected_cache_key:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.CACHE_CORRUPTION,
                    f"segment cache key mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if entry.validation_result != "passed":
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.INVALID_SEGMENT_AUDIO,
                    f"segment validation failed: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        try:
            inspection = inspect_wav_file(
                artifact_path,
                expected_sample_rate_hz=self.config.sample_rate_hz,
                expected_channel_count=self.config.channel_count,
                expected_sample_width_bytes=self.config.sample_width_bytes,
                maximum_duration_seconds=3600.0,
            )
        except Exception as exc:  # noqa: BLE001
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.INVALID_SEGMENT_AUDIO,
                    f"invalid segment audio: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                    details={"exception": type(exc).__name__},
                )
            ) from exc
        if inspection.audio_content_hash != entry.audio_content_hash:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_HASH_MISMATCH,
                    f"segment audio hash mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        if entry.output_format != self.config.output_format:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.INCOMPATIBLE_SEGMENT_FORMAT,
                    f"segment output format mismatch: {unit.render_unit_id}",
                    chapter_id=unit.scene_id,
                    render_unit_id=unit.render_unit_id,
                )
            )
        return type(
            "ValidatedSegment",
            (),
            {
                "render_unit_id": unit.render_unit_id,
                "artifact_path": str(artifact_path),
                "sidecar_path": str(sidecar_path),
                "audio_content_hash": inspection.audio_content_hash,
                "synthesis_input_hash": unit.synthesis_input_hash,
                "cache_key": entry.cache_key,
                "frame_count": inspection.frame_count,
                "duration_seconds": inspection.duration_seconds,
                "pcm_frames": inspection.pcm_frames,
                "artifact_relative_path": unit.output_artifact_key,
            },
        )()

    def _validate_segment(self, unit: Any) -> ChapterSegmentStatus:
        if unit.validation_status == "skipped":
            return ChapterSegmentStatus(
                render_unit_id=unit.render_unit_id,
                status="omitted",
                warnings=("manifest unit omitted before assembly",),
            )
        if unit.validation_status == "blocked":
            return ChapterSegmentStatus(
                render_unit_id=unit.render_unit_id,
                status="blocked",
                failure=AssemblyFailure(
                    AssemblyFailureType.MANIFEST_BLOCKED,
                    unit.blocked_reason or "manifest unit blocked",
                    render_unit_id=unit.render_unit_id,
                ),
                warnings=(unit.blocked_reason or "manifest unit blocked",),
            )
        try:
            validated = self._load_segment_inspection(unit)
        except ChapterAssemblyError as exc:
            failure = exc.failure
            status = (
                "missing"
                if failure.failure_type
                in {
                    AssemblyFailureType.SEGMENT_MISSING,
                    AssemblyFailureType.SEGMENT_SIDECAR_MISSING,
                }
                else "invalid"
            )
            return ChapterSegmentStatus(
                render_unit_id=unit.render_unit_id,
                status=status,
                failure=failure,
                warnings=(failure.message,),
            )
        return ChapterSegmentStatus(
            render_unit_id=unit.render_unit_id,
            status="ready",
            artifact_path=validated.artifact_path,
            sidecar_path=validated.sidecar_path,
            audio_content_hash=validated.audio_content_hash,
            synthesis_input_hash=validated.synthesis_input_hash,
            cache_key=validated.cache_key,
            frame_count=validated.frame_count,
            duration_seconds=validated.duration_seconds,
        )

    def _resolve_group_units(self, group: ChapterGroup) -> list[Any]:
        units_by_id = {unit.render_unit_id: unit for unit in self.manifest.render_units}
        if group.render_unit_ids:
            units = [
                units_by_id[unit_id] for unit_id in group.render_unit_ids if unit_id in units_by_id
            ]
        else:
            units = [
                unit
                for unit in self.manifest.render_units
                if int(unit.source_order[0]) == group.chapter_order
            ]
        units = [unit for unit in units if unit.validation_status != "skipped"]
        units.sort(key=self._unit_sort_key)
        return units

    def _build_chapter_groups(self) -> tuple[ChapterGroup, ...]:
        manifest = self.manifest
        if self.chapter_structure:
            chapters = (
                self.chapter_structure.get("chapters")
                or self.chapter_structure.get("sections")
                or []
            )
            if not chapters:
                raise ChapterAssemblyError(
                    AssemblyFailure(
                        AssemblyFailureType.CHAPTER_MAPPING_MISSING,
                        "chapter structure did not include chapters or sections",
                    )
                )
            groups: list[ChapterGroup] = []
            seen_ids: set[str] = set()
            assigned_units: set[str] = set()
            for index, entry in enumerate(chapters, start=1):
                chapter_id = str(
                    entry.get("chapter_id")
                    or entry.get("section_id")
                    or entry.get("id")
                    or f"chapter-{index}"
                )
                if chapter_id in seen_ids:
                    raise ChapterAssemblyError(
                        AssemblyFailure(
                            AssemblyFailureType.DUPLICATE_CHAPTER_ID,
                            f"duplicate chapter id: {chapter_id}",
                            chapter_id=chapter_id,
                        )
                    )
                seen_ids.add(chapter_id)
                chapter_order = int(
                    entry.get("chapter_order")
                    or entry.get("section_order")
                    or entry.get("order")
                    or index
                )
                chapter_title = entry.get("chapter_title") or entry.get("title")
                source_section_id = entry.get("source_section_id") or entry.get("section_id")
                explicit_unit_ids = [
                    str(item)
                    for item in (
                        entry.get("render_unit_ids") or entry.get("ordered_render_unit_ids") or []
                    )
                ]
                scene_ids = [str(item) for item in (entry.get("scene_ids") or [])]
                if explicit_unit_ids:
                    for unit_id in explicit_unit_ids:
                        if unit_id in assigned_units:
                            raise ChapterAssemblyError(
                                AssemblyFailure(
                                    AssemblyFailureType.DUPLICATE_RENDER_UNIT_MEMBERSHIP,
                                    f"render unit assigned to multiple chapters: {unit_id}",
                                    render_unit_id=unit_id,
                                    chapter_id=chapter_id,
                                )
                            )
                        assigned_units.add(unit_id)
                elif scene_ids:
                    for unit in manifest.render_units:
                        if unit.scene_id in scene_ids and unit.validation_status != "skipped":
                            if unit.render_unit_id in assigned_units:
                                raise ChapterAssemblyError(
                                    AssemblyFailure(
                                        AssemblyFailureType.DUPLICATE_RENDER_UNIT_MEMBERSHIP,
                                        f"render unit assigned to multiple chapters: {unit.render_unit_id}",
                                        render_unit_id=unit.render_unit_id,
                                        chapter_id=chapter_id,
                                    )
                                )
                            assigned_units.add(unit.render_unit_id)
                groups.append(
                    ChapterGroup(
                        chapter_id=chapter_id,
                        chapter_order=chapter_order,
                        chapter_title=chapter_title,
                        source_section_id=source_section_id,
                        render_unit_ids=tuple(explicit_unit_ids),
                        scene_ids=tuple(scene_ids),
                    )
                )
            return tuple(sorted(groups, key=lambda group: (group.chapter_order, group.chapter_id)))

        chapter_orders = sorted(
            {
                int(unit.source_order[0])
                for unit in manifest.render_units
                if unit.validation_status == "ready" and unit.source_order
            }
        )
        if chapter_orders:
            return tuple(
                ChapterGroup(
                    chapter_id=f"chapter-{order}",
                    chapter_order=order,
                    chapter_title=None,
                    source_section_id=None,
                    render_unit_ids=tuple(
                        unit.render_unit_id
                        for unit in sorted(
                            (
                                u
                                for u in manifest.render_units
                                if int(u.source_order[0]) == order
                                and u.validation_status == "ready"
                            ),
                            key=self._unit_sort_key,
                        )
                    ),
                    scene_ids=tuple(
                        sorted(
                            {
                                unit.scene_id
                                for unit in manifest.render_units
                                if int(unit.source_order[0]) == order
                                and unit.validation_status == "ready"
                            }
                        )
                    ),
                )
                for order in chapter_orders
            )
        if self.config.fallback_chapter_mode == "book":
            return (
                ChapterGroup(
                    chapter_id=manifest.book_id,
                    chapter_order=1,
                    chapter_title=(
                        manifest.source_artifacts.get("title")
                        if isinstance(manifest.source_artifacts, Mapping)
                        else None
                    ),
                    source_section_id=None,
                    render_unit_ids=tuple(
                        unit.render_unit_id
                        for unit in sorted(
                            (
                                unit
                                for unit in manifest.render_units
                                if unit.validation_status == "ready"
                            ),
                            key=self._unit_sort_key,
                        )
                    ),
                    scene_ids=tuple(
                        sorted(
                            {
                                unit.scene_id
                                for unit in manifest.render_units
                                if unit.validation_status == "ready"
                            }
                        )
                    ),
                ),
            )
        if self.config.fallback_chapter_mode == "scene":
            scenes = sorted(
                {
                    unit.scene_id
                    for unit in manifest.render_units
                    if unit.validation_status == "ready"
                }
            )
            return tuple(
                ChapterGroup(
                    chapter_id=scene_id,
                    chapter_order=index,
                    chapter_title=scene_id,
                    source_section_id=None,
                    render_unit_ids=tuple(
                        unit.render_unit_id
                        for unit in sorted(
                            (
                                unit
                                for unit in manifest.render_units
                                if unit.scene_id == scene_id and unit.validation_status == "ready"
                            ),
                            key=self._unit_sort_key,
                        )
                    ),
                    scene_ids=(scene_id,),
                )
                for index, scene_id in enumerate(scenes, start=1)
            )
        raise ChapterAssemblyError(
            AssemblyFailure(
                AssemblyFailureType.CHAPTER_MAPPING_MISSING,
                "no chapter structure available and fallback mode rejects assembly",
            )
        )

    def _is_cache_hit(
        self,
        group: ChapterGroup,
        ordered_units: list[Any],
        chapter_assembly_id: str,
        output_relative_path: str,
        output_path: Path,
        sidecar_path: Path,
        chapter_input_hash: str | None = None,
    ) -> bool:
        if not output_path.exists() or not sidecar_path.exists():
            return False
        try:
            sidecar = load_chapter_sidecar(sidecar_path)
        except ChapterSidecarError:
            return False
        try:
            expected_hash = chapter_input_hash or self._chapter_input_hash(
                group, ordered_units, chapter_assembly_id
            )
        except Exception:
            return False
        if not chapter_cache_entry_matches(
            sidecar,
            expected_chapter_assembly_id=chapter_assembly_id,
            expected_chapter_input_hash=expected_hash,
            expected_output_artifact_relative_path=output_relative_path,
            expected_render_unit_ids=tuple(unit.render_unit_id for unit in ordered_units),
            expected_output_format=self.config.output_format,
            expected_sample_rate_hz=self.config.sample_rate_hz,
            expected_channel_count=self.config.channel_count,
            expected_sample_width_bytes=self.config.sample_width_bytes,
            expected_assembly_contract_version=self.config.assembly_contract_version,
        ):
            return False
        try:
            validation = validate_rendered_audio(
                output_path,
                expected_sample_rate=self.config.sample_rate_hz,
                expected_channels=self.config.channel_count,
                expected_sample_width=self.config.sample_width_bytes,
                maximum_duration_seconds=max(1.0, sidecar.duration_seconds + 1.0),
            )
        except Exception:
            return False
        if validation.audio_content_hash != sidecar.audio_content_hash:
            return False
        if validation.frame_count != sidecar.frame_count:
            return False
        if abs(validation.duration_seconds - sidecar.duration_seconds) > 1e-6:
            return False
        return True

    def _chapter_input_hash(
        self, group: ChapterGroup, ordered_units: list[Any], chapter_assembly_id: str
    ) -> str:
        payload = {
            "chapter_assembly_id": chapter_assembly_id,
            "chapter_id": group.chapter_id,
            "chapter_order": group.chapter_order,
            "book_id": self.manifest.book_id,
            "assembly_contract_version": self.config.assembly_contract_version,
            "ordered_render_unit_ids": [unit.render_unit_id for unit in ordered_units],
            "ordered_segment_synthesis_input_hashes": [
                unit.synthesis_input_hash for unit in ordered_units
            ],
            "ordered_segment_audio_content_hashes": [
                self._segment_audio_hash(unit) for unit in ordered_units
            ],
            "ordered_segment_cache_keys": [self._segment_cache_key(unit) for unit in ordered_units],
            "ordered_segment_artifact_relative_paths": [
                unit.output_artifact_key for unit in ordered_units
            ],
            "spacing": dataclass_to_dict(self.config.spacing),
            "output_format": self.config.output_format,
            "sample_rate_hz": self.config.sample_rate_hz,
            "channel_count": self.config.channel_count,
            "sample_width_bytes": self.config.sample_width_bytes,
            "chapter_group_identity": {
                "chapter_id": group.chapter_id,
                "chapter_order": group.chapter_order,
                "source_section_id": group.source_section_id,
            },
        }
        return sha256(canonical_json_dumps(payload).encode("utf-8")).hexdigest()

    def _segment_audio_hash(self, unit: Any) -> str:
        artifact_path = self._segment_root / unit.output_artifact_key
        return inspect_wav_file(
            artifact_path,
            expected_sample_rate_hz=self.config.sample_rate_hz,
            expected_channel_count=self.config.channel_count,
            expected_sample_width_bytes=self.config.sample_width_bytes,
            maximum_duration_seconds=3600.0,
        ).audio_content_hash

    def _segment_cache_key(self, unit: Any) -> str:
        sidecar_path = Path(str(self._segment_root / unit.output_artifact_key) + ".json")
        entry = load_render_sidecar(sidecar_path)
        if entry is None:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.SEGMENT_SIDECAR_MISSING,
                    f"missing sidecar for {unit.render_unit_id}",
                    render_unit_id=unit.render_unit_id,
                )
            )
        return entry.cache_key

    def _load_manifest(
        self, manifest_source: SynthesisManifest | Mapping[str, Any] | str | Path
    ) -> SynthesisManifest:
        if isinstance(manifest_source, SynthesisManifest):
            return manifest_source
        if isinstance(manifest_source, Path):
            return load_synthesis_manifest(manifest_source)
        if isinstance(manifest_source, str):
            return load_synthesis_manifest(Path(manifest_source))
        raise ChapterAssemblyError(
            AssemblyFailure(
                AssemblyFailureType.UNKNOWN_FAILURE,
                f"unsupported manifest source: {type(manifest_source).__name__}",
            )
        )

    def _load_optional_source(
        self, source: Mapping[str, Any] | str | Path | None
    ) -> dict[str, Any] | None:
        if source is None:
            return None
        if isinstance(source, Mapping):
            return copy.deepcopy(dict(source))
        path = Path(source)
        return json.loads(path.read_text(encoding="utf-8"))

    def _unit_sort_key(self, unit: Any) -> tuple[Any, ...]:
        order = tuple(int(value) for value in unit.source_order)
        return (*order, unit.render_unit_id)

    def _silence_for_transition(self, unit: Any, next_unit: Any) -> int:
        scene_boundary = unit.scene_id != next_unit.scene_id
        if scene_boundary:
            return self.config.spacing.scene_boundary_ms
        current_type = unit.segment_type
        next_type = next_unit.segment_type
        if current_type == "narration" and next_type == "narration":
            return (
                self.config.spacing.narration_to_narration_ms
                or self.config.spacing.default_between_segments_ms
            )
        if current_type == "narration" and next_type == "dialogue":
            return (
                self.config.spacing.narration_to_dialogue_ms
                or self.config.spacing.default_between_segments_ms
            )
        if current_type == "dialogue" and next_type == "narration":
            return (
                self.config.spacing.dialogue_to_narration_ms
                or self.config.spacing.default_between_segments_ms
            )
        if current_type == "dialogue" and next_type == "dialogue":
            return (
                self.config.spacing.dialogue_to_dialogue_ms
                or self.config.spacing.default_between_segments_ms
            )
        return self.config.spacing.default_between_segments_ms

    def _pack_wav_bytes(self, pcm_bytes: bytes) -> bytes:
        import io
        import wave

        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as handle:
            handle.setnchannels(self.config.channel_count)
            handle.setsampwidth(self.config.sample_width_bytes)
            handle.setframerate(self.config.sample_rate_hz)
            handle.setcomptype("NONE", "not compressed")
            handle.writeframes(pcm_bytes)
        return buffer.getvalue()

    def _render_temp_path(self, artifact_path: Path) -> Path:
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        handle = NamedTemporaryFile(
            "wb",
            dir=artifact_path.parent,
            prefix=f".{artifact_path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            return Path(handle.name)
        finally:
            handle.close()

    def _backup_existing(self, path: Path) -> Path | None:
        if not path.exists():
            return None
        backup = Path(str(path) + ".bak")
        if backup.exists():
            backup.unlink()
        path.replace(backup)
        return backup

    def _restore_backups(
        self,
        audio_path: Path,
        sidecar_path: Path,
        audio_backup: Path | None,
        sidecar_backup: Path | None,
    ) -> None:
        if audio_backup and audio_backup.exists():
            if audio_path.exists():
                audio_path.unlink()
            os.replace(audio_backup, audio_path)
        elif audio_path.exists() and (not sidecar_path.exists() or not sidecar_backup):
            audio_path.unlink()
        if sidecar_backup and sidecar_backup.exists():
            if sidecar_path.exists():
                sidecar_path.unlink()
            os.replace(sidecar_backup, sidecar_path)
        elif sidecar_path.exists() and not sidecar_backup:
            sidecar_path.unlink()

    def _chapter_relative_path(self, group: ChapterGroup, chapter_assembly_id: str) -> str:
        chapter_token = self._chapter_token(group.chapter_id)
        relative = (
            Path(self.config.chapter_path_prefix)
            / f"{group.chapter_order:04d}-{chapter_token}"
            / f"{chapter_assembly_id}.wav"
        )
        if relative.is_absolute() or any(part == ".." for part in relative.parts):
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.UNSAFE_OUTPUT_PATH,
                    f"unsafe chapter output path: {relative}",
                    chapter_id=group.chapter_id,
                )
            )
        return relative.as_posix()

    def _chapter_absolute_path(self, group: ChapterGroup, chapter_assembly_id: str) -> Path:
        return self._assembly_root / self._chapter_relative_path(group, chapter_assembly_id)

    def _chapter_sidecar_path(self, group: ChapterGroup, chapter_assembly_id: str) -> Path:
        return Path(str(self._chapter_absolute_path(group, chapter_assembly_id)) + ".json")

    def _chapter_token(self, value: str) -> str:
        if value.startswith("/") or ".." in value or "/" in value or "\\" in value:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.UNSAFE_OUTPUT_PATH,
                    f"unsafe chapter id: {value}",
                    chapter_id=value,
                )
            )
        token = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip().lower()
        )
        while "--" in token:
            token = token.replace("--", "-")
        token = token.strip("-._")
        if not token:
            raise ChapterAssemblyError(
                AssemblyFailure(
                    AssemblyFailureType.UNSAFE_OUTPUT_PATH,
                    f"unsafe chapter id: {value}",
                    chapter_id=value,
                )
            )
        return token

    def _blocked_result(
        self,
        group: ChapterGroup,
        blocked_unit_ids: list[str],
        omitted_unit_ids: list[str],
        missing_unit_ids: list[str],
        segment_statuses: list[ChapterSegmentStatus],
        *,
        warnings: tuple[str, ...] = (),
        errors: tuple[str, ...] = (),
    ) -> ChapterAssemblyResult:
        chapter_assembly_id = build_chapter_assembly_id(
            book_id=self.manifest.book_id,
            chapter_id=group.chapter_id,
            chapter_order=group.chapter_order,
            assembly_contract_version=self.config.assembly_contract_version,
        )
        output_relative_path = self._chapter_relative_path(group, chapter_assembly_id)
        output_path = self._chapter_absolute_path(group, chapter_assembly_id)
        sidecar_path = self._chapter_sidecar_path(group, chapter_assembly_id)
        return ChapterAssemblyResult(
            chapter_assembly_id=chapter_assembly_id,
            chapter_id=group.chapter_id,
            chapter_order=group.chapter_order,
            chapter_title=group.chapter_title,
            source_section_id=group.source_section_id,
            output_artifact_relative_path=output_relative_path,
            output_artifact_path=str(output_path),
            sidecar_path=str(sidecar_path),
            chapter_input_hash="",
            render_unit_ids=tuple(blocked_unit_ids + omitted_unit_ids + missing_unit_ids),
            status="blocked",
            cache_hit=False,
            speech_frame_count=0,
            silence_frame_count=0,
            frame_count=0,
            duration_seconds=0.0,
            bytes_written=0,
            audio_content_hash=None,
            warnings=warnings,
            errors=errors,
            blocked_unit_ids=tuple(blocked_unit_ids),
            omitted_unit_ids=tuple(omitted_unit_ids),
            missing_unit_ids=tuple(missing_unit_ids),
            invalid_unit_ids=tuple(),
            segment_statuses=tuple(segment_statuses),
        )

    def _completion_status(
        self, completed: int, blocked: int, partial: int, failed: int, warnings: list[str]
    ) -> str:
        if failed and not completed:
            return "failed"
        if failed:
            return "partial"
        if blocked and not completed:
            return "blocked"
        if blocked or partial:
            return "partial"
        return "complete-with-warnings" if warnings else "complete"

    def _write_report_atomic(self, report: ChapterAssemblyReport) -> None:
        payload = dataclass_to_dict(report)
        data = canonical_json_dumps(payload) + "\n"
        report_path = self._assembly_root / self.config.report_filename
        report_path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=report_path.parent,
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, report_path)


def assemble_chapters(
    manifest_source: SynthesisManifest | Mapping[str, Any] | str | Path,
    *,
    chapter_structure_source: Mapping[str, Any] | str | Path | None = None,
    config: ChapterAssemblyConfig,
) -> ChapterAssemblyReport:
    return ChapterAssembler(
        manifest_source, chapter_structure_source=chapter_structure_source, config=config
    ).assemble()


def compare_chapter_assemblies(
    left: ChapterSidecar | Mapping[str, Any], right: ChapterSidecar | Mapping[str, Any]
) -> bool:
    left_payload = dataclass_to_dict(left) if isinstance(left, ChapterSidecar) else dict(left)
    right_payload = dataclass_to_dict(right) if isinstance(right, ChapterSidecar) else dict(right)
    return canonical_json_dumps(left_payload) == canonical_json_dumps(right_payload)


def save_chapter_atomic(
    output_path: Path, sidecar_path: Path, audio_bytes: bytes, sidecar: ChapterSidecar
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    audio_tmp = NamedTemporaryFile(
        "wb", dir=output_path.parent, prefix=f".{output_path.name}.", suffix=".tmp", delete=False
    )
    sidecar_tmp = NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=sidecar_path.parent,
        prefix=f".{sidecar_path.name}.",
        suffix=".tmp",
        delete=False,
    )
    audio_backup = None
    sidecar_backup = None
    try:
        audio_backup = Path(str(output_path) + ".bak") if output_path.exists() else None
        sidecar_backup = Path(str(sidecar_path) + ".bak") if sidecar_path.exists() else None
        if audio_backup and audio_backup.exists():
            audio_backup.unlink()
        if sidecar_backup and sidecar_backup.exists():
            sidecar_backup.unlink()
        if output_path.exists() and audio_backup is not None:
            output_path.replace(audio_backup)
        if sidecar_path.exists() and sidecar_backup is not None:
            sidecar_path.replace(sidecar_backup)
        audio_tmp.write(audio_bytes)
        audio_tmp.flush()
        os.fsync(audio_tmp.fileno())
        os.replace(Path(audio_tmp.name), output_path)
        save_chapter_sidecar_payload(Path(sidecar_tmp.name), chapter_sidecar_payload(sidecar))
        sidecar_tmp.flush()
        os.fsync(sidecar_tmp.fileno())
        os.replace(Path(sidecar_tmp.name), sidecar_path)
        if audio_backup and audio_backup.exists():
            audio_backup.unlink()
        if sidecar_backup and sidecar_backup.exists():
            sidecar_backup.unlink()
    except Exception:
        if audio_backup is not None and audio_backup.exists():
            if output_path.exists():
                output_path.unlink()
            os.replace(audio_backup, output_path)
        if sidecar_backup is not None and sidecar_backup.exists():
            if sidecar_path.exists():
                sidecar_path.unlink()
            os.replace(sidecar_backup, sidecar_path)
        raise
    finally:
        for tmp_name in (audio_tmp.name, sidecar_tmp.name):
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                tmp_path.unlink()


__all__ = [
    "CHAPTER_ASSEMBLER_VERSION",
    "ChapterAssembler",
    "ChapterAssemblyError",
    "assemble_chapters",
    "compare_chapter_assemblies",
    "save_chapter_atomic",
]
