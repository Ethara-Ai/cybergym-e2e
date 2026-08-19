"""Docker image and Dockerfile scanner for CRUCIBLE audit.

Checks:
  - Base image digest pinning (@sha256:)
  - Mutable tag usage
  - USER directive presence
  - Third-party namespace detection
  - Cross-task base-image consistency
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DockerFinding:
    task: str
    check: str
    severity: str
    detail: str
    location: str


@dataclass
class DockerScanResult:
    task: str
    dockerfile: Path
    from_line: str = ""
    image_ref: str = ""
    has_digest_pin: bool = False
    has_user_directive: bool = False
    namespace: str = ""
    tag: str = ""
    findings: list[DockerFinding] = field(default_factory=list)


_FROM_RE = re.compile(
    r"^\s*FROM\s+"
    r"(?P<image>[^\s@]+)"
    r"(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
    r"(?:\s+AS\s+\w+)?",
    re.IGNORECASE,
)

# Known trusted base-image namespaces on Docker Hub and GCR.
_TRUSTED_NAMESPACES = frozenset({
    "gcr.io/oss-fuzz-base",
    "docker.io/library",
    "ubuntu",
    "debian",
    "alpine",
})


class DockerScanner:
    """Scan task Dockerfiles for supply-chain and isolation issues."""

    def __init__(self, tasks_dir: Path) -> None:
        self.tasks_dir = tasks_dir
        self._results: list[DockerScanResult] = []

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def scan(self) -> list[DockerScanResult]:
        """Run all checks across every task Dockerfile."""
        self._results = []
        for task_dir in sorted(self.tasks_dir.iterdir()):
            if not task_dir.is_dir() or task_dir.name.startswith("."):
                continue
            dockerfile = task_dir / "environment" / "Dockerfile"
            if not dockerfile.exists():
                continue
            result = self._scan_dockerfile(task_dir.name, dockerfile)
            self._results.append(result)
        return self._results

    def summary(self) -> dict:
        """Return aggregate counts keyed by check name."""
        counts: dict[str, int] = {}
        for r in self._results:
            for f in r.findings:
                counts[f.check] = counts.get(f.check, 0) + 1
        return {
            "tasks_scanned": len(self._results),
            "total_findings": sum(counts.values()),
            "by_check": counts,
        }

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _scan_dockerfile(self, task_name: str, dockerfile: Path) -> DockerScanResult:
        text = dockerfile.read_text(errors="replace")
        lines = text.splitlines()
        result = DockerScanResult(task=task_name, dockerfile=dockerfile)

        for i, line in enumerate(lines, 1):
            m = _FROM_RE.match(line)
            if m:
                result.from_line = line.strip()
                result.image_ref = m.group("image")
                digest = m.group("digest")
                result.has_digest_pin = digest is not None

                # Parse namespace and tag.
                if "/" in result.image_ref:
                    result.namespace = result.image_ref.rsplit("/", 1)[0]
                else:
                    result.namespace = "docker.io/library"
                if ":" in result.image_ref:
                    result.tag = result.image_ref.split(":")[-1]
                else:
                    result.tag = "latest"

                # CHK-001: Digest pinning.
                if not result.has_digest_pin:
                    result.findings.append(DockerFinding(
                        task=task_name,
                        check="image_pinning",
                        severity="HIGH",
                        detail=(
                            f"Image '{result.image_ref}' uses mutable tag "
                            f"'{result.tag}' without @sha256: digest pin."
                        ),
                        location=f"{dockerfile}:{i}",
                    ))

                # Third-party namespace.
                if result.namespace not in _TRUSTED_NAMESPACES:
                    result.findings.append(DockerFinding(
                        task=task_name,
                        check="third_party_namespace",
                        severity="MEDIUM",
                        detail=(
                            f"Image namespace '{result.namespace}' is not in the "
                            f"trusted set. Third-party images can be replaced "
                            f"without notice."
                        ),
                        location=f"{dockerfile}:{i}",
                    ))

            # CHK-004: USER directive.
            if line.strip().upper().startswith("USER "):
                result.has_user_directive = True

        if not result.has_user_directive:
            result.findings.append(DockerFinding(
                task=task_name,
                check="no_user_directive",
                severity="MEDIUM",
                detail="Dockerfile has no USER directive; container runs as root.",
                location=str(dockerfile),
            ))

        return result

    # ------------------------------------------------------------------
    # cross-task checks
    # ------------------------------------------------------------------

    def cross_task_consistency(self) -> list[DockerFinding]:
        """Detect cross-task base-image anomalies (e.g. espeak using harfbuzz tag)."""
        findings: list[DockerFinding] = []
        for result in self._results:
            # A task whose image tag references a different task's arvo id.
            if result.tag and result.task:
                # Extract the arvo/oss-fuzz id from the task name.
                task_id = result.task.split("__")[-1] if "__" in result.task else ""
                if task_id and result.tag and task_id not in result.tag:
                    # The tag references a different task's image.
                    findings.append(DockerFinding(
                        task=result.task,
                        check="cross_task_image_mismatch",
                        severity="MEDIUM",
                        detail=(
                            f"Task '{result.task}' uses image tag "
                            f"'{result.tag}' which does not contain the "
                            f"task's own id '{task_id}'. Possible wrong "
                            f"base image."
                        ),
                        location=str(result.dockerfile),
                    ))
        return findings
