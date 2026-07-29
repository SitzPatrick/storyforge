from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from ebooklib import epub

from app.config import load_settings
from app.story_analysis import LLMProvider


class FakeAnalysisProvider(LLMProvider):
    def __init__(self):
        self.calls = Counter()

    def analyze_text(self, text: str):
        self.calls["analyze_text"] += 1
        return {
            "summary": "A brief chapter summary.",
            "characters": ["Ada", "Ben"],
            "locations": ["River City"],
            "organizations": ["Guild"],
        }

    def summarize_scene(self, text: str):
        self.calls["summarize_scene"] += 1
        return {
            "summary": "Ada and Ben meet in River City.",
            "characters": ["Ada", "Ben"],
            "locations": ["River City"],
        }

    def extract_entities(self, text: str):
        self.calls["extract_entities"] += 1
        return {
            "characters": [
                {"name": "Ada", "aliases": ["Ad"], "gender": "female", "role": "protagonist"},
                {"name": "Ben", "aliases": [], "gender": "male", "role": "supporting"},
            ],
            "places": [
                {"name": "River City", "aliases": ["the city"]},
                {"name": "Tower", "aliases": []},
            ],
            "organizations": [
                {"name": "Guild", "aliases": ["the Guild"]},
            ],
        }


class LengthLimitedProvider(FakeAnalysisProvider):
    def __init__(self, max_input_chars: int = 1000):
        super().__init__()
        self.max_input_chars = max_input_chars

    def _check_length(self, text: str):
        if len(text) > self.max_input_chars:
            raise AssertionError(f"input too long: {len(text)} > {self.max_input_chars}")

    def analyze_text(self, text: str):
        self._check_length(text)
        return super().analyze_text(text)

    def summarize_scene(self, text: str):
        self._check_length(text)
        return super().summarize_scene(text)

    def extract_entities(self, text: str):
        self._check_length(text)
        return super().extract_entities(text)


class CountingProvider(LengthLimitedProvider):
    def __init__(self):
        super().__init__(max_input_chars=10_000_000)
        self.fail_after_first_run = False

    def extract_entities(self, text: str):
        self.calls["extract_entities"] += 1
        if self.fail_after_first_run:
            raise AssertionError("cache was not reused")
        return super().extract_entities(text)

    def summarize_scene(self, text: str):
        self.calls["summarize_scene"] += 1
        if self.fail_after_first_run:
            raise AssertionError("cache was not reused")
        return super().summarize_scene(text)

    def analyze_text(self, text: str):
        self.calls["analyze_text"] += 1
        if self.fail_after_first_run:
            raise AssertionError("cache was not reused")
        return super().analyze_text(text)


def make_story_epub(path: Path, *, include_metadata: bool = True) -> Path:
    book = epub.EpubBook()
    book.set_identifier("storyforge-story-analysis-test")
    if include_metadata:
        book.set_title("River City Nights")
        book.set_language("en")
        book.add_author("Test Author")
        book.add_metadata("DC", "description", "A test novel about Ada and Ben.")
        book.add_metadata("DC", "publisher", "Test Press")
        book.add_metadata("DC", "date", "2024-01-02")

    ch1 = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
    ch1.content = """
    <html><body>
    <h1>Chapter 1</h1>
    <p>Ada walked into River City. The Guild watched from the Tower.</p>
    <p>"We should go now," Ada said. Ben shook his head and replied, "Not yet."</p>
    <p>They were waiting for the night bell to ring.</p>
    </body></html>
    """

    ch2 = epub.EpubHtml(title="Chapter 2", file_name="chap_02.xhtml", lang="en")
    ch2.content = """
    <html><body>
    <h1>Chapter 2</h1>
    <p>At dawn, Ada and Ben returned to River City to meet the Guild.</p>
    <p>"This is the place," Ben whispered.</p>
    <p>Then the Tower gates opened.</p>
    </body></html>
    """

    front = epub.EpubHtml(title="Title Page", file_name="title.xhtml", lang="en")
    front.content = "<html><body><h1>Title Page</h1><p>Front matter.</p></body></html>"

    book.add_item(front)
    book.add_item(ch1)
    book.add_item(ch2)
    book.toc = (epub.Link(ch1.file_name, ch1.title, "c1"), epub.Link(ch2.file_name, ch2.title, "c2"))
    book.spine = ["nav", front, ch1, ch2]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(path), book)
    return path


def make_empty_metadata_epub(path: Path) -> Path:
    return make_story_epub(path, include_metadata=False)


@pytest.fixture()
def analysis_settings(tmp_path: Path):
    settings = load_settings(None)
    settings.paths.output_dir = tmp_path / "output"
    settings.paths.temp_dir = tmp_path / "temp"
    settings.paths.log_dir = tmp_path / "logs"
    settings.analysis.cache_enabled = True
    settings.analysis.cache_directory = "analysis"
    settings.analysis.analysis_chunk_size = 160
    settings.analysis.llm_provider = "ollama"
    settings.analysis.ollama_url = "http://127.0.0.1:11434"
    settings.analysis.ollama_model = "llama3.1"
    return settings


