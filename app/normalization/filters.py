from __future__ import annotations

import re
from collections.abc import Iterable

_ARTICLES = ("the", "a", "an")
_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")


def collapse_whitespace(value: str | None) -> str:
    if value is None:
        return ""
    return _WS_RE.sub(" ", str(value).strip())


def strip_quotes_and_punct(value: str | None) -> str:
    text = collapse_whitespace(value)
    return text.strip(" \t\r\n\"'“”‘’.,;:!?()[]{}<>")


def normalize_text(value: str | None) -> str:
    return collapse_whitespace(strip_quotes_and_punct(value))


def normalize_key(value: str | None) -> str:
    text = normalize_text(value).lower()
    return _NON_ALNUM_RE.sub(" ", text).strip()


def strip_leading_article(value: str | None) -> str:
    text = normalize_text(value)
    lowered = text.lower()
    for article in _ARTICLES:
        prefix = f"{article} "
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def display_name(value: str | None) -> str:
    text = strip_leading_article(value)
    if not text:
        return ""
    if text[0].islower():
        return text[0].upper() + text[1:]
    return text


def slugify(value: str | None) -> str:
    key = normalize_key(strip_leading_article(value))
    slug = _NON_ALNUM_RE.sub("_", key).strip("_")
    return slug or "item"


def is_punctuation_only(value: str | None) -> bool:
    text = normalize_text(value)
    return not any(ch.isalnum() for ch in text)


def build_reject_index(values: Iterable[str]) -> set[str]:
    return {normalize_key(value) for value in values if normalize_key(value)}


def rejection_rule(value: str | None, reject_index: set[str]) -> str | None:
    text = normalize_text(value)
    if not text:
        return "empty"
    if is_punctuation_only(text):
        return "punctuation-only"
    key = normalize_key(text)
    if key in reject_index:
        return "configured rejection list"
    stripped = normalize_key(strip_leading_article(text))
    if stripped in reject_index:
        return "configured rejection list"
    return None


def source_record_id(record: dict) -> str:
    if record.get("id"):
        return str(record["id"])
    chapter = record.get("chapter", "unknown")
    paragraph = record.get("paragraph_index", "unknown")
    digest = str(record.get("source_text_hash") or "")[:12]
    return f"chapter_{chapter}_paragraph_{paragraph}_{digest}"


def source_reference_id(record: dict) -> str:
    chapter = record.get("chapter", "unknown")
    paragraph = record.get("paragraph_index", "unknown")
    digest = str(record.get("source_text_hash") or "")[:12]
    return f"chapter_{chapter}_paragraph_{paragraph}_{digest}"


def appearance_count(record: dict, category: str) -> int:
    if category == "characters":
        return int(record.get("dialogue_count") or 0) + int(record.get("narration_mentions") or 0)
    refs = record.get("source_references") or []
    return len(refs)


def unique_preserve(values: Iterable[str | None]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def entity_key(name: str | None, category: str) -> str:
    text = strip_leading_article(name) if category in {"places", "organizations"} else normalize_text(name)
    return normalize_key(text)
