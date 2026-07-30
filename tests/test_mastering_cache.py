from __future__ import annotations

import json
import wave
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from app.mastering import MasteringConfig, build_mastered_chapter_id, build_mastering_input_hash, load_mastering_sidecar, save_mastering_sidecar


def _write_wav(path: Path, *, sample_rate: int = 24000, samples: list[int] | None = None) -> bytes:
    samples = samples or ([0] * 240)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        payload = b"".join(int(sample).to_bytes(2, byteorder="little", signed=True) for sample in samples)
        handle.writeframes(payload)
    return path.read_bytes()


def test_mastering_input_hash_changes_with_audio_affecting_settings():
    config = MasteringConfig()
    base = build_mastering_input_hash(
        mastering_contract_version=config.mastering_contract_version,
        processor_version=config.processor_version,
        backend_name=config.backend_name,
        backend_version=config.backend_version,
        book_id="book-1",
        chapter_id="chapter-1",
        chapter_order=1,
        chapter_assembly_id="chapter-assembly-1",
        source_chapter_input_hash="assembly-input-hash",
        source_chapter_audio_content_hash="audio-hash-1",
        target_integrated_loudness_dbfs=config.target_integrated_loudness_dbfs,
        max_gain_increase_db=config.max_gain_increase_db,
        max_gain_reduction_db=config.max_gain_reduction_db,
        max_sample_peak_dbfs=config.max_sample_peak_dbfs,
        trim_leading_silence_enabled=config.trim_leading_silence_enabled,
        trim_trailing_silence_enabled=config.trim_trailing_silence_enabled,
        leading_silence_target_ms=config.leading_silence_target_ms,
        trailing_silence_target_ms=config.trailing_silence_target_ms,
        silence_detection_threshold_dbfs=config.silence_detection_threshold_dbfs,
        minimum_silence_duration_ms=config.minimum_silence_duration_ms,
        fade_in_ms=config.fade_in_ms,
        fade_out_ms=config.fade_out_ms,
        limiter_enabled=config.limiter_enabled,
        limiter_ceiling_dbfs=config.limiter_ceiling_dbfs,
        output_format=config.output_format,
        sample_rate_hz=config.sample_rate_hz,
        channel_count=config.channel_count,
        sample_width_bytes=config.sample_width_bytes,
        source_chapter_assembler_version="milestone-12",
        source_chapter_audio_format="wav",
        source_chapter_sample_rate_hz=24000,
        source_chapter_channel_count=1,
        source_chapter_sample_width_bytes=2,
        source_chapter_output_relative_path="chapters/chapter-1/chapter-abc.wav",
        source_chapter_title="Chapter 1",
    )
    notes_only = build_mastering_input_hash(
        mastering_contract_version=config.mastering_contract_version,
        processor_version=config.processor_version,
        backend_name=config.backend_name,
        backend_version=config.backend_version,
        book_id="book-1",
        chapter_id="chapter-1",
        chapter_order=1,
        chapter_assembly_id="chapter-assembly-1",
        source_chapter_input_hash="assembly-input-hash",
        source_chapter_audio_content_hash="audio-hash-1",
        target_integrated_loudness_dbfs=config.target_integrated_loudness_dbfs,
        max_gain_increase_db=config.max_gain_increase_db,
        max_gain_reduction_db=config.max_gain_reduction_db,
        max_sample_peak_dbfs=config.max_sample_peak_dbfs,
        trim_leading_silence_enabled=config.trim_leading_silence_enabled,
        trim_trailing_silence_enabled=config.trim_trailing_silence_enabled,
        leading_silence_target_ms=config.leading_silence_target_ms,
        trailing_silence_target_ms=config.trailing_silence_target_ms,
        silence_detection_threshold_dbfs=config.silence_detection_threshold_dbfs,
        minimum_silence_duration_ms=config.minimum_silence_duration_ms,
        fade_in_ms=config.fade_in_ms,
        fade_out_ms=config.fade_out_ms,
        limiter_enabled=config.limiter_enabled,
        limiter_ceiling_dbfs=config.limiter_ceiling_dbfs,
        output_format=config.output_format,
        sample_rate_hz=config.sample_rate_hz,
        channel_count=config.channel_count,
        sample_width_bytes=config.sample_width_bytes,
        source_chapter_assembler_version="milestone-12",
        source_chapter_audio_format="wav",
        source_chapter_sample_rate_hz=24000,
        source_chapter_channel_count=1,
        source_chapter_sample_width_bytes=2,
        source_chapter_output_relative_path="chapters/chapter-1/chapter-abc.wav",
        source_chapter_title="Chapter 1 (edited title)",
    )
    spacing_changed = build_mastering_input_hash(
        mastering_contract_version=config.mastering_contract_version,
        processor_version=config.processor_version,
        backend_name=config.backend_name,
        backend_version=config.backend_version,
        book_id="book-1",
        chapter_id="chapter-1",
        chapter_order=1,
        chapter_assembly_id="chapter-assembly-1",
        source_chapter_input_hash="assembly-input-hash",
        source_chapter_audio_content_hash="audio-hash-1",
        target_integrated_loudness_dbfs=config.target_integrated_loudness_dbfs,
        max_gain_increase_db=config.max_gain_increase_db,
        max_gain_reduction_db=config.max_gain_reduction_db,
        max_sample_peak_dbfs=config.max_sample_peak_dbfs,
        trim_leading_silence_enabled=config.trim_leading_silence_enabled,
        trim_trailing_silence_enabled=config.trim_trailing_silence_enabled,
        leading_silence_target_ms=config.leading_silence_target_ms + 10,
        trailing_silence_target_ms=config.trailing_silence_target_ms,
        silence_detection_threshold_dbfs=config.silence_detection_threshold_dbfs,
        minimum_silence_duration_ms=config.minimum_silence_duration_ms,
        fade_in_ms=config.fade_in_ms,
        fade_out_ms=config.fade_out_ms,
        limiter_enabled=config.limiter_enabled,
        limiter_ceiling_dbfs=config.limiter_ceiling_dbfs,
        output_format=config.output_format,
        sample_rate_hz=config.sample_rate_hz,
        channel_count=config.channel_count,
        sample_width_bytes=config.sample_width_bytes,
        source_chapter_assembler_version="milestone-12",
        source_chapter_audio_format="wav",
        source_chapter_sample_rate_hz=24000,
        source_chapter_channel_count=1,
        source_chapter_sample_width_bytes=2,
        source_chapter_output_relative_path="chapters/chapter-1/chapter-abc.wav",
        source_chapter_title="Chapter 1",
    )

    assert base == notes_only
    assert base != spacing_changed


