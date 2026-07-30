from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence
from urllib.parse import urljoin

import requests
import time

DEFAULT_STATIC_VOICES = [
    "af",
    "af_bella",
    "af_irulan",
    "af_nicole",
    "af_sarah",
    "af_sky",
    "am_adam",
    "am_gurney",
    "am_michael",
    "bf_emma",
    "bf_isabella",
    "bm_george",
    "bm_lewis",
]

OPENAI_VOICE_ALIASES = {
    "alloy": "am_adam",
    "echo": "af_bella",
    "fable": "af_sarah",
    "onyx": "bm_george",
    "nova": "bf_isabella",
    "shimmer": "af_sky",
    "ash": "af_nicole",
    "coral": "bf_emma",
    "sage": "am_michael",
}

SUPPORTED_RESPONSE_FORMATS = ["mp3", "opus", "aac", "flac", "wav", "pcm"]


@dataclass(frozen=True)
class KokoroResult:
    content_type: str
    path: Path
    response_bytes: bytes


class KokoroError(RuntimeError):
    pass


class KokoroHealthError(KokoroError):
    pass


class KokoroVoiceError(KokoroError):
    pass


class KokoroClient:
    def __init__(
        self,
        base_url: str,
        api_key: str = "not-needed",
        model: str = "kokoro",
        voice: str = "af_bella",
        speed: float = 1.0,
        timeout: float = 120.0,
        retry_delays: Sequence[float] = (2.0, 5.0, 10.0),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.speed = speed
        self.timeout = timeout
        self.retry_delays = tuple(float(delay) for delay in retry_delays)

    @property
    def speech_url(self) -> str:
        return urljoin(self.base_url + "/", "audio/speech")

    @property
    def voices_url(self) -> str:
        return urljoin(self.base_url + "/", "audio/voices")

    @property
    def models_url(self) -> str:
        return urljoin(self.base_url + "/", "models")

    def _service_root(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url[:-3].rstrip("/")
        return self.base_url.rstrip("/")

    @property
    def openapi_url(self) -> str:
        return urljoin(self._service_root() + "/", "openapi.json")

    @property
    def docs_url(self) -> str:
        return urljoin(self._service_root() + "/", "docs")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def health_check(self) -> str:
        attempts = [self.openapi_url, self.models_url, self.voices_url, self.docs_url]
        errors: List[str] = []
        for url in attempts:
            try:
                response = requests.get(url, timeout=self.timeout)
                if response.status_code < 400:
                    return url
                errors.append(f"{url} -> HTTP {response.status_code}: {_response_detail(response)}")
            except requests.RequestException as exc:
                errors.append(f"{url} -> {exc}")
        raise KokoroHealthError(
            "Kokoro health check failed. Tried: "
            + "; ".join(errors)
            + ". Check Docker network attachment, published port mapping, container name resolution, and the configured KOKORO_API_URL."
        )

    def fetch_openapi_schema(self) -> dict:
        response = requests.get(self.openapi_url, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def list_voices(self) -> List[str]:
        try:
            response = requests.get(self.voices_url, timeout=self.timeout)
            if response.status_code >= 400:
                raise KokoroVoiceError(f"HTTP {response.status_code}: {_response_detail(response)}")
            payload = response.json()
            voices = _coerce_voice_list(payload)
            if voices:
                return voices
        except (requests.RequestException, ValueError, KokoroVoiceError):
            pass
        # Fall back to documented voices plus OpenAI aliases. Keep this configurable via env/CLI.
        return sorted(set(DEFAULT_STATIC_VOICES) | set(OPENAI_VOICE_ALIASES.keys()))

    def voice_is_supported(self, voice: str) -> bool:
        voices = self.list_voices()
        if voice in voices:
            return True
        if voice in OPENAI_VOICE_ALIASES:
            return True
        return False

    def validate_voice(self, voice: str) -> None:
        if self.voice_is_supported(voice):
            return
        raise KokoroVoiceError(
            f"Voice '{voice}' was not listed by the Kokoro instance. Supported/documented voices: {', '.join(self.list_voices())}"
        )

    def synthesize(self, text: str, output_path: Path) -> KokoroResult:
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": "wav",
            "stream": True,
        }
        if self.speed is not None:
            payload["speed"] = self.speed

        attempts = max(1, len(self.retry_delays) + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    self.speech_url,
                    headers=self._headers(),
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                body = response.content

                if response.status_code >= 400:
                    last_error = KokoroError(
                        f"Kokoro returned HTTP {response.status_code}: {_response_detail(response)}"
                    )
                elif "json" in content_type or body.lstrip()[:1] in {b"{", b"["}:
                    last_error = KokoroError(
                        f"Kokoro returned JSON instead of audio: {_response_detail(response)}"
                    )
                elif (
                    content_type
                    and not content_type.startswith("audio/")
                    and content_type != "application/octet-stream"
                ):
                    last_error = KokoroError(
                        f"Unexpected Kokoro content-type '{content_type}' for audio response"
                    )
                else:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(body)
                    return KokoroResult(
                        content_type=content_type or "application/octet-stream",
                        path=output_path,
                        response_bytes=body,
                    )

            if attempt < attempts:
                time.sleep(self.retry_delays[attempt - 1])

        if isinstance(last_error, Exception):
            if isinstance(last_error, KokoroError):
                raise last_error
            raise KokoroError(f"Request to Kokoro failed: {last_error}") from last_error
        raise KokoroError("Request to Kokoro failed for an unknown reason")


def _coerce_voice_list(payload: object) -> List[str]:
    voices: List[str] = []
    if isinstance(payload, dict):
        raw = payload.get("voices")
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str):
                    voices.append(item)
                elif isinstance(item, dict):
                    for key in ("id", "name", "voice"):
                        value = item.get(key)
                        if isinstance(value, str) and value.strip():
                            voices.append(value.strip())
                            break
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                voices.append(item)
            elif isinstance(item, dict):
                for key in ("id", "name", "voice"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        voices.append(value.strip())
                        break
    return sorted({voice for voice in voices if voice})


def _response_detail(response: requests.Response) -> str:
    try:
        data = response.json()
    except Exception:
        text = response.text[:1000]
        return text or "<empty response>"
    return json.dumps(data, ensure_ascii=False)[:1000]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect a Kokoro instance.")
    parser.add_argument(
        "--api-url",
        default=os.getenv("KOKORO_API_URL", "http://127.0.0.1:8880/v1"),
        help="OpenAI-compatible Kokoro base URL",
    )
    parser.add_argument(
        "--api-key", default=os.getenv("KOKORO_API_KEY", "not-needed"), help="API key placeholder"
    )
    parser.add_argument(
        "--voice", default=os.getenv("KOKORO_VOICE", "af_bella"), help="Voice to validate"
    )
    parser.add_argument(
        "--timeout",
        default=float(os.getenv("STORYFORGE_KOKORO_TIMEOUT", 120.0)),
        type=float,
        help="Request timeout",
    )
    parser.add_argument(
        "--list-voices",
        action="store_true",
        help="List voices supported by the installed Kokoro instance",
    )
    parser.add_argument(
        "--show-schema", action="store_true", help="Fetch and summarize the OpenAPI speech schema"
    )
    return parser


def _summarize_speech_schema(schema: dict) -> dict:
    try:
        speech = schema["components"]["schemas"]["OpenAISpeechRequest"]
    except Exception as exc:
        raise KokoroError(
            f"OpenAPI schema does not expose components.schemas.OpenAISpeechRequest: {exc}"
        ) from exc
    properties = speech.get("properties", {}) if isinstance(speech, dict) else {}
    required = speech.get("required", []) if isinstance(speech, dict) else []
    return {
        "required": required,
        "model_default": properties.get("model", {}).get("default"),
        "voice_default": properties.get("voice", {}).get("default"),
        "response_format_enum": properties.get("response_format", {}).get("enum"),
        "speed_range": {
            "minimum": properties.get("speed", {}).get("minimum"),
            "maximum": properties.get("speed", {}).get("maximum"),
            "default": properties.get("speed", {}).get("default"),
        },
        "stream_default": properties.get("stream", {}).get("default"),
        "stream_description": properties.get("stream", {}).get("description"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    client = KokoroClient(
        base_url=args.api_url, api_key=args.api_key, voice=args.voice, timeout=args.timeout
    )

    if args.show_schema:
        schema = client.fetch_openapi_schema()
        print(json.dumps(_summarize_speech_schema(schema), indent=2, sort_keys=True))
        return 0

    if args.list_voices:
        voices = client.list_voices()
        print("\n".join(voices))
        return 0

    print(client.health_check())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
