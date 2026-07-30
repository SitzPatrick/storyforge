from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..models import ProviderCapabilities, ProviderRenderRequest, ProviderRenderResult, ProviderRenderSession, RenderFailure, RenderFailureType, TTSProviderAdapter


@dataclass
class KokoroProviderSession(ProviderRenderSession):
    adapter: "KokoroProviderAdapter"
    _client: Any | None = None

    def _client_instance(self):
        if self._client is None:
            self._client = self.adapter.client_factory()
        return self._client

    def _voice_allowed(self, voice_id: str) -> bool:
        if voice_id in self.adapter.voice_map:
            return True
        if voice_id in self.adapter.voice_aliases.values():
            return True
        if voice_id in self.adapter.voice_aliases:
            return True
        if voice_id in self.adapter.allowed_voice_ids:
            return True
        return False

    def render_text(self, text: str, output_path: Path, *, voice_id: str, render_unit_id: str, request: ProviderRenderRequest | None = None) -> ProviderRenderResult:
        if not self._voice_allowed(voice_id):
            raise RenderFailure(
                RenderFailureType.UNSUPPORTED_VOICE,
                f"unsupported voice: {voice_id}",
                retryable=False,
                provider=self.adapter.provider_id,
                provider_adapter_version=self.adapter.adapter_version,
                model_version=self.adapter.model_version,
                render_unit_id=render_unit_id,
            )
        client = self._client_instance()
        resolved_voice_id = self.adapter.resolve_voice_id(voice_id)
        client.voice = resolved_voice_id
        validate_voice = getattr(client, "validate_voice", None)
        if callable(validate_voice):
            validate_voice(resolved_voice_id)
        synthesizer = getattr(client, "synthesize", None)
        if not callable(synthesizer):
            raise RenderFailure(
                RenderFailureType.MODEL_UNAVAILABLE,
                "Kokoro client does not provide synthesize(text, output_path)",
                retryable=False,
                provider=self.adapter.provider_id,
                provider_adapter_version=self.adapter.adapter_version,
                model_version=self.adapter.model_version,
                render_unit_id=render_unit_id,
            )
        synthesize_result = synthesizer(text, output_path)
        return ProviderRenderResult(
            provider=self.adapter.provider_id,
            provider_adapter_version=self.adapter.adapter_version,
            model_version=self.adapter.model_version,
            output_path=Path(getattr(synthesize_result, "path", output_path)),
            claimed_deterministic=False,
            warnings=[],
        )

    def render(self, request: ProviderRenderRequest, output_path: Path) -> ProviderRenderResult:
        return self.render_text(
            request.synthesis_text,
            output_path,
            voice_id=request.provider_voice_id,
            render_unit_id=request.render_unit_id,
            request=request,
        )


class KokoroProviderAdapter(TTSProviderAdapter):
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        voice_map: Mapping[str, str] | None = None,
        client_factory: Callable[[], Any] | None = None,
        adapter_version: str = "1.0.0",
        model_version: str | None = None,
        allowed_voice_ids: set[str] | None = None,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.voice_map = dict(voice_map or {})
        self.voice_aliases = {key: value for key, value in self.voice_map.items() if isinstance(key, str) and isinstance(value, str)}
        self.allowed_voice_ids = set(allowed_voice_ids or set(self.voice_map.values()) or set(self.voice_map.keys()))
        self.adapter_version = adapter_version
        self.model_version = model_version or model
        self.client_factory = client_factory or self._default_client_factory

        self.provider_id = "kokoro"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id,
            adapter_version=self.adapter_version,
            model_version=self.model_version,
            supported_output_formats=("wav",),
            supported_sample_rates=(24000,),
            supported_channel_counts=(1,),
            supports_seed=False,
            deterministic=False,
        )

    def open_session(self) -> KokoroProviderSession:
        return KokoroProviderSession(self)

    def validate_voice(self, voice_id: str) -> None:
        session = self.open_session()
        if not session._voice_allowed(voice_id):
            raise RenderFailure(
                RenderFailureType.UNSUPPORTED_VOICE,
                f"unsupported voice: {voice_id}",
                retryable=False,
                provider=self.provider_id,
                provider_adapter_version=self.adapter_version,
                model_version=self.model_version,
            )

    def resolve_voice_id(self, voice_id: str) -> str:
        if voice_id in self.voice_map:
            return self.voice_map[voice_id]
        return voice_id

    def _default_client_factory(self):
        from app.kokoro_client import KokoroClient

        return KokoroClient(base_url=self.api_url, api_key=self.api_key, model=self.model, voice=next(iter(self.allowed_voice_ids), "af_bella"))
