"""Benchmark plugin registry (FORGE.md:184)."""
import importlib
import sys
from pathlib import Path
from typing import Callable

from .base import Benchmark

_REGISTRY: dict[str, Callable[[], Benchmark]] = {}


def register(name: str, factory: Callable[[], Benchmark]) -> None:
    """Register a benchmark plugin by name."""
    _REGISTRY[name] = factory


def get(name: str) -> Benchmark:
    """Get a benchmark plugin instance by name."""
    if name not in _REGISTRY:
        _discover()
    if name not in _REGISTRY:
        raise KeyError(f"benchmark {name!r} not registered; known: {sorted(_REGISTRY)}")
    return _REGISTRY[name]()


def names() -> list[str]:
    """List registered benchmark plugin names."""
    if not _REGISTRY:
        _discover()
    return sorted(_REGISTRY)


def _discover() -> None:
    """Walk benchmarks/ and import each plugin.py (side-effect: register)."""
    root = Path(__file__).resolve().parent.parent / "benchmarks"
    if not root.is_dir():
        return
    parent = str(root.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")):
            continue
        if not (child / "plugin.py").is_file():
            continue
        try:
            importlib.import_module(f"benchmarks.{child.name}.plugin")
        except Exception as exc:
            print(f"warning: could not import benchmarks/{child.name}: {exc}", file=sys.stderr)
