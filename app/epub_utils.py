from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, ITEM_IMAGE, epub


_NAV_PATTERNS = [
    r"^table of contents$",
    r"^contents$",
    r"^next$",
    r"^previous$",
    r"^back$",
    r"^home$",
    r"^page\s+\d+$",
    r"^\d+$",
    r"^copyright$",
]


@dataclass(frozen=True)
class ChapterEntry:
    number: int
    title: str
    href: str
    anchor: str = ""

    @property
    def display_title(self) -> str:
        return self.title.strip() or f"Chapter {self.number}"


@dataclass(frozen=True)
class BookMetadata:
    title: str
    authors: List[str]
    series: str
    publisher: str
    language: str
    description: str
    publication_date: str
    identifier: str


@dataclass(frozen=True)
class CoverImage:
    filename: str
    media_type: str
    data: bytes


def read_epub(epub_path: Path) -> epub.EpubBook:
    return epub.read_epub(str(epub_path))


def extract_book_metadata(book: epub.EpubBook) -> BookMetadata:
    def meta_value(namespace: str, name: str) -> str:
        values = book.get_metadata(namespace, name)
        if not values:
            return ""
        first = values[0]
        if isinstance(first, tuple):
            return str(first[0] or "").strip()
        return str(first or "").strip()

    authors: List[str] = []
    for item in book.get_metadata("DC", "creator"):
        value = item[0] if isinstance(item, tuple) else item
        value = str(value or "").strip()
        if value:
            authors.append(value)

    series = _extract_series(book)
    description = meta_value("DC", "description")
    title = meta_value("DC", "title") or Path(getattr(book, "file_name", "") or "").stem
    return BookMetadata(
        title=title,
        authors=authors,
        series=series,
        publisher=meta_value("DC", "publisher"),
        language=meta_value("DC", "language"),
        description=description,
        publication_date=meta_value("DC", "date"),
        identifier=meta_value("DC", "identifier"),
    )


def extract_cover_image(book: epub.EpubBook) -> Optional[CoverImage]:
    items = list(book.get_items_of_type(ITEM_IMAGE))
    if not items:
        return None

    preferred = None
    for item in items:
        file_name = (getattr(item, "file_name", "") or getattr(item, "href", "") or "").lower()
        if "cover" in file_name:
            preferred = item
            break
    if preferred is None:
        preferred = items[0]

    filename = getattr(preferred, "file_name", "cover.jpg") or "cover.jpg"
    media_type = getattr(preferred, "media_type", "image/jpeg") or "image/jpeg"
    return CoverImage(filename=filename, media_type=media_type, data=preferred.get_content())


def _extract_series(book: epub.EpubBook) -> str:
    candidates = [
        ("OPF", "series"),
        ("OPF", "belongs-to-collection"),
        ("OPF", "collection"),
        ("DC", "relation"),
    ]
    for namespace, name in candidates:
        for item in book.get_metadata(namespace, name):
            value = item[0] if isinstance(item, tuple) else item
            value = str(value or "").strip()
            if not value:
                continue
            if value.lower().startswith("series:"):
                return value.split(":", 1)[1].strip()
            if namespace == "OPF":
                return value
    return ""


def _flatten_toc(items: Sequence, seen_hrefs: set[str]) -> List[Tuple[str, str]]:
    flat: List[Tuple[str, str]] = []
    for item in items or []:
        if isinstance(item, (list, tuple)):
            flat.extend(_flatten_toc(item, seen_hrefs))
            continue

        title = str(getattr(item, "title", "") or "").strip()
        href = str(getattr(item, "href", "") or "").strip()
        subitems = getattr(item, "subitems", None)
        if href and href not in seen_hrefs:
            seen_hrefs.add(href)
            flat.append((title, href))
        if subitems:
            flat.extend(_flatten_toc(subitems, seen_hrefs))
    return flat


def _normalize_href(href: str) -> str:
    return href.split("#", 1)[0].strip().lower()


def _document_text_length(html_bytes: bytes | str) -> int:
    html_text = html_bytes.decode("utf-8", errors="ignore") if isinstance(html_bytes, bytes) else html_bytes
    soup = BeautifulSoup(html_text, "html.parser")
    return len(soup.get_text(" ", strip=True))


