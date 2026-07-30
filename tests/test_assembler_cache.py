from __future__ import annotations

import json
from pathlib import Path

from app.assembler.cache import build_chapter_assembly_id, build_chapter_input_hash, chapter_cache_entry_matches, load_chapter_sidecar, save_chapter_sidecar_payload
from app.assembler.models import ChapterSidecar, ChapterSpacingConfig


def test_chapter_input_hash_changes_with_audio_affecting_inputs():
    base = build_chapter_input_hash(
        chapter_assembly_id="chapter-abc",
        chapter_id="chapter-1",
        chapter_order=1,
        book_id="book-1",
        assembly_contract_version=1,
        ordered_render_unit_ids=["u1", "u2"],
        ordered_segment_synthesis_input_hashes=["s1", "s2"],
        ordered_segment_audio_content_hashes=["a1", "a2"],
        ordered_segment_cache_keys=["c1", "c2"],
        ordered_segment_artifact_relative_paths=["segments/u1.wav", "segments/u2.wav"],
        spacing=ChapterSpacingConfig(narration_to_dialogue_ms=125).__dict__,
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
    )
    notes_only = build_chapter_input_hash(
        chapter_assembly_id="chapter-abc",
        chapter_id="chapter-1",
        chapter_order=1,
        book_id="book-1",
        assembly_contract_version=1,
        ordered_render_unit_ids=["u1", "u2"],
        ordered_segment_synthesis_input_hashes=["s1", "s2"],
        ordered_segment_audio_content_hashes=["a1", "a2"],
        ordered_segment_cache_keys=["c1", "c2"],
        ordered_segment_artifact_relative_paths=["segments/u1.wav", "segments/u2.wav"],
        spacing=ChapterSpacingConfig(narration_to_dialogue_ms=125).__dict__,
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
    )
    spacing_changed = build_chapter_input_hash(
        chapter_assembly_id="chapter-abc",
        chapter_id="chapter-1",
        chapter_order=1,
        book_id="book-1",
        assembly_contract_version=1,
        ordered_render_unit_ids=["u1", "u2"],
        ordered_segment_synthesis_input_hashes=["s1", "s2"],
        ordered_segment_audio_content_hashes=["a1", "a2"],
        ordered_segment_cache_keys=["c1", "c2"],
        ordered_segment_artifact_relative_paths=["segments/u1.wav", "segments/u2.wav"],
        spacing=ChapterSpacingConfig(narration_to_dialogue_ms=250).__dict__,
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
    )

    assert base == notes_only
    assert base != spacing_changed


def test_chapter_sidecar_roundtrip_and_cache_match(tmp_path: Path):
    sidecar = ChapterSidecar(
        chapter_assembly_id="chapter-abc",
        chapter_id="chapter-1",
        chapter_order=1,
        chapter_title="Chapter 1",
        source_section_id="section-1",
        book_id="book-1",
        manifest_content_hash="manifest-hash",
        assembly_contract_version=1,
        assembler_version="milestone-12",
        chapter_input_hash="input-hash",
        ordered_render_unit_ids=("u1", "u2"),
        ordered_segment_synthesis_input_hashes=("s1", "s2"),
        ordered_segment_audio_content_hashes=("a1", "a2"),
        ordered_segment_cache_keys=("c1", "c2"),
        ordered_segment_artifact_relative_paths=("segments/u1.wav", "segments/u2.wav"),
        output_artifact_relative_path="chapters/chapter-1/chapter-abc.wav",
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
        frame_count=100,
        speech_frame_count=80,
        silence_frame_count=20,
        duration_seconds=100 / 24000,
        audio_content_hash="audio-hash",
        validation_result="passed",
        warnings=("note",),
        errors=(),
        blocked_unit_ids=(),
        omitted_unit_ids=(),
        missing_unit_ids=(),
        invalid_unit_ids=(),
        chapter_source={"chapters": []},
    )

    sidecar_path = tmp_path / "chapter.wav.json"
    save_chapter_sidecar_payload(sidecar_path, json.loads(json.dumps(sidecar.__dict__, default=list)))
    loaded = load_chapter_sidecar(sidecar_path)

    assert loaded.chapter_assembly_id == sidecar.chapter_assembly_id
    assert chapter_cache_entry_matches(
        loaded,
        expected_chapter_assembly_id="chapter-abc",
        expected_chapter_input_hash="input-hash",
        expected_output_artifact_relative_path="chapters/chapter-1/chapter-abc.wav",
        expected_render_unit_ids=("u1", "u2"),
        expected_output_format="wav",
        expected_sample_rate_hz=24000,
        expected_channel_count=1,
        expected_sample_width_bytes=2,
        expected_assembly_contract_version=1,
    )


def test_chapter_assembly_id_is_stable():
    assert build_chapter_assembly_id(book_id="book-1", chapter_id="chapter-1", chapter_order=1, assembly_contract_version=1) == build_chapter_assembly_id(book_id="book-1", chapter_id="chapter-1", chapter_order=1, assembly_contract_version=1)
