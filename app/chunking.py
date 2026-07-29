from __future__ import annotations

import re
from typing import List


_sentence_split_re = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'({\[])")
_whitespace_re = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{2,}", "\n\n", text)
    text = _whitespace_re.sub(" ", text)
    return text.strip()


def split_into_chunks(text: str, max_chars: int = 1200) -> List[str]:
    text = normalize_text(text)
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for paragraph in paragraphs:
        sentences = _split_sentences(paragraph)
        if not sentences:
            sentences = [paragraph]

        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue

            if len(sentence) > max_chars:
                flush()
                chunks.extend(_split_long_text(sentence, max_chars))
                continue

            candidate = f"{current} {sentence}".strip() if current else sentence
            if len(candidate) <= max_chars:
                current = candidate
                continue

            flush()
            current = sentence

    flush()
    return chunks


def _split_sentences(text: str) -> List[str]:
    parts = _sentence_split_re.split(text)
    return [part.strip() for part in parts if part.strip()]


def _split_long_text(text: str, max_chars: int) -> List[str]:
    words = text.split()
    if not words:
        return []

    out: List[str] = []
    current_words: List[str] = []
    current_len = 0
    for word in words:
        word_len = len(word) + (1 if current_words else 0)
        if current_words and current_len + word_len > max_chars:
            out.append(" ".join(current_words).strip())
            current_words = [word]
            current_len = len(word)
        else:
            current_words.append(word)
            current_len += word_len
    if current_words:
        out.append(" ".join(current_words).strip())
    return out
