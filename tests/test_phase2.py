from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

import pytest
from ebooklib import epub

from app.audio import probe_audio
from app.config import load_settings
from app.epub_utils import extract_book_metadata, extract_cover_image, list_chapters, read_epub
from app.kokoro_client import KokoroClient
from app.manifest import ConversionManifest, load_manifest, save_manifest
from app.m4b import create_m4b
from app.runner import BookConversionRunner


def make_cover(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def make_test_epub(path: Path, chapter_count: int = 3) -> Path:
    book = epub.EpubBook()
    book.set_identifier("storyforge-phase2-test")
    book.set_title("Phase 2 Test Book")
    book.set_language("en")
    book.add_author("Storyforge Test Author")
    book.add_metadata("DC", "date", "2024-01-02")

    book.add_metadata("DC", "description", "A synthetic EPUB used to validate Storyforge Phase 2.")
    book.add_metadata("DC", "publisher", "Storyforge Press")
    book.add_metadata("DC", "language", "en")
    book.add_metadata("DC", "relation", "Series: Synthetic Saga")

    cover_bytes = make_cover(path.parent / "cover.jpg").read_bytes()
    cover_item = epub.EpubItem(uid="cover-image", file_name="images/cover.jpg", media_type="image/jpeg", content=cover_bytes)
    book.add_item(cover_item)

    chapters = []
    for idx in range(1, chapter_count + 1):
        item = epub.EpubHtml(title=f"Chapter {idx}", file_name=f"chap_{idx:02d}.xhtml", lang="en")
        item.content = f"""
        <html><body>
        <h1>Chapter {idx}</h1>
        <p>This is the body of chapter {idx}. It exists to prove ordering and conversion.</p>
        <p>More words for chapter {idx} so chunking and metadata are realistic.</p>
        </body></html>
        """
        book.add_item(item)
        chapters.append(item)

    front = epub.EpubHtml(title="Title Page", file_name="titlepage.xhtml", lang="en")
    front.content = "<html><body><h1>Title Page</h1><p>Front matter only.</p></body></html>"
    book.add_item(front)

    book.toc = tuple(epub.Link(ch.file_name, ch.title, f"id{idx}") for idx, ch in enumerate(chapters, start=1))
    book.spine = ["nav", front, *chapters]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)
    return path


def make_wav(path: Path, duration: float = 0.25, frequency: float = 440.0, sample_rate: int = 24000) -> Path:
    import math
    import struct

    nframes = int(duration * sample_rate)
    amplitude = 16000
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(nframes):
            sample = int(amplitude * math.sin(2 * math.pi * frequency * (i / sample_rate)))
            wf.writeframes(struct.pack("<h", sample))
    return path


