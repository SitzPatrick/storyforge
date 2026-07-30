from __future__ import annotations

import copy
import json
import wave
from hashlib import sha256
from pathlib import Path

import pytest

from app.mastering import MasteringConfig, MasteringFailureType, MasteringReport, master_chapters


def _write_wav(path: Path, *, samples: list[int], sample_rate: int = 24000) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        payload = b"".join(int(sample).to_bytes(2, byteorder="little", signed=True) for sample in samples)
        handle.writeframes(payload)
    return path.read_bytes()


def _config(tmp_path: Path, *, target_loudness: float = -20.0, trim: bool = True, limiter: bool = True) -> MasteringConfig:
    return MasteringConfig(
        mastering_root=tmp_path / "mastered",
        source_root=tmp_path / "assembly",
        mastering_contract_version=1,
        processor_version="milestone-13",
        backend_name="python-rms",
        backend_version="1",
        target_integrated_loudness_dbfs=target_loudness,
        max_gain_increase_db=24.0,
        max_gain_reduction_db=24.0,
        max_sample_peak_dbfs=-1.0,
        trim_leading_silence_enabled=trim,
        trim_trailing_silence_enabled=trim,
        leading_silence_target_ms=5,
        trailing_silence_target_ms=5,
        silence_detection_threshold_dbfs=-60.0,
        minimum_silence_duration_ms=10,
        fade_in_ms=0,
        fade_out_ms=0,
        limiter_enabled=limiter,
        limiter_ceiling_dbfs=-1.0,
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
        assembler_compatibility_version=1,
        source_assembler_version="milestone-12",
    )


