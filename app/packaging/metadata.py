from __future__ import annotations

import hashlib
import unicodedata
from typing import Any, Mapping

from .models import BookMetadata
from .serialization import canonical_json

_CONTROL_CHARACTERS = {chr(codepoint) for codepoint in range(0x00, 0x20)} - {"\t", "\n"}


def normalize_book_metadata(metadata: BookMetadata | Mapping[str, Any]) -> BookMetadata:
    book = _coerce_book_metadata(metadata)
    return BookMetadata(
        title=_normalize_optional_text(book.title, allow_newlines=False),
        subtitle=_normalize_optional_text(book.subtitle, allow_newlines=False),
        author=_normalize_optional_text(book.author, allow_newlines=False),
        narrator=_normalize_optional_text(book.narrator, allow_newlines=False),
        series=_normalize_optional_text(book.series, allow_newlines=False),
        series_position=_normalize_optional_scalar(book.series_position),
        publisher=_normalize_optional_text(book.publisher, allow_newlines=False),
        publication_year=_normalize_optional_scalar(book.publication_year),
        description=_normalize_optional_text(book.description, allow_newlines=True),
        language=_normalize_optional_text(book.language, allow_newlines=False),
        copyright=_normalize_optional_text(book.copyright, allow_newlines=False),
        genre=_normalize_optional_text(book.genre, allow_newlines=False),
        identifier=_normalize_optional_text(book.identifier, allow_newlines=False),
        comment=_normalize_optional_text(book.comment, allow_newlines=True),
    )


def build_book_metadata_hash(metadata: BookMetadata | Mapping[str, Any]) -> str:
    normalized = normalize_book_metadata(metadata)
    return hashlib.sha256(canonical_json(normalized).encode("utf-8")).hexdigest()


def normalize_metadata_mapping(metadata: BookMetadata | Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_book_metadata(metadata)
    return {
        "title": normalized.title,
        "subtitle": normalized.subtitle,
        "author": normalized.author,
        "narrator": normalized.narrator,
        "series": normalized.series,
        "series_position": normalized.series_position,
        "publisher": normalized.publisher,
        "publication_year": normalized.publication_year,
        "description": normalized.description,
        "language": normalized.language,
        "copyright": normalized.copyright,
        "genre": normalized.genre,
        "identifier": normalized.identifier,
        "comment": normalized.comment,
    }


def _coerce_book_metadata(metadata: BookMetadata | Mapping[str, Any]) -> BookMetadata:
    if isinstance(metadata, BookMetadata):
        return metadata
    if not isinstance(metadata, Mapping):
        raise TypeError("book metadata must be a BookMetadata or mapping")
    return BookMetadata(**dict(metadata))


def _normalize_optional_scalar(value: str | int | None) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = _normalize_text(str(value), allow_newlines=False)
    if not text:
        return None
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return text


def _normalize_optional_text(value: str | None, *, allow_newlines: bool) -> str | None:
    if value is None:
        return None
    text = _normalize_text(str(value), allow_newlines=allow_newlines)
    return text or None


def _normalize_text(value: str, *, allow_newlines: bool) -> str:
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    if not allow_newlines:
        text = text.replace("\n", " ")
    if any(char in _CONTROL_CHARACTERS for char in text):
        raise ValueError("metadata contains unsupported control characters")
    return text
