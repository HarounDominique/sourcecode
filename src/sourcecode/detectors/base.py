from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sourcecode.schema import EntryPoint, FrameworkDetection, StackDetection


@dataclass
class DetectionContext:
    """Contexto comun que reciben todos los detectores."""

    root: Path
    file_tree: dict[str, Any]
    manifests: list[str] = field(default_factory=list)


class AbstractDetector(ABC):
    """Contrato base para detectores de stack."""

    name: str = "abstract"
    priority: int = 100

    @abstractmethod
    def can_detect(self, context: DetectionContext) -> bool:
        """Indica si este detector aplica al proyecto dado."""

    @abstractmethod
    def detect(self, context: DetectionContext) -> tuple[list[StackDetection], list[EntryPoint]]:
        """Produce stacks y entry points detectados."""


__all__ = [
    "AbstractDetector",
    "DetectionContext",
    "EntryPoint",
    "FrameworkDetection",
    "StackDetection",
]
