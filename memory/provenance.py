"""ENGRAM provenance gate. Phase 1 step 8.

Binds ledger digest, seed digest, proof paths/hashes, git SHA, clean bit,
and approved-scope digest. CURRENT requires external proof signatures,
clean recorded tree, no seed drift, and deterministic recompute.
E8: every CFER traces to a verifiable proof digest.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    import json as yaml
    yaml.safe_load = yaml.loads
    yaml.safe_dump = yaml.dumps


@dataclass
class ProvenanceBinding:
    ledger_digest: str
    seed_digest: str | None
    proof_paths: list[str]
    proof_hashes: dict[str, str]
    git_sha: str
    clean_bit: bool
    approved_scope_digest: str
    requirements_digest: str | None
    touchstone_digest: str | None
    dataset_digest: str | None


@dataclass
class ProvenanceResult:
    disposition: str
    binding: ProvenanceBinding | None = None
    missing: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    reason: str = ""


def _file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dir_digest(dir_path: Path, pattern: str = "*.yaml") -> str | None:
    if not dir_path.exists():
        return None
    h = hashlib.sha256()
    for f in sorted(dir_path.glob(pattern)):
        h.update(f.read_bytes())
    return h.hexdigest()


def compute_binding(
    memory_dir: Path,
    project_root: Path,
    git_sha: str,
    clean_bit: bool,
) -> ProvenanceBinding:
    ledger_path = memory_dir / "ledger.yaml"
    seed_path = memory_dir / "seed.yaml"
    approval_path = memory_dir / "approval"
    scope_path = memory_dir / "scope.yaml"
    proofs_dir = memory_dir / "proofs"
    req_dir = project_root / "requirements"
    touch_dir = project_root / "touchstones"
    dataset_dir = project_root / "dataset"

    ledger_dig = _file_digest(ledger_path) or "missing"
    seed_dig = _file_digest(seed_path)
    scope_dig = _file_digest(scope_path) or "missing"

    proof_paths = []
    proof_hashes: dict[str, str] = {}
    if proofs_dir.exists():
        for p in sorted(proofs_dir.glob("*.yaml")):
            proof_paths.append(str(p.relative_to(project_root)))
            proof_hashes[p.name] = _file_digest(p) or "unreadable"

    req_dig = _dir_digest(req_dir, "*.md") if req_dir.exists() else None
    touch_dig = _dir_digest(touch_dir) if touch_dir.exists() else None
    dataset_dig = _dir_digest(dataset_dir) if dataset_dir.exists() else None

    approved_dig = ""
    if approval_path.exists():
        approved_dig = approval_path.read_text().strip()

    return ProvenanceBinding(
        ledger_digest=ledger_dig,
        seed_digest=seed_dig,
        proof_paths=proof_paths,
        proof_hashes=proof_hashes,
        git_sha=git_sha,
        clean_bit=clean_bit,
        approved_scope_digest=approved_dig,
        requirements_digest=req_dig,
        touchstone_digest=touch_dig,
        dataset_digest=dataset_dig,
    )


def check_provenance(
    binding: ProvenanceBinding,
    has_external_signatures: bool,
    seed_drift: bool,
    recompute_identical: bool,
) -> ProvenanceResult:
    missing = []
    broken = []

    if binding.ledger_digest == "missing":
        missing.append("ledger.yaml")

    if not binding.approved_scope_digest:
        missing.append("approval digest")

    if not has_external_signatures:
        missing.append("external proof signatures")

    if not binding.clean_bit:
        broken.append("dirty git tree")

    if seed_drift:
        broken.append("seed drift detected")

    if not recompute_identical:
        broken.append("non-identical recompute")

    if not binding.requirements_digest:
        missing.append("requirements/ digest")

    if not binding.dataset_digest:
        missing.append("dataset/ digest")

    if broken:
        return ProvenanceResult(
            disposition="BROKEN",
            binding=binding,
            missing=missing,
            broken=broken,
            reason=f"broken: {', '.join(broken)}",
        )

    if missing:
        return ProvenanceResult(
            disposition="STALE",
            binding=binding,
            missing=missing,
            broken=broken,
            reason=f"missing: {', '.join(missing)}",
        )

    return ProvenanceResult(
        disposition="CURRENT",
        binding=binding,
        missing=missing,
        broken=broken,
        reason="all provenance checks pass",
    )
