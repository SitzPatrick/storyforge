from __future__ import annotations

import copy
import json
import wave
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.renderer import RenderContext, RenderFailure, RenderFailureType, RenderSettings, SegmentRenderer
from app.renderer.providers.base import ProviderCapabilities, ProviderRenderRequest, ProviderRenderResult, ProviderRenderSession, TTSProviderAdapter
from app.renderer import engine as renderer_engine
from app.voice_planner import build_synthesis_manifest


def _make_wav(path: Path, *, duration: float = 0.1, sample_rate: int = 24000, channels: int = 1, sample_width: int = 2) -> Path:
    nframes = max(1, int(duration * sample_rate))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(sample_width)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00" * nframes * channels * sample_width)
    return path


def _registry() -> dict[str, object]:
    return {
        "schema_version": 1,
        "registry_version": "test",
        "voices": [
            {"schema_version": 1, "voice_id": "alpha.v1", "provider": "alpha", "provider_voice_id": "v1", "display_name": "Alpha V1", "availability": "available", "quality_score": 0.95, "base_priority": 100, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
            {"schema_version": 1, "voice_id": "alpha.v2", "provider": "alpha", "provider_voice_id": "v2", "display_name": "Alpha V2", "availability": "available", "quality_score": 0.93, "base_priority": 95, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
            {"schema_version": 1, "voice_id": "beta.v2", "provider": "beta", "provider_voice_id": "v2", "display_name": "Beta V2", "availability": "available", "quality_score": 0.92, "base_priority": 90, "archetype_tags": [], "style_tags": [], "supported_languages": ["en-US"], "supported_controls": ["rate"], "similarity_cluster": None},
        ],
    }


def _plan(*, ada_voice: str = "v1", ben_voice: str = "v2"):
    from app.voice_planner import EditableVoicePlan, VoiceAssignment, VoicePlan, CharacterPlan, NarratorPlan

    return VoicePlan(
        schema_version=1,
        planner_version="planner-1",
        book_id="book-9",
        series_id="series-9",
        source_analysis_hash="analysis-hash",
        source_analysis_path="/tmp/analysis.json",
        narrator=NarratorPlan(
            assignment=VoiceAssignment(
                voice_id="beta.v2",
                provider="beta",
                provider_voice_id="v2",
                locked=False,
                source="automatic",
                generated=True,
            ),
            rationale="narrator",
        ),
        characters=[
            CharacterPlan(
                canonical_character_id="ada",
                canonical_name="Ada",
                role="protagonist",
                prominence="major",
                speaking_frequency=10,
                first_appearance=1,
                likely_recurrence=True,
                assignment=VoiceAssignment(
                    voice_id=f"alpha.{ada_voice}",
                    provider="alpha",
                    provider_voice_id=ada_voice,
                    locked=False,
                    source="automatic",
                    generated=True,
                ),
            ),
            CharacterPlan(
                canonical_character_id="ben",
                canonical_name="Ben",
                role="supporting",
                prominence="secondary",
                speaking_frequency=4,
                first_appearance=2,
                likely_recurrence=True,
                assignment=VoiceAssignment(
                    voice_id=f"beta.{ben_voice}",
                    provider="beta",
                    provider_voice_id=ben_voice,
                    locked=False,
                    source="automatic",
                    generated=True,
                ),
            ),
        ],
        warnings=[],
        statistics={"total_characters": 2},
        user_editable_notes=[],
    )


def _story(*, ada_text: str = '"We should go now," Ada said.', ben_text: str = '"Not yet," Ben whispered.', source_order_variant: bool = False, blocked: bool = False, omitted: bool = False, unsafe_key: bool = False, note_only: bool = False, ada_rate: float = 1.0, ada_pronunciation_notes: str = "Ada pronounced AY-dah", ada_performance_notes: str = "calm"):
    segments = [
        {
            "segment_id": "narration-1",
            "segment_type": "narration",
            "scene_id": "scene-1",
            "chapter": 1,
            "source_order": 1,
            "source_text": "The morning light filled the room.",
            "synthesis_text": "The morning light filled the room.",
            "source_text_hash": "n1",
            "source_reference": {"chapter": 1, "paragraph_index": 1, "source_document_id": "book-9", "source_text_hash": "n1", "excerpt": "The morning light filled the room."},
        },
        {
            "segment_id": "dialogue-ada",
            "segment_type": "dialogue",
            "scene_id": "scene-1",
            "chapter": 1,
            "source_order": 2,
            "speaker": "Ada",
            "speaker_type": "character",
            "source_text": ada_text,
            "synthesis_text": ada_text,
            "source_text_hash": "d1",
            "source_reference": {"chapter": 1, "paragraph_index": 2, "source_document_id": "book-9", "source_text_hash": "d1", "excerpt": ada_text},
            "controls": {"rate": ada_rate},
            "pronunciation_notes": ada_pronunciation_notes,
            "performance_notes": ada_performance_notes,
        },
        {
            "segment_id": "narration-2",
            "segment_type": "narration",
            "scene_id": "scene-2",
            "chapter": 1,
            "source_order": 3,
            "source_text": "Ben nodded in agreement.",
            "synthesis_text": "Ben nodded in agreement.",
            "source_text_hash": "n2",
            "source_reference": {"chapter": 1, "paragraph_index": 3, "source_document_id": "book-9", "source_text_hash": "n2", "excerpt": "Ben nodded in agreement."},
        },
        {
            "segment_id": "dialogue-ben",
            "segment_type": "dialogue",
            "scene_id": "scene-2",
            "chapter": 1,
            "source_order": 4,
            "speaker": "Ben",
            "speaker_type": "character",
            "source_text": ben_text,
            "synthesis_text": "Not yet.",
            "source_text_hash": "d2",
            "source_reference": {"chapter": 1, "paragraph_index": 4, "source_document_id": "book-9", "source_text_hash": "d2", "excerpt": ben_text},
            "controls": {"rate": 1.0},
        },
    ]
    if source_order_variant:
        segments = [segments[2], segments[0], segments[3], segments[1]]
    if blocked:
        segments[3]["speaker"] = "Unknown Traveler"
        segments[3]["speaker_type"] = "unresolved"
    if omitted:
        segments[3]["speaker"] = "Unknown Traveler"
        segments[3]["speaker_type"] = "unresolved"
    if unsafe_key:
        segments[1]["segment_id"] = "../escape"
    return {
        "schema_version": 1,
        "book_id": "book-9",
        "series_id": "series-9",
        "title": "River City Nights",
        "author": "Test Author",
        "language": "en",
        "source_analysis_hash": "analysis-hash",
        "source_analysis_path": "/tmp/analysis.json",
        "source_document_id": "book-9",
        "source_signature": {"sha256": "analysis-hash"},
        "characters": [
            {"canonical_character_id": "ada", "canonical_name": "Ada", "aliases": ["Ad"], "source_aliases": ["Ad"]},
            {"canonical_character_id": "ben", "canonical_name": "Ben", "aliases": [], "source_aliases": []},
        ],
        "scenes": [
            {"scene_id": "scene-1", "chapter": 1, "scene_number": 1, "start_paragraph": 1, "end_paragraph": 2, "summary": "Ada speaks with Ben.", "source_document_id": "book-9", "source_text_hash": "scene-1"},
            {"scene_id": "scene-2", "chapter": 1, "scene_number": 2, "start_paragraph": 3, "end_paragraph": 4, "summary": "Ben and Ada speak.", "source_document_id": "book-9", "source_text_hash": "scene-2"},
        ],
        "dialogue": [
            {"dialogue_id": "d1", "scene_id": "scene-1", "chapter": 1, "paragraph_index": 2, "speaker": "Ada", "quoted_text": "We should go now.", "source_document_id": "book-9", "source_text_hash": "d1", "source_reference": {"chapter": 1, "paragraph_index": 2, "source_document_id": "book-9", "source_text_hash": "d1", "excerpt": ada_text}},
            {"dialogue_id": "d2", "scene_id": "scene-2", "chapter": 1, "paragraph_index": 4, "speaker": "Ben", "quoted_text": "Not yet.", "source_document_id": "book-9", "source_text_hash": "d2", "source_reference": {"chapter": 1, "paragraph_index": 4, "source_document_id": "book-9", "source_text_hash": "d2", "excerpt": ben_text}},
        ],
        "narration_paragraphs": [
            {"chapter": 1, "paragraph_index": 1, "text": "The morning light filled the room.", "source_document_id": "book-9", "source_text_hash": "n1", "source_reference": {"chapter": 1, "paragraph_index": 1, "source_document_id": "book-9", "source_text_hash": "n1", "excerpt": "The morning light filled the room."}},
            {"chapter": 1, "paragraph_index": 3, "text": "Ben nodded in agreement.", "source_document_id": "book-9", "source_text_hash": "n2", "source_reference": {"chapter": 1, "paragraph_index": 3, "source_document_id": "book-9", "source_text_hash": "n2", "excerpt": "Ben nodded in agreement."}},
        ],
        "segments": segments,
        "source_artifacts": {"normalized_story": "analysis/normalized_story.json"},
    }


def _manifest(tmp_path: Path, *, ada_voice: str = "v1", ben_voice: str = "v2", story_kwargs: dict | None = None, renderer_contract_version: int = 1, unresolved_policy: str = "block"):
    story = _story(**(story_kwargs or {}))
    plan = _plan(ada_voice=ada_voice, ben_voice=ben_voice)
    registry = _registry()
    config = {"voice_planner": {"renderer_contract_version": renderer_contract_version, "default_unresolved_speaker_policy": unresolved_policy, "manifest_filename": "synthesis_manifest.json"}}
    result = build_synthesis_manifest(story, plan, registry, config, unresolved_speaker_policy=unresolved_policy)
    manifest = result.manifest
    if story_kwargs and story_kwargs.get("note_only"):
        manifest = replace(manifest, validation_report=replace(manifest.validation_report, warnings=[*manifest.validation_report.warnings, "note-only metadata change"]))
    if story_kwargs and story_kwargs.get("omitted"):
        manifest = replace(manifest, render_units=[*manifest.render_units[:-1], replace(manifest.render_units[-1], validation_status="skipped", warnings=["manifest omission"], blocked_reason=None)])
    if story_kwargs and story_kwargs.get("unsafe_key"):
        manifest = replace(manifest, render_units=[replace(manifest.render_units[0], output_artifact_key="../escape.wav"), *manifest.render_units[1:]])
    return manifest


@dataclass
class FakeSession(ProviderRenderSession):
    adapter: "FakeProviderAdapter"

    def render(self, request: ProviderRenderRequest, output_path: Path) -> ProviderRenderResult:
        self.adapter.render_requests.append(request)
        if self.adapter.fail_next:
            self.adapter.fail_next = False
            raise RenderFailure(RenderFailureType.SYNTHESIS_FAILURE, "temporary failure", retryable=True, provider=self.adapter.provider_id)
        if self.adapter.permanent_fail_next:
            self.adapter.permanent_fail_next = False
            raise RenderFailure(RenderFailureType.UNSUPPORTED_VOICE, "permanent failure", retryable=False, provider=self.adapter.provider_id)
        _make_wav(output_path, duration=0.05)
        return ProviderRenderResult(provider=self.adapter.provider_id, provider_adapter_version=self.adapter.adapter_version, model_version=self.adapter.model_version, output_path=output_path, claimed_deterministic=True, warnings=[])


class FakeProviderAdapter(TTSProviderAdapter):
    def __init__(self, provider_id: str, *, adapter_version: str = "1.0.0", model_version: str = "model-a", supported_voices: set[str] | None = None):
        self.provider_id = provider_id
        self.adapter_version = adapter_version
        self.model_version = model_version
        self.supported_voices = supported_voices or {"v1", "v2"}
        self.open_session_calls = 0
        self.render_requests: list[ProviderRenderRequest] = []
        self.fail_next = False
        self.permanent_fail_next = False

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider_id=self.provider_id, adapter_version=self.adapter_version, model_version=self.model_version, supported_output_formats=("wav",), supported_sample_rates=(24000,), supported_channel_counts=(1,), supports_seed=False, deterministic=False)

    def open_session(self) -> ProviderRenderSession:
        self.open_session_calls += 1
        return FakeSession(self)

    def validate_voice(self, voice_id: str) -> None:
        if voice_id not in self.supported_voices:
            raise RenderFailure(RenderFailureType.UNSUPPORTED_VOICE, f"unsupported voice: {voice_id}", retryable=False, provider=self.provider_id)


def _renderer(tmp_path: Path, *, max_attempts: int = 2, allow_ready_with_warnings: bool = False):
    context = RenderContext(
        render_root=tmp_path / "renders",
        report_path=tmp_path / "render_report.json",
        output_format="wav",
        sample_rate_hz=24000,
        channel_count=1,
        sample_width_bytes=2,
        maximum_duration_seconds=10.0,
        renderer_contract_version=1,
        allow_ready_with_warnings=allow_ready_with_warnings,
        max_attempts=max_attempts,
        retry_delay_seconds=0.0,
        retryable_failure_types=(RenderFailureType.SYNTHESIS_FAILURE, RenderFailureType.TIMEOUT, RenderFailureType.PROVIDER_UNAVAILABLE),
    )
    return SegmentRenderer(context)


def test_basic_rendering_writes_audio_sidecars_and_report(tmp_path: Path):
    manifest = _manifest(tmp_path)
    renderer = _renderer(tmp_path)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")

    report = renderer.render(manifest, adapters={"alpha": provider, "beta": beta})

    assert report.completion_status == "complete"
    assert report.cache_hits == 0
    assert report.rendered_units == 4
    assert provider.open_session_calls == 1
    assert beta.open_session_calls == 1
    assert [item.render_unit_id for item in report.unit_results] == [unit.render_unit_id for unit in sorted(manifest.render_units, key=lambda u: u.source_order)]

    for unit in manifest.render_units:
        audio_path = renderer.resolve_artifact_path(unit.output_artifact_key)
        assert audio_path.exists()
        assert audio_path.suffix == ".audio"
        sidecar = Path(str(audio_path) + ".json")
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["render_unit_id"] == unit.render_unit_id
        assert payload["artifact_relative_path"] == unit.output_artifact_key
        assert payload["validation_result"] == "passed"

    report_json = json.loads((tmp_path / "render_report.json").read_text(encoding="utf-8"))
    assert report_json["completion_status"] == "complete"
    assert report_json["provider_adapters_used"] == ["alpha", "beta"]


def test_cache_hit_skips_provider_calls_and_records_hits(tmp_path: Path):
    manifest = _manifest(tmp_path)
    renderer = _renderer(tmp_path)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")

    first = renderer.render(manifest, adapters={"alpha": provider, "beta": beta})
    second = renderer.render(manifest, adapters={"alpha": provider, "beta": beta})

    assert first.rendered_units == 4
    assert second.cache_hits == 4
    assert second.rendered_units == 0
    assert provider.render_requests
    assert provider.open_session_calls == 1
    assert beta.open_session_calls == 1


def test_changed_text_voice_control_and_notes_inputs_invalidate_only_affected_units(tmp_path: Path):
    base = _manifest(tmp_path)
    renderer = _renderer(tmp_path)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")
    renderer.render(base, adapters={"alpha": provider, "beta": beta})

    changed_notes = _manifest(tmp_path, story_kwargs={"note_only": True})
    report_notes = renderer.render(changed_notes, adapters={"alpha": provider, "beta": beta})
    assert report_notes.cache_hits == 4
    assert report_notes.rendered_units == 0

    changed_manifest_hash = replace(base, manifest_content_hash="manifest-hash-2")
    report_manifest_hash = renderer.render(changed_manifest_hash, adapters={"alpha": provider, "beta": beta})
    assert report_manifest_hash.cache_hits == 4
    assert report_manifest_hash.rendered_units == 0

    changed_text = _manifest(tmp_path, story_kwargs={"ada_text": '"We should leave now," Ada said.'})
    report_text = renderer.render(changed_text, adapters={"alpha": provider, "beta": beta})
    assert report_text.rendered_units == 1

    changed_voice = _manifest(tmp_path, ada_voice="v2")
    report_voice = renderer.render(changed_voice, adapters={"alpha": provider, "beta": beta})
    assert report_voice.rendered_units == 1

    changed_control = _manifest(tmp_path, story_kwargs={"ada_rate": 1.25})
    report_control = renderer.render(changed_control, adapters={"alpha": provider, "beta": beta})
    assert report_control.rendered_units == 1

    changed_pronunciation = _manifest(tmp_path, story_kwargs={"ada_pronunciation_notes": "Ada pronounced AH-dah"})
    report_pronunciation = renderer.render(changed_pronunciation, adapters={"alpha": provider, "beta": beta})
    assert report_pronunciation.rendered_units == 1


def test_renderer_contract_and_adapter_version_changes_invalidate_cache(tmp_path: Path):
    manifest = _manifest(tmp_path)
    renderer = _renderer(tmp_path)
    provider = FakeProviderAdapter("alpha", adapter_version="1.0.0")
    beta = FakeProviderAdapter("beta", adapter_version="1.0.0")
    renderer.render(manifest, adapters={"alpha": provider, "beta": beta})

    provider_new = FakeProviderAdapter("alpha", adapter_version="2.0.0")
    beta_new = FakeProviderAdapter("beta", adapter_version="2.0.0")
    report_adapter = renderer.render(manifest, adapters={"alpha": provider_new, "beta": beta_new})
    assert report_adapter.rendered_units == 4

    newer_manifest = _manifest(tmp_path, renderer_contract_version=2)
    report_contract = renderer.render(newer_manifest, adapters={"alpha": provider_new, "beta": beta_new})
    assert report_contract.rendered_units == 4


def test_missing_audio_sidecar_and_corrupt_audio_rerender(tmp_path: Path):
    manifest = _manifest(tmp_path)
    renderer = _renderer(tmp_path)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")
    renderer.render(manifest, adapters={"alpha": provider, "beta": beta})

    first_unit = sorted(manifest.render_units, key=lambda u: u.source_order)[0]
    audio_path = renderer.resolve_artifact_path(first_unit.output_artifact_key)
    sidecar_path = Path(str(audio_path) + ".json")

    sidecar_path.unlink()
    report_missing_sidecar = renderer.render(manifest, adapters={"alpha": provider, "beta": beta})
    assert report_missing_sidecar.rendered_units == 1

    audio_path.write_bytes(b"broken")
    report_corrupt_audio = renderer.render(manifest, adapters={"alpha": provider, "beta": beta})
    assert report_corrupt_audio.rendered_units == 1


def test_blocked_manifest_and_blocked_unit_do_not_call_provider(tmp_path: Path):
    blocked_manifest = _manifest(tmp_path, unresolved_policy="block", story_kwargs={"blocked": True})
    renderer = _renderer(tmp_path)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")
    report = renderer.render(blocked_manifest, adapters={"alpha": provider, "beta": beta})
    assert report.completion_status == "blocked"
    assert provider.open_session_calls == 0
    assert beta.open_session_calls == 0

    omit_manifest = _manifest(tmp_path, unresolved_policy="omit", story_kwargs={"omitted": True})
    report_omit = renderer.render(omit_manifest, adapters={"alpha": provider, "beta": beta}, allow_ready_with_warnings=True)
    assert report_omit.skipped_units == 1
    assert report_omit.blocked_units == 0


def test_retry_policy_and_atomic_write_failure_and_resume(tmp_path: Path, monkeypatch):
    manifest = _manifest(tmp_path)
    renderer = _renderer(tmp_path, max_attempts=2)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")
    provider.fail_next = True

    report_retry = renderer.render(manifest, adapters={"alpha": provider, "beta": beta})
    assert report_retry.retryable_failures == 1
    assert report_retry.rendered_units == 4

    manifest2 = _manifest(tmp_path)
    renderer2 = _renderer(tmp_path)
    provider2 = FakeProviderAdapter("alpha")
    beta2 = FakeProviderAdapter("beta")
    renderer2.render(manifest2, adapters={"alpha": provider2, "beta": beta2})
    first_unit = sorted(manifest2.render_units, key=lambda u: u.source_order)[0]
    audio_path = renderer2.resolve_artifact_path(first_unit.output_artifact_key)
    original = audio_path.read_bytes()
    Path(str(audio_path) + ".json").unlink()

    def fail_replace(src, dst):
        raise OSError("atomic replace failed")

    monkeypatch.setattr("app.renderer.engine.os.replace", fail_replace)
    report_atomic = renderer2.render(manifest2, adapters={"alpha": provider2, "beta": beta2}, unit_ids=[first_unit.render_unit_id])
    assert report_atomic.failed_units == 1
    assert audio_path.read_bytes() == original

    monkeypatch.undo()
    resumed = renderer2.render(manifest2, adapters={"alpha": provider2, "beta": beta2})
    assert resumed.cache_hits == 3
    assert resumed.rendered_units == 1

    original_sidecar = Path(str(audio_path) + ".json").read_text(encoding="utf-8")
    audio_path.unlink()
    replace_calls = {"count": 0}
    original_replace = renderer_engine.os.replace

    def fail_second_replace(src, dst):
        replace_calls["count"] += 1
        if replace_calls["count"] == 2:
            raise OSError("sidecar replace failed")
        return original_replace(src, dst)

    monkeypatch.setattr("app.renderer.engine.os.replace", fail_second_replace)
    report_sidecar = renderer2.render(manifest2, adapters={"alpha": provider2, "beta": beta2}, unit_ids=[first_unit.render_unit_id])
    assert report_sidecar.failed_units == 1
    assert audio_path.read_bytes() == original
    assert Path(str(audio_path) + ".json").read_text(encoding="utf-8") == original_sidecar

    monkeypatch.undo()
    resumed_again = renderer2.render(manifest2, adapters={"alpha": provider2, "beta": beta2})
    assert resumed_again.cache_hits == 4
    assert resumed_again.rendered_units == 0

def test_unsafe_artifact_path_is_rejected_before_provider_invocation(tmp_path: Path):
    manifest = _manifest(tmp_path, story_kwargs={"unsafe_key": True})
    renderer = _renderer(tmp_path)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")

    with pytest.raises(ValueError, match="path traversal"):
        renderer.render(manifest, adapters={"alpha": provider, "beta": beta})
    assert provider.open_session_calls == 0
    assert beta.open_session_calls == 0


def test_reordered_manifest_input_renders_in_canonical_order_and_does_not_mutate_input(tmp_path: Path):
    manifest = _manifest(tmp_path, story_kwargs={"source_order_variant": True})
    original = copy.deepcopy(manifest)
    renderer = _renderer(tmp_path)
    original_context = copy.deepcopy(renderer.context)
    provider = FakeProviderAdapter("alpha")
    beta = FakeProviderAdapter("beta")

    report = renderer.render(manifest, adapters={"alpha": provider, "beta": beta})
    assert [item.render_unit_id for item in report.unit_results] == [unit.render_unit_id for unit in sorted(manifest.render_units, key=lambda u: u.source_order)]
    assert manifest == original
    assert renderer.context == original_context