def test_epub_metadata_chapter_order_and_cover(tmp_path: Path):
    epub_path = make_test_epub(tmp_path / "sample.epub", chapter_count=3)
    book = read_epub(epub_path)

    metadata = extract_book_metadata(book)
    assert metadata.title == "Phase 2 Test Book"
    assert metadata.authors == ["Storyforge Test Author"]
    assert metadata.publisher == "Storyforge Press"
    assert metadata.language == "en"
    assert metadata.description.startswith("A synthetic EPUB")

    cover = extract_cover_image(book)
    assert cover is not None
    assert cover.filename.endswith("cover.jpg")
    assert cover.data.startswith(b"\xff\xd8")

    chapters = list_chapters(book)
    assert [chapter.title for chapter in chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert [chapter.number for chapter in chapters] == [1, 2, 3]


def test_manifest_roundtrip_and_resume_state(tmp_path: Path):
    manifest_path = tmp_path / "manifest.json"
    manifest = ConversionManifest.create(
        title="Phase 2 Test Book",
        author="Storyforge Test Author",
        chapters_total=3,
        voice="af_heart",
        speed=1.0,
        output_directory=str(tmp_path / "book"),
        source_epub=str(tmp_path / "sample.epub"),
    )
    manifest.mark_chapter_completed(1, title="Chapter 1", words=1000, chunks=4, duration_seconds=10.5, generation_seconds=1.2)
    save_manifest(manifest, manifest_path)

    loaded = load_manifest(manifest_path)
    assert loaded.title == manifest.title
    assert loaded.completed_chapters == [1]
    assert loaded.status == "running"
    assert loaded.should_process(1) is False
    assert loaded.should_process(2) is True


def test_kokoro_client_retries_failed_request(monkeypatch, tmp_path: Path):
    attempts = []

    class FakeResponse:
        def __init__(self, status_code: int, content: bytes, content_type: str = "audio/wav"):
            self.status_code = status_code
            self.content = content
            self.headers = {"content-type": content_type}

        def json(self):
            return json.loads(self.content.decode("utf-8"))

        @property
        def text(self):
            return self.content.decode("utf-8", errors="ignore")

    def fake_post(url, headers=None, json=None, timeout=None):
        attempts.append(json)
        if len(attempts) < 3:
            return FakeResponse(503, b"service unavailable", content_type="text/plain")
        return FakeResponse(200, make_wav(tmp_path / "payload.wav").read_bytes())

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    client = KokoroClient("http://example.com/v1", retry_delays=(0, 0, 0))
    out = tmp_path / "speech.wav"
    result = client.synthesize("hello world", out)
    assert result.path == out
    assert out.exists()
    assert len(attempts) == 3


def test_create_m4b_embeds_chapters_and_cover(tmp_path: Path):
    chapter1 = make_wav(tmp_path / "chapter1.wav", frequency=440.0)
    chapter2 = make_wav(tmp_path / "chapter2.wav", frequency=660.0)
    cover = make_cover(tmp_path / "cover.jpg")

    output = tmp_path / "Book Name.m4b"
    metadata = {
        "title": "Book Name",
        "author": "Storyforge Test Author",
        "album": "Book Name",
        "year": "2024",
        "language": "en",
    }
    chapters = [
        {"chapter": 1, "title": "Chapter One", "duration_seconds": probe_audio(chapter1)["duration"]},
        {"chapter": 2, "title": "Chapter Two", "duration_seconds": probe_audio(chapter2)["duration"]},
    ]

    create_m4b([chapter1, chapter2], output, metadata=metadata, chapters=chapters, cover_path=cover, bitrate="96k")
    assert output.exists()

    info = probe_audio(output)
    assert info["codec_name"] == "aac"

    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_chapters", "-print_format", "json", str(output)],
        capture_output=True,
        text=True,
        check=True,
    )
    chapter_data = json.loads(probe.stdout)
    assert len(chapter_data.get("chapters", [])) == 2


def test_runner_resumes_and_retries_without_regenerating_completed_chapters(tmp_path: Path, monkeypatch):
    epub_path = make_test_epub(tmp_path / "sample.epub", chapter_count=3)
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    log_dir = tmp_path / "logs"
    settings = load_settings(None)
    settings.paths.output_dir = output_dir
    settings.paths.temp_dir = temp_dir
    settings.paths.log_dir = log_dir
    settings.kokoro.voice = "af_heart"
    settings.kokoro.speed = 1.0
    settings.kokoro.retry_delays = [0, 0, 0]
    settings.conversion.chunk_chars = 4000

    calls = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            self.voice = kwargs.get("voice", "af_heart")

        def health_check(self):
            return "openapi.json"

        def validate_voice(self, voice):
            return None

        def synthesize(self, text, output_path):
            chapter_marker = "Chapter 2" if "chapter 2" in text.lower() else ("Chapter 3" if "chapter 3" in text.lower() else "Chapter 1")
            calls.append(chapter_marker)
            if chapter_marker == "Chapter 2" and calls.count("Chapter 2") == 1:
                raise RuntimeError("temporary Kokoro failure")
            make_wav(output_path, duration=0.1)
            return output_path

    monkeypatch.setattr("app.runner.KokoroClient", FakeClient)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    runner = BookConversionRunner(settings)
    first = runner.run_book(epub_path)
    assert first.status == "failed"
    assert first.chapters_completed == 2
    assert first.failed_chapters == [2]

    second = runner.run_book(epub_path)
    assert second.status == "complete"
    assert second.chapters_completed == 3
    assert second.failed_chapters == []
    assert calls.count("Chapter 1") == 1
    assert calls.count("Chapter 2") == 2  # first run failed, resume regenerated
    assert calls.count("Chapter 3") == 1

    book_dir = output_dir / "Phase 2 Test Book"
    assert (book_dir / "Chapter 001.wav").exists()
    assert (book_dir / "Chapter 002.wav").exists()
    assert (book_dir / "Chapter 003.wav").exists()
    assert (book_dir / "Phase 2 Test Book.m4b").exists()
    assert json.loads((book_dir / "manifest.json").read_text())["status"] == "complete"