def _is_front_matter_document(title: str, href: str, text_len: int) -> bool:
    normalized = f"{title} {href}".strip().lower()
    front_matter_markers = (
        "titlepage",
        "title page",
        "cover",
        "copyright",
        "contents",
        "table of contents",
        "acknowledgments",
        "acknowledgements",
        "about the author",
        "author",
        "dedication",
        "foreword",
        "preface",
        "prologue",
        "index",
    )
    if text_len <= 20:
        return True
    if any(marker in normalized for marker in front_matter_markers) and text_len < 5000:
        return True
    return False


def list_chapters(book: epub.EpubBook) -> List[ChapterEntry]:
    toc_items = _flatten_toc(getattr(book, "toc", []), set())
    toc_by_href = {_normalize_href(href): title for title, href in toc_items if href}
    entries: List[ChapterEntry] = []

    idx = 1
    for spine_entry in getattr(book, "spine", []):
        item_id = spine_entry[0] if isinstance(spine_entry, (list, tuple)) else spine_entry
        if item_id in {"nav", "ncx"}:
            continue
        item = book.get_item_with_id(item_id)
        if item is None or getattr(item, "get_type", lambda: None)() != ITEM_DOCUMENT:
            continue

        href = getattr(item, "file_name", "") or getattr(item, "href", "") or item_id
        title = toc_by_href.get(_normalize_href(href)) or _infer_document_title(item.get_content(), fallback=f"Chapter {idx}")
        text_len = _document_text_length(item.get_content())
        if _is_front_matter_document(title, href, text_len):
            continue

        entries.append(ChapterEntry(number=idx, title=title or f"Chapter {idx}", href=href))
        idx += 1

    return entries


def get_chapter_document(book: epub.EpubBook, chapter_number: int, chapters: Sequence[ChapterEntry]):
    if chapter_number < 1 or chapter_number > len(chapters):
        raise IndexError(f"Chapter {chapter_number} is out of range. Available chapters: 1-{len(chapters)}")

    chapter = chapters[chapter_number - 1]
    normalized_href = _normalize_href(chapter.href)

    candidates = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        file_name = getattr(item, "file_name", "") or getattr(item, "href", "") or ""
        if _normalize_href(file_name) == normalized_href:
            return chapter, item
        candidates.append(item)

    if chapter_number <= len(candidates):
        return chapter, candidates[chapter_number - 1]

    raise LookupError(f"Unable to locate EPUB document for chapter {chapter_number} ({chapter.title!r}).")


def extract_chapter_html_text(html_bytes: bytes | str) -> str:
    if isinstance(html_bytes, bytes):
        html_text = html_bytes.decode("utf-8", errors="ignore")
    else:
        html_text = html_bytes
    return clean_html_text(html_text)


def clean_html_text(html_text: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")

    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg", "img"]):
        tag.decompose()

    for tag in soup.find_all(attrs={"aria-hidden": "true"}):
        tag.decompose()

    raw_text = soup.get_text("\n")
    raw_text = html.unescape(raw_text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw_text.splitlines()]

    counts = {}
    for line in lines:
        normalized = _normalize_line(line)
        if normalized:
            counts[normalized] = counts.get(normalized, 0) + 1

    filtered: List[str] = []
    for line in lines:
        if not line:
            continue
        normalized = _normalize_line(line)
        if _looks_like_nav_line(normalized, counts):
            continue
        filtered.append(line)

    text = "\n".join(filtered)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip().lower()


def _looks_like_nav_line(normalized_line: str, counts: dict[str, int]) -> bool:
    if not normalized_line:
        return True
    for pattern in _NAV_PATTERNS:
        if re.match(pattern, normalized_line, re.IGNORECASE):
            return True
    if counts.get(normalized_line, 0) > 1 and len(normalized_line) <= 80:
        if normalized_line.isupper() or normalized_line.istitle() or not normalized_line.endswith((".", "!", "?", ",", ";", ":")):
            return True
    if len(normalized_line) <= 3 and normalized_line.isdigit():
        return True
    return False


_heading_re = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL)


def _infer_document_title(html_bytes: bytes, fallback: str) -> str:
    html_text = html_bytes.decode("utf-8", errors="ignore") if isinstance(html_bytes, bytes) else html_bytes
    match = _heading_re.search(html_text)
    if match:
        candidate = BeautifulSoup(match.group(1), "html.parser").get_text(" ", strip=True)
        if candidate:
            return candidate
    soup = BeautifulSoup(html_text, "html.parser")
    title_tag = soup.find("title")
    if title_tag:
        candidate = title_tag.get_text(" ", strip=True)
        if candidate:
            return candidate
    return fallback