def _chapter_fixture(tmp_path: Path):
    source_root = tmp_path / "assembly"
    mastered_root = tmp_path / "mastered"
    chapter_records = []
    for idx, samples in enumerate(([0] * 100 + [1200] * 300 + [0] * 100, [0] * 50 + [2200] * 250 + [0] * 75), start=1):
        chapter_id = f"chapter-{idx}"
        chapter_assembly_id = f"chapter-assembly-{idx}"
        chapter_dir = source_root / "chapters" / chapter_id
        chapter_audio_path = chapter_dir / f"{chapter_assembly_id}.wav"
        audio_bytes = _write_wav(chapter_audio_path, samples=samples)
        sidecar = {
            "chapter_assembly_id": chapter_assembly_id,
            "chapter_id": chapter_id,
            "chapter_order": idx,
            "chapter_title": f"Chapter {idx}",
            "source_section_id": f"section-{idx}",
            "book_id": "book-9",
            "manifest_content_hash": "manifest-hash",
            "assembly_contract_version": 1,
            "assembler_version": "milestone-12",
            "chapter_input_hash": f"chapter-input-{idx}",
            "ordered_render_unit_ids": [f"render-unit-{idx}"],
            "ordered_segment_synthesis_input_hashes": [f"synthesis-hash-{idx}"],
            "ordered_segment_audio_content_hashes": [sha256(audio_bytes).hexdigest()],
            "ordered_segment_cache_keys": [f"cache-key-{idx}"],
            "ordered_segment_artifact_relative_paths": [f"chapters/{chapter_id}/{chapter_assembly_id}.wav"],
            "output_artifact_relative_path": f"chapters/{chapter_id}/{chapter_assembly_id}.wav",
            "output_format": "wav",
            "sample_rate_hz": 24000,
            "channel_count": 1,
            "sample_width_bytes": 2,
            "frame_count": len(samples),
            "speech_frame_count": len(samples) - 200,
            "silence_frame_count": 200,
            "duration_seconds": len(samples) / 24000,
            "audio_content_hash": sha256(audio_bytes).hexdigest(),
            "validation_result": "passed",
            "warnings": [],
            "errors": [],
            "blocked_unit_ids": [],
            "omitted_unit_ids": [],
            "missing_unit_ids": [],
            "invalid_unit_ids": [],
            "chapter_source": {"chapters": [chapter_id]},
        }
        chapter_dir.mkdir(parents=True, exist_ok=True)
        (chapter_dir / "chapter_sidecar.json").write_text(json.dumps(sidecar, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        chapter_records.append({
            "chapter_id": chapter_id,
            "chapter_order": idx,
            "chapter_title": f"Chapter {idx}",
            "source_section_id": f"section-{idx}",
            "chapter_assembly_id": chapter_assembly_id,
            "chapter_input_hash": f"chapter-input-{idx}",
            "output_artifact_relative_path": f"chapters/{chapter_id}/{chapter_assembly_id}.wav",
        })
    return source_root, mastered_root, chapter_records


def test_basic_mastering_produces_mastered_chapter_and_sidecar(tmp_path: Path):
    _, _, chapter_records = _chapter_fixture(tmp_path)
    report = master_chapters(chapter_records, config=_config(tmp_path))

    assert isinstance(report, MasteringReport)
    assert report.total_chapters == 2
    assert report.mastered_chapters == 2
    assert report.cache_hit_chapters == 0
    assert report.newly_processed_chapters == 2
    assert report.completion_status == "complete"
    first = report.chapter_results[0]
    assert Path(first.output_artifact_path).exists()
    assert Path(first.sidecar_path).exists()
    assert first.status in {"passed", "passed-with-warnings"}


def test_cache_hit_rerun_does_not_rewrite(tmp_path: Path):
    _, _, chapter_records = _chapter_fixture(tmp_path)
    config = _config(tmp_path)
    first = master_chapters(chapter_records, config=config)
    audio_path = Path(first.chapter_results[0].output_artifact_path)
    sidecar_path = Path(first.chapter_results[0].sidecar_path)
    audio_mtime = audio_path.stat().st_mtime_ns
    sidecar_mtime = sidecar_path.stat().st_mtime_ns

    second = master_chapters(chapter_records, config=config)
    assert second.cache_hit_chapters == 2
    assert audio_path.stat().st_mtime_ns == audio_mtime
    assert sidecar_path.stat().st_mtime_ns == sidecar_mtime


def test_changed_source_audio_reprocesses_only_one_chapter(tmp_path: Path):
    source_root, _, chapter_records = _chapter_fixture(tmp_path)
    config = _config(tmp_path)
    master_chapters(chapter_records, config=config)
    source_audio_path = source_root / chapter_records[0]["output_artifact_relative_path"]
    _write_wav(source_audio_path, samples=[0] * 80 + [4000] * 120 + [0] * 80)
    sidecar_path = source_audio_path.parent / "chapter_sidecar.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["audio_content_hash"] = sha256(source_audio_path.read_bytes()).hexdigest()
    sidecar["frame_count"] = 280
    sidecar["duration_seconds"] = 280 / 24000
    sidecar["silence_frame_count"] = 160
    sidecar["speech_frame_count"] = 120
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    rerun = master_chapters(chapter_records, config=config)
    assert rerun.cache_hit_chapters == 1
    assert rerun.newly_processed_chapters == 1


def test_notes_only_change_keeps_cache_hit(tmp_path: Path):
    _, _, chapter_records = _chapter_fixture(tmp_path)
    config = _config(tmp_path)
    master_chapters(chapter_records, config=config)
    updated = [dict(item) for item in chapter_records]
    updated[0]["chapter_title"] = "Edited Title"
    rerun = master_chapters(updated, config=config)
    assert rerun.cache_hit_chapters == 2


def test_corrupt_mastered_sidecar_rebuilds(tmp_path: Path):
    _, _, chapter_records = _chapter_fixture(tmp_path)
    config = _config(tmp_path)
    first = master_chapters(chapter_records, config=config)
    sidecar_path = Path(first.chapter_results[0].sidecar_path)
    sidecar_path.write_text("{broken", encoding="utf-8")
    rerun = master_chapters(chapter_records, config=config)
    assert rerun.cache_hit_chapters == 1
    assert rerun.newly_processed_chapters == 1


def test_source_mutation_is_false(tmp_path: Path):
    _, _, chapter_records = _chapter_fixture(tmp_path)
    snapshot = copy.deepcopy(chapter_records)
    config = _config(tmp_path)
    master_chapters(chapter_records, config=config)
    assert chapter_records == snapshot
