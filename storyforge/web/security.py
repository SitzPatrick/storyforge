from __future__ import annotations

import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class SecurityError(ValueError):
    pass


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def validate_slug(slug: str) -> str:
    if not slug or not SLUG_RE.fullmatch(slug):
        raise SecurityError("project slug must use lowercase letters, numbers, and hyphens")
    return slug


def secure_filename(filename: str) -> str:
    name = Path(filename).name.strip()
    if not name or name in {".", ".."}:
        raise SecurityError("invalid filename")
    if name != filename and ("/" in filename or "\\" in filename):
        raise SecurityError("filename may not contain path separators")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not cleaned:
        raise SecurityError("invalid filename")
    return cleaned


def ensure_within_root(root: Path, path: Path) -> Path:
    root = root.resolve()
    candidate = path.resolve()
    if os.path.commonpath([str(root), str(candidate)]) != str(root):
        raise SecurityError(f"path escape rejected: {path}")
    return candidate


def safe_child(root: Path, *parts: str) -> Path:
    root = root.resolve()
    candidate = root.joinpath(*parts)
    return ensure_within_root(root, candidate)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
        handle.write(text)
        temp_name = handle.name
    os.replace(temp_name, path)


def atomic_write_json(path: Path, data: object) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))
