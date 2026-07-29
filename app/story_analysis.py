from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests

from .config import AnalysisSettings, StoryforgeSettings
from .epub_utils import extract_chapter_html_text, extract_book_metadata, get_chapter_document, list_chapters, read_epub

ANALYSIS_VERSION = 1
_DIALOGUE_VERBS = ("said", "asked", "replied", "whispered", "shouted", "murmured", "called", "added", "answered")
_QUOTE_RE = re.compile(r"[\"“](.+?)[\"”]")
_PARA_SPLIT_RE = re.compile(r"\n\s*\n+")


class BookAnalysisError(RuntimeError):
    pass


class LLMProvider(ABC):
    @abstractmethod
    def analyze_text(self, text: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def summarize_scene(self, text: str) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def extract_entities(self, text: str) -> dict[str, Any]:
        raise NotImplementedError


class OllamaProvider(LLMProvider):
    def __init__(self, base_url: str, model: str, timeout: float = 120.0, logger: logging.Logger | None = None, retries: int = 2, retry_delay: float = 1.5) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.logger = logger or logging.getLogger(__name__)
        self.retries = max(0, int(retries))
        self.retry_delay = float(retry_delay)

    def analyze_text(self, text: str) -> dict[str, Any]:
        return self._chat_json(
            "You analyze book text and return JSON only. Provide a brief summary and any useful high-level story signals.",
            text,
            label="analyze_text",
        )

    def summarize_scene(self, text: str) -> dict[str, Any]:
        return self._chat_json(
            "You summarize a single scene from a novel and return JSON only with keys summary, characters, and locations.",
            text,
            label="summarize_scene",
        )

    def extract_entities(self, text: str) -> dict[str, Any]:
        return self._chat_json(
            "You extract recurring story entities from EPUB prose and return JSON only with keys characters, places, and organizations.",
            text,
            label="extract_entities",
        )

    def _chat_json(self, system_prompt: str, user_text: str, label: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "stream": False,
        }
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            started = time.perf_counter()
            try:
                self.logger.info("ollama request start label=%s attempt=%s/%s model=%s url=%s", label, attempt + 1, self.retries + 1, self.model, self.base_url)
                response = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                content = ""
                if isinstance(data, dict):
                    message = data.get("message") or {}
                    if isinstance(message, dict):
                        content = str(message.get("content") or "")
                    elif isinstance(data.get("response"), str):
                        content = str(data.get("response") or "")
                if not content:
                    raise BookAnalysisError("Ollama returned an empty response")
                parsed = _coerce_json(content)
                if not isinstance(parsed, dict):
                    raise BookAnalysisError("Ollama response was not a JSON object")
                elapsed = time.perf_counter() - started
                self.logger.info("ollama request complete label=%s attempt=%s elapsed=%.2fs", label, attempt + 1, elapsed)
                return parsed
            except Exception as exc:  # pragma: no cover - network dependent
                elapsed = time.perf_counter() - started
                last_error = exc
                self.logger.warning("ollama request failed label=%s attempt=%s elapsed=%.2fs error=%s", label, attempt + 1, elapsed, exc)
                if attempt < self.retries:
                    time.sleep(self.retry_delay * (attempt + 1))
                    continue
                break
        raise BookAnalysisError(f"Ollama request failed after {self.retries + 1} attempt(s): {last_error}")


class HeuristicProvider(LLMProvider):
    def analyze_text(self, text: str) -> dict[str, Any]:
        return {
            "summary": _summarize_text(text),
            "characters": [record["name"] for record in _heuristic_entities(text)["characters"]],
            "locations": [record["name"] for record in _heuristic_entities(text)["places"]],
            "organizations": [record["name"] for record in _heuristic_entities(text)["organizations"]],
        }

    def summarize_scene(self, text: str) -> dict[str, Any]:
        payload = _heuristic_entities(text)
        return {
            "summary": _summarize_text(text),
            "characters": [record["name"] for record in payload["characters"]],
            "locations": [record["name"] for record in payload["places"]],
        }

    def extract_entities(self, text: str) -> dict[str, Any]:
        return _heuristic_entities(text)


@dataclass
class StoryAnalysisResult:
    story: dict[str, Any]
    entities: dict[str, Any]
    scenes: list[dict[str, Any]]
    dialogue: list[dict[str, Any]]
    analysis_dir: Path
    cache_hit: bool
    processing_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "story": self.story,
            "entities": self.entities,
            "scenes": self.scenes,
            "dialogue": self.dialogue,
            "analysis_dir": str(self.analysis_dir),
            "cache_hit": self.cache_hit,
            "processing_seconds": self.processing_seconds,
        }


