from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import replace
from pathlib import Path

from app.mastering.cache import build_mastered_chapter_id, build_mastering_input_hash
from app.packaging import (
    BookMetadata,
    FakePackagingBackend,
    MasteredChapterInput,
    PackagingCompletionStatus,
    PackagingConfig,
    PackagingFailureType,
    PackagingValidationStatus,
    package_audiobook,
)


def _write_silence_wav(path: Path, *, sample_rate: int = 24_000, duration_seconds: float = 1.0) -> None:
    frame_count = int(sample_rate * duration_seconds)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frame_count)


def _write_mastering_sidecar(
    *,
    path: Path,
    book_id: str,
    chapter_id: str,
    chapter_order: int,
    chapter_title: str,
    mastered_chapter_id: str,
    audio_path: Path,
    audio_hash: str,
    validation_result: str = "passed",
) -> None:
    source_chapter_assembly_id = f"assembly-{chapter_id}"
    source_chapter_input_hash = hashlib.sha256(f"{book_id}:{chapter_id}:{chapter_order}".encode("utf-8")).hexdigest()
    mastering_input_hash = build_mastering_input_hash(
        mastering_contract_version=1,
        processor_version="1.0",
        backend_name="mastering-backend",
        backend_version="1.0",
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_order=chapter_order,
        chapter_assembly_id=source_chapter_assembly_id,
        source_chapter_input_hash=source_chapter_input_hash,
        source_chapter_audio_content_hash=audio_hash,
        target_integrated_loudness_dbfs=-16.0,
        max_gain_increase_db=6.0,
        max_gain_reduction_db=12.0,
        max_sample_peak_dbfs=-1.0,
        trim_leading_silence_enabled=True,
        trim_trailing_silence_enabled=True,
        leading_silence_target_ms=0,
        trailing_silence_target_ms=0,
        silence_detection_threshold_dbfs=-40.0,
        minimum_silence_duration_ms=50,
        fade_in_ms=0,
        fade_out_ms=0,
        limiter_enabled=False,
        limiter_ceiling_dbfs=-1.0,
        output_format="wav",
        sample_rate_hz=24_000,
        channel_count=1,
        sample_width_bytes=2,
        source_chapter_assembler_version="1.0",
        source_chapter_audio_format="wav",
        source_chapter_sample_rate_hz=24_000,
        source_chapter_channel_count=1,
        source_chapter_sample_width_bytes=2,
        source_chapter_output_relative_path=f"mastered/{chapter_id}.wav",
        source_chapter_title=chapter_title,
    )
    payload = {
        "mastered_chapter_id": mastered_chapter_id,
        "chapter_id": chapter_id,
        "chapter_order": chapter_order,
        "chapter_title": chapter_title,
        "book_id": book_id,
        "source_chapter_assembly_id": source_chapter_assembly_id,
        "source_chapter_input_hash": source_chapter_input_hash,
        "source_chapter_audio_content_hash": audio_hash,
        "mastering_contract_version": 1,
        "mastering_processor_version": "1.0",
        "processing_backend": "mastering-backend",
        "processing_backend_version": "1.0",
        "mastering_input_hash": mastering_input_hash,
        "output_artifact_relative_path": f"mastered/{chapter_id}.wav",
        "output_format": "wav",
        "sample_rate_hz": 24_000,
        "channel_count": 1,
        "sample_width_bytes": 2,
        "input_frame_count": 24_000,
        "output_frame_count": 24_000,
        "input_duration_seconds": 1.0,
        "output_duration_seconds": 1.0,
        "input_integrated_loudness_dbfs": -18.0,
        "output_integrated_loudness_dbfs": -16.0,
        "input_sample_peak_dbfs": -1.5,
        "output_sample_peak_dbfs": -1.0,
        "true_peak_dbfs": -1.0,
        "requested_gain_db": 2.0,
        "applied_gain_db": 2.0,
        "gain_constrained": False,
        "limiter_activated": False,
        "limiter_amount_db": None,
        "original_leading_silence_frames": 0,
        "original_trailing_silence_frames": 0,
        "trimmed_leading_silence_frames": 0,
        "trimmed_trailing_silence_frames": 0,
        "final_leading_silence_frames": 0,
        "final_trailing_silence_frames": 0,
        "fade_in_frames": 0,
        "fade_out_frames": 0,
        "mastered_audio_content_hash": audio_hash,
        "validation_result": validation_result,
        "warnings": [],
        "errors": [],
        "source_chapter_output_relative_path": f"mastered/{chapter_id}.wav",
        "source_chapter_source": str(audio_path),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def _make_chapter_fixture(base: Path, book_id: str, chapter_id: str, chapter_order: int) -> MasteredChapterInput:
    chapter_dir = base / book_id / chapter_id
    chapter_dir.mkdir(parents=True, exist_ok=True)
    audio_path = chapter_dir / f"{chapter_id}.wav"
    sidecar_path = chapter_dir / "mastering_sidecar.json"
    _write_silence_wav(audio_path)
    audio_hash = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    mastered_chapter_id = build_mastered_chapter_id(
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_assembly_id=f"assembly-{chapter_id}",
        mastering_contract_version=1,
    )
    _write_mastering_sidecar(
        path=sidecar_path,
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_order=chapter_order,
        chapter_title=f"Chapter {chapter_order}",
        mastered_chapter_id=mastered_chapter_id,
        audio_path=audio_path,
        audio_hash=audio_hash,
    )
    mastering_input_hash = build_mastering_input_hash(
        mastering_contract_version=1,
        processor_version="1.0",
        backend_name="mastering-backend",
        backend_version="1.0",
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_order=chapter_order,
        chapter_assembly_id=f"assembly-{chapter_id}",
        source_chapter_input_hash=hashlib.sha256(f"{book_id}:{chapter_id}:{chapter_order}".encode("utf-8")).hexdigest(),
        source_chapter_audio_content_hash=audio_hash,
        target_integrated_loudness_dbfs=-16.0,
        max_gain_increase_db=6.0,
        max_gain_reduction_db=12.0,
        max_sample_peak_dbfs=-1.0,
        trim_leading_silence_enabled=True,
        trim_trailing_silence_enabled=True,
        leading_silence_target_ms=0,
        trailing_silence_target_ms=0,
        silence_detection_threshold_dbfs=-40.0,
        minimum_silence_duration_ms=50,
        fade_in_ms=0,
        fade_out_ms=0,
        limiter_enabled=False,
        limiter_ceiling_dbfs=-1.0,
        output_format="wav",
        sample_rate_hz=24_000,
        channel_count=1,
        sample_width_bytes=2,
        source_chapter_assembler_version="1.0",
        source_chapter_audio_format="wav",
        source_chapter_sample_rate_hz=24_000,
        source_chapter_channel_count=1,
        source_chapter_sample_width_bytes=2,
        source_chapter_output_relative_path=f"mastered/{chapter_id}.wav",
        source_chapter_title=f"Chapter {chapter_order}",
    )
    return MasteredChapterInput(
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_order=chapter_order,
        chapter_title=f"Chapter {chapter_order}",
        mastered_chapter_id=mastered_chapter_id,
        source_chapter_assembly_id=f"assembly-{chapter_id}",
        mastered_chapter_input_hash=mastering_input_hash,
        mastered_audio_content_hash=audio_hash,
        output_artifact_relative_path=f"mastered/{chapter_id}.wav",
        mastered_audio_path=audio_path,
        mastered_sidecar_path=sidecar_path,
        duration_seconds=1.0,
        sample_rate_hz=24_000,
        channel_count=1,
        sample_width_bytes=2,
        mastering_validation_result=PackagingValidationStatus.PASSED.value,
        source_chapter_path=audio_path,
    )


def _metadata() -> BookMetadata:
    return BookMetadata(
        title="Test Book",
        author="Patrick Sitz",
        narrator="Narrator One",
        language="en",
        subtitle="Subtitle",
        series="Series",
        series_position=1,
        publisher="Example Press",
        publication_year=2024,
        description="A test book.",
        copyright="© 2024 Example",
        genre="Fiction",
        identifier="ISBN-1234567890",
        comment="Test comment",
    )


def test_package_audiobook_builds_and_reuses_cache(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    package_root = tmp_path / "packages"
    chapters = [
        _make_chapter_fixture(mastered_root, "book-1", "chapter-2", 2),
        _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1),
    ]
    backend = FakePackagingBackend()
    config = PackagingConfig(package_root=package_root)

    result = package_audiobook(chapters, metadata=_metadata(), config=config, backend=backend)
    assert result.status == PackagingCompletionStatus.COMPLETE
    assert result.cache_hit is False
    assert result.newly_created is True
    assert result.output_artifact_path.exists()
    assert result.sidecar_path.exists()
    assert result.report_path.exists()
    assert result.chapter_count == 2
    assert backend.package_calls == 1

    cached = package_audiobook(list(reversed(chapters)), metadata=_metadata(), config=config, backend=backend)
    assert cached.cache_hit is True
    assert cached.newly_created is False
    assert cached.output_artifact_path == result.output_artifact_path
    assert backend.package_calls == 1
    assert backend.probe_calls >= 2


def test_package_audiobook_sorts_reordered_inputs_deterministically(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    config_a = PackagingConfig(package_root=tmp_path / "packages-a")
    config_b = PackagingConfig(package_root=tmp_path / "packages-b")
    chapters = [
        _make_chapter_fixture(mastered_root, "book-1", "chapter-3", 3),
        _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1),
        _make_chapter_fixture(mastered_root, "book-1", "chapter-2", 2),
    ]
    backend_a = FakePackagingBackend()
    backend_b = FakePackagingBackend()

    result_a = package_audiobook(chapters, metadata=_metadata(), config=config_a, backend=backend_a)
    result_b = package_audiobook(list(reversed(chapters)), metadata=_metadata(), config=config_b, backend=backend_b)

    assert result_a.package_input_hash == result_b.package_input_hash
    assert result_a.package_id == result_b.package_id
    assert result_a.report.chapters_expected == 3
    assert result_b.report.chapters_packaged == 3


def test_package_audiobook_blocks_before_backend_on_hash_mismatch(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    package_root = tmp_path / "packages"
    chapter = _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1)
    sidecar = json.loads(chapter.mastered_sidecar_path.read_text(encoding="utf-8"))
    sidecar["mastered_audio_content_hash"] = "bad-hash"
    chapter.mastered_sidecar_path.write_text(json.dumps(sidecar, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

    backend = FakePackagingBackend()
    result = package_audiobook([chapter], metadata=_metadata(), config=PackagingConfig(package_root=package_root), backend=backend)

    assert result.status == PackagingCompletionStatus.BLOCKED
    assert result.failure is not None
    assert result.failure.failure_type == PackagingFailureType.SOURCE_HASH_MISMATCH
    assert backend.package_calls == 0


def test_package_audiobook_backend_unavailable(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    chapter = _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1)
    backend = FakePackagingBackend(available=False)

    result = package_audiobook([chapter], metadata=_metadata(), config=PackagingConfig(package_root=tmp_path / "packages"), backend=backend)

    assert result.status == PackagingCompletionStatus.BLOCKED
    assert result.failure is not None
    assert result.failure.failure_type == PackagingFailureType.PACKAGING_BACKEND_UNAVAILABLE
    assert backend.package_calls == 0


def test_package_audiobook_encoding_failure(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    chapter = _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1)
    backend = FakePackagingBackend(fail_on_package=True)

    result = package_audiobook([chapter], metadata=_metadata(), config=PackagingConfig(package_root=tmp_path / "packages"), backend=backend)

    assert result.status == PackagingCompletionStatus.FAILED
    assert result.failure is not None
    assert result.failure.failure_type == PackagingFailureType.ENCODING_FAILURE
    assert backend.package_calls == 1


def test_package_audiobook_output_validation_failure(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    chapter = _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1)
    backend = FakePackagingBackend(
        probe_override={
            "output_path": None,
            "output_container": "m4b",
            "audio_codec": "mp3",
            "audio_bitrate_kbps": 96,
            "sample_rate_hz": 24_000,
            "channel_count": 1,
            "duration_seconds": 1.0,
            "chapter_count": 1,
            "chapter_probe_data": [],
            "metadata_probe_data": {},
            "cover_art_probe_state": None,
            "backend_name": "fake-packaging-backend",
            "backend_version": "1.0",
            "encoder_name": "fake-encoder",
            "encoder_version": "1.0",
            "file_size": 0,
            "audio_content_hash": "x",
            "validation_result": "failed",
            "warnings": [],
            "errors": ["bad codec"],
        }
    )

    result = package_audiobook([chapter], metadata=_metadata(), config=PackagingConfig(package_root=tmp_path / "packages"), backend=backend)

    assert result.status in {PackagingCompletionStatus.FAILED, PackagingCompletionStatus.BLOCKED}
    assert result.failure is not None
    assert result.failure.failure_type == PackagingFailureType.OUTPUT_VALIDATION_FAILURE


def test_package_audiobook_rolls_back_on_sidecar_failure_and_resumes(tmp_path: Path, monkeypatch):
    mastered_root = tmp_path / "mastered"
    package_root = tmp_path / "packages"
    chapter = _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1)
    backend = FakePackagingBackend()
    config = PackagingConfig(package_root=package_root)

    first = package_audiobook([chapter], metadata=_metadata(), config=config, backend=backend)
    original_bytes = first.output_artifact_path.read_bytes()
    original_sidecar = first.sidecar_path.read_text(encoding="utf-8")

    monkeypatch.setattr("app.packaging.engine.save_package_sidecar", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sidecar write failed")))
    mutated = replace(chapter, chapter_title="Chapter 1 revised")
    failed = package_audiobook([mutated], metadata=_metadata(), config=config, backend=backend)

    assert failed.status == PackagingCompletionStatus.FAILED
    assert first.output_artifact_path.read_bytes() == original_bytes
    assert first.sidecar_path.read_text(encoding="utf-8") == original_sidecar

    monkeypatch.undo()
    resumed = package_audiobook([mutated], metadata=_metadata(), config=config, backend=backend)
    assert resumed.status == PackagingCompletionStatus.COMPLETE
    assert resumed.cache_hit is False


def test_package_audiobook_rejects_unsafe_output_root(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    chapter = _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1)
    backend = FakePackagingBackend()
    config = PackagingConfig(package_root=Path("../unsafe-packages"))

    result = package_audiobook([chapter], metadata=_metadata(), config=config, backend=backend)

    assert result.status == PackagingCompletionStatus.BLOCKED
    assert result.failure is not None
    assert result.failure.failure_type == PackagingFailureType.UNSAFE_OUTPUT_PATH
    assert backend.package_calls == 0


def test_package_audiobook_does_not_mutate_inputs(tmp_path: Path):
    mastered_root = tmp_path / "mastered"
    chapter = _make_chapter_fixture(mastered_root, "book-1", "chapter-1", 1)
    chapters = [chapter]
    backend = FakePackagingBackend()
    config = PackagingConfig(package_root=tmp_path / "packages")

    before = chapters[0]
    result = package_audiobook(chapters, metadata=_metadata(), config=config, backend=backend)

    assert result.status == PackagingCompletionStatus.COMPLETE
    assert chapters[0] == before
