from __future__ import annotations

import pytest

from app.packaging.backends.ffmpeg import FFmpegPackagingBackend


@pytest.mark.integration
def test_ffmpeg_backend_version_probe():
    backend = FFmpegPackagingBackend()
    if not backend.ffmpeg_path or not backend.ffprobe_path:
        pytest.skip("ffmpeg or ffprobe not available")

    info = backend.probe()

    assert info["backend_name"] == backend.backend_name
    assert info["backend_version"] == backend.backend_version
    assert info["encoder_name"] == backend.encoder_name
    assert info["encoder_version"] == backend.encoder_version