@dataclass
class AnalysisPlan:
    epub_path: Path
    book_output_dir: Path
    analysis_dir: Path
    metadata: Any
    chapters: list[Any]
    book: Any
    source_signature: dict[str, Any]
    config_signature: dict[str, Any]
    source_hash: str
    config_hash: str
    cache_hit: bool
    cached_payload: dict[str, Any] | None


class StoryAnalyzer:
    def __init__(self, settings: StoryforgeSettings, provider: LLMProvider | None = None) -> None:
        self.settings = settings
        self.logger = logging.getLogger("storyforge.analysis")
        self.provider = provider or self._build_provider(settings.analysis)

    def prepare(self, epub_path: Path | str) -> AnalysisPlan:
        epub_path = Path(epub_path)
        try:
            book = read_epub(epub_path)
        except (zipfile.BadZipFile, FileNotFoundError, OSError, Exception) as exc:
            raise BookAnalysisError(f"Unable to read EPUB {epub_path}: {exc}") from exc

        metadata = extract_book_metadata(book)
        chapters = list_chapters(book)
        if not chapters:
            raise BookAnalysisError(f"No readable chapters were found in {epub_path}")

        book_output_dir = self.settings.paths.output_dir / _safe_name(metadata.title)
        analysis_dir = self._analysis_dir(book_output_dir)
        source_signature = _file_signature(epub_path)
        config_signature = _analysis_signature(self.settings.analysis)
        source_hash = source_signature["sha256"]
        config_hash = _signature_hash(config_signature)

        cached_payload = None
        cache_hit = False
        if self.settings.analysis.cache_enabled:
            cached_payload = self._load_cache(analysis_dir, source_signature, config_signature)
            cache_hit = cached_payload is not None

        return AnalysisPlan(
            epub_path=epub_path,
            book_output_dir=book_output_dir,
            analysis_dir=analysis_dir,
            metadata=metadata,
            chapters=chapters,
            book=book,
            source_signature=source_signature,
            config_signature=config_signature,
            source_hash=source_hash,
            config_hash=config_hash,
            cache_hit=cache_hit,
            cached_payload=cached_payload,
        )

    def analyze(self, epub_path: Path | str) -> StoryAnalysisResult:
        started = time.perf_counter()
        plan = self.prepare(epub_path)
        if plan.cache_hit and plan.cached_payload is not None:
            self.logger.info("analysis cache hit path=%s", plan.analysis_dir)
            return StoryAnalysisResult(
                story=plan.cached_payload["story"],
                entities=plan.cached_payload["entities"],
                scenes=plan.cached_payload["scenes"],
                dialogue=plan.cached_payload["dialogue"],
                analysis_dir=plan.analysis_dir,
                cache_hit=True,
                processing_seconds=0.0,
            )

        plan.analysis_dir.mkdir(parents=True, exist_ok=True)
        story, entities, scenes, dialogue, cache_payload = self._analyze_book(
            plan.book,
            plan.chapters,
            plan.metadata,
            plan.epub_path,
            plan.source_signature,
            plan.config_signature,
            plan.source_hash,
            plan.config_hash,
        )
        validate_story_model(story)

        _write_json(plan.analysis_dir / "story.json", story)
        _write_json(plan.analysis_dir / "entities.json", entities)
        _write_json(plan.analysis_dir / "scenes.json", scenes)
        _write_json(plan.analysis_dir / "dialogue.json", dialogue)
        _write_json(plan.analysis_dir / "cache.json", cache_payload)
        self.logger.info("analysis cache write complete path=%s", plan.analysis_dir)

        processing_seconds = time.perf_counter() - started
        return StoryAnalysisResult(
            story=story,
            entities=entities,
            scenes=scenes,
            dialogue=dialogue,
            analysis_dir=plan.analysis_dir,
            cache_hit=False,
            processing_seconds=processing_seconds,
        )

    def _build_provider(self, analysis: AnalysisSettings) -> LLMProvider:
        provider_name = (analysis.llm_provider or "ollama").strip().lower()
        if provider_name == "ollama":
            return OllamaProvider(analysis.ollama_url, analysis.ollama_model, logger=self.logger)
        if provider_name == "heuristic":
            return HeuristicProvider()
        raise BookAnalysisError(f"Unsupported llm_provider: {analysis.llm_provider!r}")

    def _analysis_dir(self, book_output_dir: Path) -> Path:
        cache_dir = Path(self.settings.analysis.cache_directory)
        return cache_dir if cache_dir.is_absolute() else book_output_dir / cache_dir

    def _load_cache(self, analysis_dir: Path, source_signature: dict[str, Any], config_signature: dict[str, Any]) -> dict[str, Any] | None:
        cache_file = analysis_dir / "cache.json"
        story_file = analysis_dir / "story.json"
        entities_file = analysis_dir / "entities.json"
        scenes_file = analysis_dir / "scenes.json"
        dialogue_file = analysis_dir / "dialogue.json"
        if not all(path.exists() for path in [cache_file, story_file, entities_file, scenes_file, dialogue_file]):
            return None
        try:
            cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
            if not isinstance(cache_data, dict):
                return None
            if cache_data.get("analysis_version") != ANALYSIS_VERSION:
                return None
            if cache_data.get("source_hash") != source_signature.get("sha256"):
                return None
            if cache_data.get("config_hash") != _signature_hash(config_signature):
                return None
            if cache_data.get("provider") != self.settings.analysis.llm_provider:
                return None
            if cache_data.get("model") != self.settings.analysis.ollama_model:
                return None
            if cache_data.get("chunk_size") != self.settings.analysis.analysis_chunk_size:
                return None
            story = json.loads(story_file.read_text(encoding="utf-8"))
            entities = json.loads(entities_file.read_text(encoding="utf-8"))
            scenes = json.loads(scenes_file.read_text(encoding="utf-8"))
            dialogue = json.loads(dialogue_file.read_text(encoding="utf-8"))
            validate_story_model(story)
            return {"story": story, "entities": entities, "scenes": scenes, "dialogue": dialogue}
        except Exception:
            return None

    def _analyze_book(
        self,
        book,
        chapters,
        metadata,
        epub_path: Path,
        source_signature: dict[str, Any],
        config_signature: dict[str, Any],
        source_document_id: str,
        config_hash: str,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        character_index: dict[str, dict[str, Any]] = {}
        place_index: dict[str, dict[str, Any]] = {}
        org_index: dict[str, dict[str, Any]] = {}
        scenes: list[dict[str, Any]] = []
        dialogue: list[dict[str, Any]] = []
        narration_paragraphs: list[dict[str, Any]] = []
        chapter_records: list[dict[str, Any]] = []

        for chapter_entry in chapters:
            chapter_started = time.perf_counter()
            self.logger.info("analysis chapter start chapter=%s title=%s", chapter_entry.number, chapter_entry.display_title)
            _, chapter_item = get_chapter_document(book, chapter_entry.number, chapters)
            chapter_text = extract_chapter_html_text(chapter_item.get_content())
            paragraphs = _split_paragraphs(chapter_text)
            chapter_summary = self._safe_provider_call("analyze_text", chapter_text)
            chapter_entities = self._safe_provider_call("extract_entities", chapter_text)
            _merge_entity_payload(character_index, place_index, org_index, chapter_entities, chapter_entry.number)

            chapter_scenes: list[dict[str, Any]] = []
            scene_chunks = _chunk_paragraphs(paragraphs, self.settings.analysis.analysis_chunk_size)
            for scene_number, chunk in enumerate(scene_chunks, start=1):
                self.logger.info("analysis chunk start chapter=%s chunk=%s/%s", chapter_entry.number, scene_number, len(scene_chunks))
                scene_text = "\n\n".join(item["text"] for item in chunk)
                scene_info = self._safe_provider_call("summarize_scene", scene_text)
                chunk_entities = self._safe_provider_call("extract_entities", scene_text)
                _merge_entity_payload(character_index, place_index, org_index, chunk_entities, chapter_entry.number)

                scene = _build_scene_record(
                    chapter_number=chapter_entry.number,
                    scene_number=scene_number,
                    chunk=chunk,
                    text=scene_text,
                    scene_info=scene_info,
                    characters=list(character_index.values()),
                    places=list(place_index.values()),
                    organizations=list(org_index.values()),
                    source_document_id=source_document_id,
                )
                scenes.append(scene)
                chapter_scenes.append(scene)

            chapter_dialogue, chapter_narration = _detect_dialogue_and_narration(paragraphs, character_index, chapter_entry.number, source_document_id)
            dialogue.extend(chapter_dialogue)
            narration_paragraphs.extend(chapter_narration)
            _update_mentions(character_index, place_index, org_index, chapter_dialogue, chapter_narration, chapter_text, chapter_entry.number, source_document_id)

            chapter_elapsed = time.perf_counter() - chapter_started
            self.logger.info("analysis chapter complete chapter=%s elapsed=%.2fs scenes=%s", chapter_entry.number, chapter_elapsed, len(chapter_scenes))

            chapter_records.append(
                {
                    "number": chapter_entry.number,
                    "title": chapter_entry.display_title,
                    "paragraph_count": len(paragraphs),
                    "scene_count": len(chapter_scenes),
                    "summary": _first_text(chapter_summary.get("summary") if isinstance(chapter_summary, dict) else None) or _summarize_text(chapter_text),
                    "source_document_id": source_document_id,
                    "source_text_hash": _text_hash(chapter_text),
                    "source_reference": {
                        "chapter": chapter_entry.number,
                        "paragraph_index": 1 if paragraphs else None,
                        "source_document_id": source_document_id,
                        "source_text_hash": _text_hash(chapter_text),
                        "excerpt": _excerpt(chapter_text),
                    },
                }
            )

        characters = [_finalize_entity(record, "character") for record in _sorted_entities(character_index)]
        places = [_finalize_entity(record, "place") for record in _sorted_entities(place_index)]
        organizations = [_finalize_entity(record, "organization") for record in _sorted_entities(org_index)]

        entities = {
            "characters": characters,
            "places": places,
            "organizations": organizations,
        }
        story = {
            "title": metadata.title or epub_path.stem,
            "author": ", ".join(metadata.authors) or "",
            "language": metadata.language or "",
            "characters": characters,
            "places": places,
            "organizations": organizations,
            "chapters": chapter_records,
            "scenes": scenes,
            "narration_paragraphs": narration_paragraphs,
            "source_document_id": source_document_id,
            "source_signature": source_signature,
            "config_hash": config_hash,
            "config_signature": config_signature,
        }
        cache_payload = {
            "analysis_version": ANALYSIS_VERSION,
            "source_epub": str(epub_path),
            "source_signature": source_signature,
            "config_signature": config_signature,
            "source_hash": source_document_id,
            "config_hash": config_hash,
            "provider": self.settings.analysis.llm_provider,
            "model": self.settings.analysis.ollama_model,
            "chunk_size": self.settings.analysis.analysis_chunk_size,
        }
        return story, entities, scenes, dialogue, cache_payload

    def _safe_provider_call(self, method_name: str, text: str) -> dict[str, Any]:
        method = getattr(self.provider, method_name)
        try:
            result = method(text)
        except Exception as exc:
            raise BookAnalysisError(f"{method_name} failed: {exc}") from exc
        if result is None:
            return {}
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            parsed = _coerce_json(result)
            if isinstance(parsed, dict):
                return parsed
        return {}


def validate_story_model(story: dict[str, Any]) -> None:
    required_keys = ["title", "author", "language", "characters", "places", "organizations", "chapters", "scenes", "narration_paragraphs"]
    if not isinstance(story, dict):
        raise BookAnalysisError("story.json must be a JSON object")
    for key in required_keys:
        if key not in story:
            raise BookAnalysisError(f"story.json missing required key: {key}")
    if not isinstance(story["title"], str):
        raise BookAnalysisError("story.title must be a string")
    if not isinstance(story["author"], str):
        raise BookAnalysisError("story.author must be a string")
    if not isinstance(story["language"], str):
        raise BookAnalysisError("story.language must be a string")
    for key in ["characters", "places", "organizations", "chapters", "scenes", "narration_paragraphs"]:
        if not isinstance(story[key], list):
            raise BookAnalysisError(f"story.{key} must be a list")
    for chapter in story["chapters"]:
        if not isinstance(chapter, dict):
            raise BookAnalysisError("Each chapter must be an object")
        for key in ["number", "title", "paragraph_count", "scene_count", "summary"]:
            if key not in chapter:
                raise BookAnalysisError(f"Chapter record missing {key}")
    for scene in story["scenes"]:
        if not isinstance(scene, dict):
            raise BookAnalysisError("Each scene must be an object")
        for key in ["chapter", "scene_number", "start_paragraph", "end_paragraph", "summary", "participating_characters", "locations"]:
            if key not in scene:
                raise BookAnalysisError(f"Scene record missing {key}")
    for item in story["narration_paragraphs"]:
        if not isinstance(item, dict):
            raise BookAnalysisError("Narration records must be objects")
        for key in ["chapter", "paragraph_index", "text"]:
            if key not in item:
                raise BookAnalysisError(f"Narration record missing {key}")


def _build_scene_record(
    chapter_number: int,
    scene_number: int,
    chunk: list[dict[str, Any]],
    text: str,
    scene_info: dict[str, Any],
    characters: list[dict[str, Any]],
    places: list[dict[str, Any]],
    organizations: list[dict[str, Any]],
    source_document_id: str,
) -> dict[str, Any]:
    start_paragraph = int(chunk[0]["index"])
    end_paragraph = int(chunk[-1]["index"])
    participating_characters = _participants_from_text(text, characters)
    locations = _participants_from_text(text, places)
    if isinstance(scene_info, dict):
        for name in _ensure_list(scene_info.get("characters")):
            if name not in participating_characters:
                participating_characters.append(name)
        for name in _ensure_list(scene_info.get("locations")):
            if name not in locations:
                locations.append(name)
    return {
        "chapter": chapter_number,
        "scene_number": scene_number,
        "start_paragraph": start_paragraph,
        "end_paragraph": end_paragraph,
        "summary": _first_text(scene_info.get("summary") if isinstance(scene_info, dict) else None) or _summarize_text(text),
        "participating_characters": participating_characters,
        "locations": locations,
        "source_document_id": source_document_id,
        "source_text_hash": _text_hash(text),
        "source_reference": {
            "chapter": chapter_number,
            "paragraph_index": start_paragraph,
            "source_document_id": source_document_id,
            "source_text_hash": _text_hash(text),
            "excerpt": _excerpt(text),
        },
    }


def _detect_dialogue_and_narration(
    paragraphs: list[dict[str, Any]],
    character_index: dict[str, dict[str, Any]],
    chapter_number: int,
    source_document_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    dialogue: list[dict[str, Any]] = []
    narration: list[dict[str, Any]] = []
    known_names = [record["name"] for record in character_index.values()]
    known_aliases = {alias.lower(): record["name"] for record in character_index.values() for alias in record.get("aliases", [])}

    for paragraph in paragraphs:
        text = paragraph["text"]
        quotes = [match.group(1).strip() for match in _QUOTE_RE.finditer(text)]
        if not quotes:
            narration.append({
                "chapter": chapter_number,
                "paragraph_index": int(paragraph["index"]),
                "text": text,
                "source_document_id": source_document_id,
                "source_text_hash": _text_hash(text),
                "source_reference": {
                    "chapter": chapter_number,
                    "paragraph_index": int(paragraph["index"]),
                    "source_document_id": source_document_id,
                    "source_text_hash": _text_hash(text),
                    "excerpt": _excerpt(text),
                },
            })
            continue
        for quote in quotes:
            speaker, confidence = _guess_speaker(text, quote, known_names, known_aliases)
            dialogue.append(
                {
                    "chapter": chapter_number,
                    "paragraph_index": int(paragraph["index"]),
                    "speaker": speaker,
                    "confidence": confidence,
                    "quoted_text": quote,
                    "source_document_id": source_document_id,
                    "source_text_hash": _text_hash(text),
                    "source_reference": {
                        "chapter": chapter_number,
                        "paragraph_index": int(paragraph["index"]),
                        "source_document_id": source_document_id,
                        "source_text_hash": _text_hash(text),
                        "excerpt": _excerpt(text),
                    },
                }
            )
    return dialogue, narration


def _guess_speaker(text: str, quote: str, known_names: list[str], known_aliases: dict[str, str]) -> tuple[str, float]:
    lowered = text.lower()
    for name in known_names:
        name_lower = name.lower()
        near = re.search(rf"\b{re.escape(name_lower)}\b[^.?!]{{0,40}}\b(?:{'|'.join(_DIALOGUE_VERBS)})\b", lowered)
        if near:
            return name, 0.9
        near = re.search(rf"\b(?:{'|'.join(_DIALOGUE_VERBS)})\b[^.?!]{{0,40}}\b{re.escape(name_lower)}\b", lowered)
        if near:
            return name, 0.9
    for alias, canonical in known_aliases.items():
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return canonical, 0.7
    return "unknown", 0.35


def _update_mentions(
    character_index: dict[str, dict[str, Any]],
    place_index: dict[str, dict[str, Any]],
    org_index: dict[str, dict[str, Any]],
    dialogue: list[dict[str, Any]],
    narration: list[dict[str, Any]],
    chapter_text: str,
    chapter_number: int,
    source_document_id: str,
) -> None:
    for segment in dialogue:
        speaker = segment.get("speaker")
        if speaker and speaker != "unknown":
            key = _slugify(speaker)
            if key in character_index:
                character_index[key]["dialogue_count"] = int(character_index[key].get("dialogue_count", 0)) + 1
                _register_chapter(character_index[key], chapter_number)
                ref = segment.get("source_reference")
                if isinstance(ref, dict):
                    character_index[key].setdefault("source_references", []).append(ref)
    for paragraph in narration:
        text = str(paragraph.get("text") or "")
        paragraph_index = int(paragraph.get("paragraph_index") or chapter_number)
        for record in character_index.values():
            if _record_mentioned(text, record):
                record["narration_mentions"] = int(record.get("narration_mentions", 0)) + 1
                _register_chapter(record, chapter_number)
                _append_source_reference(record, chapter_number, paragraph_index, source_document_id, text)
        for record in place_index.values():
            if _record_mentioned(text, record):
                _register_chapter(record, chapter_number)
                _append_source_reference(record, chapter_number, paragraph_index, source_document_id, text)
        for record in org_index.values():
            if _record_mentioned(text, record):
                _register_chapter(record, chapter_number)
                _append_source_reference(record, chapter_number, paragraph_index, source_document_id, text)
    for record in character_index.values():
        if _record_mentioned(chapter_text, record):
            _register_chapter(record, chapter_number)
    for record in place_index.values():
        if _record_mentioned(chapter_text, record):
            _register_chapter(record, chapter_number)
    for record in org_index.values():
        if _record_mentioned(chapter_text, record):
            _register_chapter(record, chapter_number)


def _merge_entity_payload(
    character_index: dict[str, dict[str, Any]],
    place_index: dict[str, dict[str, Any]],
    org_index: dict[str, dict[str, Any]],
    payload: dict[str, Any],
    chapter_number: int,
) -> None:
    _merge_entity_records(character_index, payload.get("characters"), chapter_number, entity_type="character")
    _merge_entity_records(place_index, payload.get("places"), chapter_number, entity_type="place")
    _merge_entity_records(org_index, payload.get("organizations"), chapter_number, entity_type="organization")


def _merge_entity_records(target: dict[str, dict[str, Any]], values: Any, chapter_number: int, entity_type: str) -> None:
    for value in _ensure_list(values):
        if isinstance(value, str):
            record = {"name": value}
        elif isinstance(value, dict):
            record = dict(value)
        else:
            continue
        name = str(record.get("name") or record.get("canonical_name") or "").strip()
        if not name:
            continue
        key = _slugify(name)
        existing = target.get(key)
        aliases = _normalize_aliases(record.get("aliases"))
        if existing is None:
            existing = {
                "id": key,
                "name": name,
                "aliases": aliases,
                "gender": _null_if_blank(record.get("gender")),
                "age": _null_if_blank(record.get("age")),
                "role": _null_if_blank(record.get("role")),
                "first_chapter": chapter_number,
                "chapters": [chapter_number],
                "dialogue_count": 0 if entity_type == "character" else None,
                "narration_mentions": 0 if entity_type == "character" else None,
                "source_references": _normalize_source_references(record.get("source_references"), chapter_number),
            }
            target[key] = existing
            continue
        if name and existing["name"] == existing.get("id"):
            existing["name"] = name
        if name and len(name) > len(existing.get("name", "")):
            existing["name"] = name
        existing["aliases"] = _unique_list(existing.get("aliases", []) + aliases)
        if existing.get("gender") in (None, ""):
            existing["gender"] = _null_if_blank(record.get("gender"))
        if existing.get("age") in (None, ""):
            existing["age"] = _null_if_blank(record.get("age"))
        if existing.get("role") in (None, ""):
            existing["role"] = _null_if_blank(record.get("role"))
        if chapter_number not in existing.get("chapters", []):
            existing.setdefault("chapters", []).append(chapter_number)
        if chapter_number < int(existing.get("first_chapter") or chapter_number):
            existing["first_chapter"] = chapter_number


def _finalize_entity(record: dict[str, Any], entity_type: str) -> dict[str, Any]:
    output = dict(record)
    output["aliases"] = _unique_list(output.get("aliases", []))
    output["chapters"] = sorted(set(int(ch) for ch in output.get("chapters", [])))
    if "source_references" in output:
        deduped = []
        seen = set()
        for ref in output.get("source_references", []):
            key = json.dumps(ref, sort_keys=True, ensure_ascii=False) if isinstance(ref, dict) else str(ref)
            if key not in seen:
                seen.add(key)
                deduped.append(ref)
        output["source_references"] = deduped
    if entity_type != "character":
        output.pop("dialogue_count", None)
        output.pop("narration_mentions", None)
    else:
        output["dialogue_count"] = int(output.get("dialogue_count") or 0)
        output["narration_mentions"] = int(output.get("narration_mentions") or 0)
    if output.get("gender") in ("", None):
        output["gender"] = None
    if output.get("age") in ("", None):
        output["age"] = None
    if output.get("role") in ("", None):
        output["role"] = None
    return output


def _sorted_entities(index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(index.values(), key=lambda record: (int(record.get("first_chapter") or 0), str(record.get("name") or "").lower()))


def _register_chapter(record: dict[str, Any], chapter_number: int) -> None:
    chapters = record.setdefault("chapters", [])
    if chapter_number not in chapters:
        chapters.append(chapter_number)
    if not record.get("first_chapter") or chapter_number < int(record.get("first_chapter")):
        record["first_chapter"] = chapter_number


def _append_source_reference(record: dict[str, Any], chapter_number: int, paragraph_index: int, source_document_id: str, text: str) -> None:
    refs = record.setdefault("source_references", [])
    reference = {
        "chapter": chapter_number,
        "paragraph_index": paragraph_index,
        "source_document_id": source_document_id,
        "source_text_hash": _text_hash(text),
        "excerpt": _excerpt(text),
    }
    if reference not in refs:
        refs.append(reference)


def _record_mentioned(text: str, record: dict[str, Any]) -> bool:
    names = [str(record.get("name") or "")]
    names.extend(_ensure_list(record.get("aliases")))
    lowered = text.lower()
    for name in names:
        if not name:
            continue
        if re.search(rf"\b{re.escape(str(name).lower())}\b", lowered):
            return True
    return False


def _participants_from_text(text: str, records: list[dict[str, Any]]) -> list[str]:
    participants: list[str] = []
    for record in records:
        if _record_mentioned(text, record):
            name = str(record.get("name") or "").strip()
            if name and name not in participants:
                participants.append(name)
    return participants


def _split_paragraphs(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    if not cleaned:
        return []
    raw_paragraphs = [segment.strip() for segment in _PARA_SPLIT_RE.split(cleaned) if segment.strip()]
    if len(raw_paragraphs) <= 1:
        raw_paragraphs = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return [{"index": idx, "text": paragraph} for idx, paragraph in enumerate(raw_paragraphs, start=1)]


def _chunk_paragraphs(paragraphs: list[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    if not paragraphs:
        return []
    max_chars = max(1, int(max_chars))
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_len = 0
    for paragraph in paragraphs:
        text = str(paragraph["text"])
        paragraph_len = len(text)
        if current and current_len + paragraph_len > max_chars:
            chunks.append(current)
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += paragraph_len
    if current:
        chunks.append(current)
    return chunks


def _null_if_blank(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def _ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    if isinstance(value, str):
        return [value]
    return [value]


def _normalize_aliases(value: Any) -> list[str]:
    aliases: list[str] = []
    for alias in _ensure_list(value):
        text = str(alias or "").strip()
        if text and text not in aliases:
            aliases.append(text)
    return aliases


def _normalize_source_references(value: Any, chapter_number: int) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in _ensure_list(value):
        if isinstance(item, dict):
            ref = dict(item)
            if "chapter" not in ref:
                ref["chapter"] = chapter_number
            refs.append(ref)
    return refs


def _unique_list(items: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug or "entity"


def _safe_provider_call(self, method_name: str, text: str) -> dict[str, Any]:
    method = getattr(self.provider, method_name)
    try:
        result = method(text)
    except Exception as exc:
        raise BookAnalysisError(f"{method_name} failed: {exc}") from exc
    if result is None:
        return {}
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        parsed = _coerce_json(result)
        if isinstance(parsed, dict):
            return parsed
    return {}


# Bind method on the class after definition without polluting the main flow.
StoryAnalyzer._safe_provider_call = _safe_provider_call  # type: ignore[attr-defined]


def _build_scene_record(
    chapter_number: int,
    scene_number: int,
    chunk: list[dict[str, Any]],
    text: str,
    scene_info: dict[str, Any],
    characters: list[dict[str, Any]],
    places: list[dict[str, Any]],
    organizations: list[dict[str, Any]],
    source_document_id: str,
) -> dict[str, Any]:
    start_paragraph = int(chunk[0]["index"])
    end_paragraph = int(chunk[-1]["index"])
    participating_characters = _participants_from_text(text, characters)
    locations = _participants_from_text(text, places)
    if isinstance(scene_info, dict):
        for name in _ensure_list(scene_info.get("characters")):
            name = str(name or "").strip()
            if name and name not in participating_characters:
                participating_characters.append(name)
        for name in _ensure_list(scene_info.get("locations")):
            name = str(name or "").strip()
            if name and name not in locations:
                locations.append(name)
    return {
        "chapter": chapter_number,
        "scene_number": scene_number,
        "start_paragraph": start_paragraph,
        "end_paragraph": end_paragraph,
        "summary": _first_text(scene_info.get("summary") if isinstance(scene_info, dict) else None) or _summarize_text(text),
        "participating_characters": participating_characters,
        "locations": locations,
        "source_document_id": source_document_id,
        "source_text_hash": _text_hash(text),
        "source_reference": {
            "chapter": chapter_number,
            "paragraph_index": start_paragraph,
            "source_document_id": source_document_id,
            "source_text_hash": _text_hash(text),
            "excerpt": _excerpt(text),
        },
    }


def _analysis_signature(settings: AnalysisSettings) -> dict[str, Any]:
    return {
        "llm_provider": settings.llm_provider,
        "ollama_url": settings.ollama_url,
        "ollama_model": settings.ollama_model,
        "analysis_chunk_size": int(settings.analysis_chunk_size),
        "cache_enabled": bool(settings.cache_enabled),
        "cache_directory": settings.cache_directory,
    }


def _signature_hash(signature: dict[str, Any]) -> str:
    payload = json.dumps(signature, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _excerpt(text: str, limit: int = 140) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _heuristic_entities(text: str) -> dict[str, Any]:
    candidates = re.findall(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", text)
    stopwords = {
        "A",
        "An",
        "And",
        "At",
        "But",
        "By",
        "Chapter",
        "He",
        "Her",
        "His",
        "I",
        "In",
        "It",
        "No",
        "Of",
        "On",
        "Or",
        "The",
        "Then",
        "There",
        "They",
        "This",
        "To",
        "We",
        "When",
        "With",
        "You",
    }
    place_markers = ("city", "town", "village", "tower", "hall", "castle", "road", "street", "forest", "bay", "harbor", "harbour", "river", "mountain", "island", "garden", "camp", "station", "school", "house")
    org_markers = ("guild", "order", "council", "company", "agency", "corps", "institute", "society", "clan", "league", "union", "church", "school", "university", "bureau")

    def _record(name: str) -> dict[str, Any]:
        return {"name": name, "aliases": []}

    characters: list[dict[str, Any]] = []
    places: list[dict[str, Any]] = []
    organizations: list[dict[str, Any]] = []
    seen = set()
    for candidate in candidates:
        name = candidate.strip()
        if not name or name in stopwords or name in seen:
            continue
        seen.add(name)
        lowered = name.lower()
        if any(marker in lowered for marker in org_markers):
            organizations.append(_record(name))
        elif any(marker in lowered for marker in place_markers):
            places.append(_record(name))
        elif len(name.split()) <= 3:
            characters.append(_record(name))
    return {"characters": characters, "places": places, "organizations": organizations}


def _first_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = _first_text(item)
            if text:
                return text
    return ""


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {" ", "-", "_", "."} else "_" for ch in value).strip()
    return safe or "Storyforge Book"


def _summarize_text(text: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _file_signature(path: Path) -> dict[str, Any]:
    stat = path.stat()
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": sha256}


def _coerce_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except Exception:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except Exception:
                return None
    return None


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
