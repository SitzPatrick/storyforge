from __future__ import annotations

from pathlib import Path

import pytest

from app.renderer.providers.kokoro import KokoroProviderAdapter


def test_kokoro_adapter_lazily_loads_client_and_reuses_session(tmp_path: Path):
    calls = {"factory": 0, "validate": 0, "synthesize": 0}

    class FakeClient:
        def validate_voice(self, voice_id: str) -> None:
            calls["validate"] += 1
            if voice_id != "af_bella":
                raise RuntimeError("unsupported voice")

        def synthesize(self, text: str, output_path: Path):
            calls["synthesize"] += 1
            output_path.write_bytes(b"RIFF....WAVEfmt ")
            return output_path

    def factory():
        calls["factory"] += 1
        return FakeClient()

    adapter = KokoroProviderAdapter(
        api_url="http://example.invalid/v1",
        api_key="not-needed",
        model="kokoro",
        voice_map={"narrator": "af_bella", "ada": "af_bella"},
        client_factory=factory,
        adapter_version="1.0.0",
        model_version="kokoro-1",
    )

    session = adapter.open_session()
    out1 = tmp_path / "one.wav"
    out2 = tmp_path / "two.wav"
    session.render_text("hello", out1, voice_id="af_bella", render_unit_id="ru_1")
    session.render_text("world", out2, voice_id="af_bella", render_unit_id="ru_2")

    assert calls["factory"] == 1
    assert calls["validate"] == 2
    assert calls["synthesize"] == 2
    assert out1.exists() and out2.exists()


def test_kokoro_adapter_rejects_unsupported_voice_before_render(tmp_path: Path):
    adapter = KokoroProviderAdapter(
        api_url="http://example.invalid/v1",
        api_key="not-needed",
        model="kokoro",
        voice_map={"narrator": "af_bella"},
        client_factory=lambda: object(),
        adapter_version="1.0.0",
        model_version="kokoro-1",
    )

    session = adapter.open_session()
    with pytest.raises(Exception):
        session.render_text("hello", tmp_path / "out.wav", voice_id="not-a-voice", render_unit_id="ru_1")


@pytest.mark.integration
@pytest.mark.skipif(not Path("/tmp").exists(), reason="placeholder skip; real Kokoro endpoint not configured")
def test_kokoro_integration_placeholder_skips_cleanly():
    pytest.skip("real Kokoro integration not configured in this environment")