def test_story_analyzer_extracts_entities_dialogue_and_scenes(tmp_path: Path, analysis_settings):
    from app.story_analysis import StoryAnalyzer

    epub_path = make_story_epub(tmp_path / "story.epub")
    provider = FakeAnalysisProvider()
    analyzer = StoryAnalyzer(analysis_settings, provider=provider)

    result = analyzer.analyze(epub_path)

    assert result.cache_hit is False
    assert result.story["title"] == "River City Nights"
    assert result.story["author"] == "Test Author"
    assert result.story["language"] == "en"
    assert len(result.story["characters"]) >= 2
    assert any(char["name"] == "Ada" for char in result.story["characters"])
    assert any(place["name"] == "River City" for place in result.story["places"])
    assert any(org["name"] == "Guild" for org in result.story["organizations"])
    assert len(result.story["chapters"]) == 2
    assert len(result.scenes) >= 2
    assert len(result.dialogue) >= 2
    assert any(seg["speaker"] == "Ada" for seg in result.dialogue)
    assert any(seg["speaker"] == "Ben" for seg in result.dialogue)
    assert len(result.story["narration_paragraphs"]) > 0
    assert provider.calls["extract_entities"] > 0
    assert provider.calls["summarize_scene"] > 0
    assert provider.calls["analyze_text"] > 0

    analysis_dir = analysis_settings.paths.output_dir / "River City Nights" / "analysis"
    for name in ["story.json", "entities.json", "scenes.json", "dialogue.json"]:
        assert (analysis_dir / name).exists()

    loaded_story = json.loads((analysis_dir / "story.json").read_text())
    assert loaded_story["title"] == "River City Nights"


def test_story_analyzer_reuses_cache_without_reinvoking_provider(tmp_path: Path, analysis_settings):
    from app.story_analysis import StoryAnalyzer

    epub_path = make_story_epub(tmp_path / "story.epub")
    provider = CountingProvider()
    analyzer = StoryAnalyzer(analysis_settings, provider=provider)

    first = analyzer.analyze(epub_path)
    assert first.cache_hit is False
    assert provider.calls["extract_entities"] > 0

    provider.fail_after_first_run = True
    second = analyzer.analyze(epub_path)
    assert second.cache_hit is True
    assert second.story["title"] == first.story["title"]


def test_story_analyzer_handles_long_text_with_length_limited_provider(tmp_path: Path, analysis_settings):
    from app.story_analysis import StoryAnalyzer

    # Build a longer chapter to prove the analyzer trims the model-facing inputs.
    epub_path = tmp_path / "long-story.epub"
    book = epub.EpubBook()
    book.set_identifier("storyforge-long-analysis-test")
    book.set_title("Long Story")
    book.set_language("en")
    book.add_author("Test Author")

    paragraphs = []
    for idx in range(1, 40):
        paragraphs.append(f"<p>Chapter paragraph {idx}. \"We must go,\" Ada said. Ben answered from River City and the Guild watched from the Tower.</p>")
    chapter = epub.EpubHtml(title="Chapter 1", file_name="chap_01.xhtml", lang="en")
    chapter.content = "<html><body><h1>Chapter 1</h1>" + "\n".join(paragraphs) + "</body></html>"
    book.add_item(chapter)
    book.toc = [epub.Link(chapter.file_name, chapter.title, "c1")]
    book.spine = ["nav", chapter]
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(str(epub_path), book)

    analyzer = StoryAnalyzer(analysis_settings, provider=LengthLimitedProvider(max_input_chars=1000))
    result = analyzer.analyze(epub_path)

    assert result.cache_hit is False
    assert len(result.story["chapters"]) == 1
    assert len(result.scenes) > 0
    assert len(result.dialogue) > 0


def test_story_analyzer_handles_malformed_epub(tmp_path: Path, analysis_settings):
    from app.story_analysis import BookAnalysisError, StoryAnalyzer

    bad_epub = tmp_path / "broken.epub"
    bad_epub.write_text("this is not an epub", encoding="utf-8")
    analyzer = StoryAnalyzer(analysis_settings, provider=FakeAnalysisProvider())

    with pytest.raises(BookAnalysisError):
        analyzer.analyze(bad_epub)


def test_story_analyzer_handles_missing_metadata(tmp_path: Path, analysis_settings):
    from app.story_analysis import StoryAnalyzer

    epub_path = make_empty_metadata_epub(tmp_path / "untitled.epub")
    analyzer = StoryAnalyzer(analysis_settings, provider=FakeAnalysisProvider())

    result = analyzer.analyze(epub_path)
    assert result.story["title"]
    assert result.story["language"] in {"", "en"}
    assert result.story["author"] in {"", "Test Author"}


def test_story_schema_validation(tmp_path: Path, analysis_settings):
    from app.story_analysis import StoryAnalyzer, validate_story_model

    epub_path = make_story_epub(tmp_path / "story.epub")
    analyzer = StoryAnalyzer(analysis_settings, provider=FakeAnalysisProvider())
    result = analyzer.analyze(epub_path)

    validate_story_model(result.story)
    assert isinstance(result.entities["characters"], list)
    assert isinstance(result.scenes, list)
    assert isinstance(result.dialogue, list)
