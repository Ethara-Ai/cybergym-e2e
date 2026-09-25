"""Abstract Benchmark base class (FORGE.md:184 three-duty interface)."""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Instance:
    """Prepared bundle instance."""
    bundle_path: Path
    task_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sandbox:
    """Per-instance sandboxed workspace."""
    container_id: str | None = None
    image_digest: str | None = None
    isolation_verified: bool = False
    working_dir: Path | None = None


@dataclass
class Reward:
    """Evaluation output."""
    value: float = 0.0
    stages: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class Benchmark(ABC):
    """Trinity Benchmark interface with three duty methods (FORGE.md:184)."""

    name: str = ""

    @abstractmethod
    def prepare_instance(self, bundle_path: Path) -> Instance:
        """Read a resident bundle; return an Instance."""

    @abstractmethod
    def prepare_sandbox(self, instance: Instance) -> Sandbox:
        """Build the per-instance sandbox and verify isolation."""

    @abstractmethod
    def evaluate_instance(self, instance: Instance, rollout_path: Path) -> Reward:
        """Grade a rollout against a bundle; return a Reward."""
