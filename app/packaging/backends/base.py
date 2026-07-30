from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..models import PackagingBackendResult, PackagingRequest


class PackagingBackend(ABC):
    backend_name: str = "backend"
    backend_version: str = "unknown"
    encoder_name: str = "encoder"
    encoder_version: str | None = None

    @abstractmethod
    def is_available(self) -> bool:
        raise NotImplementedError

    @abstractmethod
    def package(self, request: PackagingRequest) -> PackagingBackendResult:
        raise NotImplementedError

    @abstractmethod
    def probe(self, path: Path | None = None) -> dict[str, Any]:
        raise NotImplementedError
