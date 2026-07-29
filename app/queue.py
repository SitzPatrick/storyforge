from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

from .manifest import ConversionManifest, load_manifest


class QueueStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class QueueItem:
    manifest_path: Path
    status: QueueStatus
    title: str
    source_epub: str


class ConversionQueue:
    def __init__(self, output_root: Path) -> None:
        self.output_root = Path(output_root)

    def discover(self) -> list[QueueItem]:
        items: list[QueueItem] = []
        if not self.output_root.exists():
            return items
        for manifest_path in self.output_root.glob("*/manifest.json"):
            try:
                manifest = load_manifest(manifest_path)
            except Exception:
                continue
            items.append(
                QueueItem(
                    manifest_path=manifest_path,
                    status=QueueStatus(manifest.status if manifest.status in QueueStatus._value2member_map_ else "failed"),
                    title=manifest.title,
                    source_epub=manifest.source_epub,
                )
            )
        return items

    def unfinished(self) -> list[QueueItem]:
        return [item for item in self.discover() if item.status != QueueStatus.COMPLETED]