def test_mastered_chapter_id_is_stable():
    assert build_mastered_chapter_id(book_id="book-1", chapter_id="chapter-1", chapter_assembly_id="chapter-assembly-1", mastering_contract_version=1) == build_mastered_chapter_id(book_id="book-1", chapter_id="chapter-1", chapter_assembly_id="chapter-assembly-1", mastering_contract_version=1)


def test_mastering_sidecar_roundtrip(tmp_path: Path):
    payload = {
        "mastered_chapter_id": "mastered-1",
        "chapter_id": "chapter-1",
        "chapter_order": 1,
        "chapter_title": "Chapter 1",
        "book_id": "book-1",
        "source_chapter_assembly_id": "chapter-assembly-1",
        "source_chapter_input_hash": "input-hash",
        "source_chapter_audio_content_hash": "audio-hash",
        "mastering_contract_version": 1,
        "mastering_processor_version": "milestone-13",
        "processing_backend": "python-rms",
        "processing_backend_version": "1",
        "mastering_input_hash": "mastering-input-hash",
        "output_artifact_relative_path": "mastered/chapter-1/mastered-1.wav",
        "output_format": "wav",
        "sample_rate_hz": 24000,
        "channel_count": 1,
        "sample_width_bytes": 2,
        "input_frame_count": 240,
        "output_frame_count": 240,
        "input_duration_seconds": 0.01,
        "output_duration_seconds": 0.01,
        "input_integrated_loudness_dbfs": -26.0,
        "output_integrated_loudness_dbfs": -20.0,
        "input_sample_peak_dbfs": -18.0,
        "output_sample_peak_dbfs": -12.0,
        "true_peak_dbfs": None,
        "requested_gain_db": 6.0,
        "applied_gain_db": 6.0,
        "gain_constrained": False,
        "limiter_activated": False,
        "limiter_amount_db": None,
        "original_leading_silence_frames": 10,
        "original_trailing_silence_frames": 8,
        "trimmed_leading_silence_frames": 0,
        "trimmed_trailing_silence_frames": 0,
        "final_leading_silence_frames": 10,
        "final_trailing_silence_frames": 8,
        "fade_in_frames": 0,
        "fade_out_frames": 0,
        "mastered_audio_content_hash": "hash",
        "validation_result": "passed",
        "warnings": [],
    }
    sidecar_path = tmp_path / "mastered.json"
    save_mastering_sidecar(sidecar_path, payload)
    loaded = load_mastering_sidecar(sidecar_path)
    assert loaded.mastered_chapter_id == "mastered-1"
    assert loaded.validation_result == "passed"
    assert loaded.output_artifact_relative_path == "mastered/chapter-1/mastered-1.wav"
