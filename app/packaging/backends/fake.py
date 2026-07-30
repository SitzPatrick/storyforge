from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import PackagingBackendResult, PackagingRequest, PackagingValidationStatus
from ..serialization import canonical_json
from .base import PackagingBackend


@dataclass
class FakePackagingBackend(PackagingBackend):
    backend_name: str = "fake-packaging"
    backend_version: str = "1"
    encoder_name: str = "fake-encoder"
    encoder_version: str | None = "1"
    available: bool = True
    fail_on_package: bool = False
    corrupt_output: bool = False
    probe_override: dict[str, Any] | None = None
    package_calls: int = 0
    probe_calls: int = 0
    captured_requests: list[PackagingRequest] = field(default_factory=list)

    def is_available(self) -> bool:
        return self.available

    def package(self, request: PackagingRequest) -> PackagingBackendResult:
        self.package_calls += 1
        self.captured_requests.append(request)
        if self.fail_on_package:
            raise RuntimeError("fake backend encoding failure")
        payload = {
            "book_id": request.book_id,
            "package_id": request.package_id,
            "package_input_hash": request.package_input_hash,
            "chapter_ids": [chapter.chapter_id for chapter in request.chapter_inputs],
            "chapter_orders": [chapter.chapter_order for chapter in request.chapter_inputs],
            "chapter_timeline": request.chapter_timeline,
            "metadata": request.normalized_metadata,
            "cover_art": request.cover_art,
            "backend_name": self.backend_name,
            "backend_version": self.backend_version,
            "encoder_name": self.encoder_name,
            "encoder_version": self.encoder_version,
        }
        content = canonical_json(payload).encode("utf-8")
        if self.corrupt_output:
            content = content + b"\nCORRUPT"
        request.temp_output_path.parent.mkdir(parents=True, exist_ok=True)
        request.temp_output_path.write_bytes(content)
        output_hash = hashlib.sha256(content).hexdigest()
        return PackagingBackendResult(
            output_path=request.temp_output_path,
            output_artifact_relative_path=request.output_artifact_relative_path,
            output_container=request.config.container_format,
            audio_codec=request.config.audio_codec,
            audio_bitrate_kbps=request.config.audio_bitrate_kbps,
            sample_rate_hz=request.config.sample_rate_hz,
            channel_count=request.config.channel_count,
            duration_seconds=sum(chapter.duration_seconds for chapter in request.chapter_inputs),
            chapter_count=len(request.chapter_inputs),
            chapter_probe_data=tuple({
                "chapter_id": chapter.chapter_id,
                "chapter_order": chapter.chapter_order,
                "mastered_chapter_id": chapter.mastered_chapter_id,
                "duration_seconds": chapter.duration_seconds,
            } for chapter in request.chapter_inputs),
            metadata_probe_data={"title": request.normalized_metadata.title},
            cover_art_probe_state=None if request.cover_art is None else {"enabled": request.cover_art.enabled, "expected_embedded": request.cover_art.expected_embedded},
            backend_name=self.backend_name,
            backend_version=self.backend_version,
            encoder_name=self.encoder_name,
            encoder_version=self.encoder_version,
            file_size=request.temp_output_path.stat().st_size,
            audio_content_hash=output_hash,
            validation_result=PackagingValidationStatus.PASSED,
            warnings=(),
            errors=(),
            probe_data={"fake": True},
        )

    def probe(self, path: Path | None = None) -> dict[str, Any]:
        self.probe_calls += 1
        if self.probe_override is not None:
            return dict(self.probe_override)
        if path is None:
            return {
                "output_path": None,
                "output_container": "m4b",
                "audio_codec": "aac",
                "audio_bitrate_kbps": 96,
                "sample_rate_hz": 24000,
                "channel_count": 1,
                "duration_seconds": 0.0,
                "chapter_count": 0,
                "chapter_probe_data": [],
                "metadata_probe_data": {},
                "cover_art_probe_state": None,
                "backend_name": self.backend_name,
                "backend_version": self.backend_version,
                "encoder_name": self.encoder_name,
                "encoder_version": self.encoder_version,
                "file_size": 0,
                "audio_content_hash": None,
                "validation_result": "passed",
                "warnings": [],
                "errors": [],
            }
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            decoded = {}
        chapter_timeline = decoded.get("chapter_timeline", []) if isinstance(decoded, dict) else []
        chapter_count = len(chapter_timeline) if isinstance(chapter_timeline, list) else 0
        duration_seconds = float(sum(float(item.get("duration_ticks", 0)) for item in chapter_timeline)) / 1_000_000 if chapter_timeline else 0.0
        return {
            "output_path": str(path),
            "output_container": "m4b",
            "audio_codec": "aac",
            "audio_bitrate_kbps": 96,
            "sample_rate_hz": 24000,
            "channel_count": 1,
            "duration_seconds": duration_seconds,
            "chapter_count": chapter_count,
            "chapter_probe_data": chapter_timeline,
            "metadata_probe_data": decoded.get("metadata", {}) if isinstance(decoded, dict) else {},
            "cover_art_probe_state": None if not isinstance(decoded, dict) else decoded.get("cover_art"),
            "backend_name": self.backend_name,
            "backend_version": self.backend_version,
            "encoder_name": self.encoder_name,
            "encoder_version": self.encoder_version,
            "file_size": path.stat().st_size,
            "audio_content_hash": digest,
            "validation_result": "passed",
            "warnings": [],
            "errors": [],
        }
