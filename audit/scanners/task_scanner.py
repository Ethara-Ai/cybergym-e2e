"""Task bundle scanner for CRUCIBLE audit.

Validates task.toml fields, PoC files, patch files, instruction.md,
and structural conformance of each Harbor task bundle under tasks/.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


# -- Required schema fields for task.toml v1.1 ----------------------------

REQUIRED_TOP = {"schema_version", "artifacts"}
REQUIRED_TASK = {"name", "description", "keywords"}
REQUIRED_METADATA = {"difficulty", "category", "subcategory", "family", "tags"}
REQUIRED_VERIFIER = {"timeout_sec", "environment_mode"}
REQUIRED_AGENT = {"timeout_sec"}
REQUIRED_ENVIRONMENT = {"build_timeout_sec", "allow_internet"}

# -- Expected file tree per task -------------------------------------------

REQUIRED_FILES = [
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "solution/fix.patch",
    "solution/poc.bin",
    "tests/test.sh",
]

OPTIONAL_FILES = [
    "environment/validate.py",
    "environment/src.tgz",
    "tests/test_output.py",
    "tests/rubric.json",
    "tests/test_weights.json",
    "tests/data/poc.bin",
    "tests/validate.py",
]


@dataclass
class TaskFinding:
    task: str
    check: str
    severity: str
    detail: str
    location: str


@dataclass
class TaskScanResult:
    task: str
    task_dir: Path
    toml_parsed: bool = False
    toml_data: dict = field(default_factory=dict)
    poc_hash: str = ""
    poc_size: int = 0
    patch_size: int = 0
    gt_poc_hash: str = ""
    findings: list[TaskFinding] = field(default_factory=list)


class TaskScanner:
    """Scan every task bundle for schema and content integrity."""

    def __init__(self, tasks_dir: Path) -> None:
        self.tasks_dir = tasks_dir
        self._results: list[TaskScanResult] = []

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def scan(self) -> list[TaskScanResult]:
        self._results = []
        for task_dir in sorted(self.tasks_dir.iterdir()):
            if not task_dir.is_dir() or task_dir.name.startswith("."):
                continue
            result = self._scan_task(task_dir)
            self._results.append(result)
        self._cross_task_checks()
        return self._results

    def summary(self) -> dict:
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
    # per-task
    # ------------------------------------------------------------------

    def _scan_task(self, task_dir: Path) -> TaskScanResult:
        task_name = task_dir.name
        result = TaskScanResult(task=task_name, task_dir=task_dir)

        # -- file layout -------------------------------------------------
        for rel in REQUIRED_FILES:
            if not (task_dir / rel).exists():
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="missing_required_file",
                    severity="HIGH",
                    detail=f"Required file '{rel}' is missing.",
                    location=str(task_dir / rel),
                ))

        # -- task.toml parsing -------------------------------------------
        toml_path = task_dir / "task.toml"
        if toml_path.exists():
            try:
                with open(toml_path, "rb") as f:
                    data = tomllib.load(f)
                result.toml_parsed = True
                result.toml_data = data
                self._check_toml_schema(result, data, toml_path)
            except Exception as exc:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="toml_parse_error",
                    severity="HIGH",
                    detail=f"task.toml parse failed: {exc}",
                    location=str(toml_path),
                ))

        # -- PoC file integrity ------------------------------------------
        poc_path = task_dir / "solution" / "poc.bin"
        if poc_path.exists():
            data = poc_path.read_bytes()
            result.poc_size = len(data)
            result.poc_hash = hashlib.sha256(data).hexdigest()

            # Null PoC detection.
            if data == b"\x00" * len(data) and len(data) > 0:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="null_poc",
                    severity="MEDIUM",
                    detail=(
                        f"solution/poc.bin is {len(data)} null bytes — "
                        f"likely a placeholder, not a genuine exploit input."
                    ),
                    location=str(poc_path),
                ))

            if len(data) == 0:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="empty_poc",
                    severity="HIGH",
                    detail="solution/poc.bin is empty (0 bytes).",
                    location=str(poc_path),
                ))

        # -- Ground-truth PoC parity -------------------------------------
        gt_poc = task_dir / "tests" / "data" / "poc.bin"
        if gt_poc.exists():
            gt_data = gt_poc.read_bytes()
            result.gt_poc_hash = hashlib.sha256(gt_data).hexdigest()
            if poc_path.exists() and result.poc_hash != result.gt_poc_hash:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="poc_gt_mismatch",
                    severity="HIGH",
                    detail=(
                        "solution/poc.bin and tests/data/poc.bin differ. "
                        "Ground-truth PoC should be byte-identical."
                    ),
                    location=str(gt_poc),
                ))

        # -- Patch file --------------------------------------------------
        patch_path = task_dir / "solution" / "fix.patch"
        if patch_path.exists():
            patch_data = patch_path.read_bytes()
            result.patch_size = len(patch_data)
            if len(patch_data) == 0:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="empty_patch",
                    severity="HIGH",
                    detail="solution/fix.patch is empty (0 bytes).",
                    location=str(patch_path),
                ))

        return result

    # ------------------------------------------------------------------
    # toml schema validation
    # ------------------------------------------------------------------

    def _check_toml_schema(
        self, result: TaskScanResult, data: dict, path: Path
    ) -> None:
        task_name = result.task

        # Top-level keys.
        for key in REQUIRED_TOP:
            if key not in data:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="toml_missing_field",
                    severity="MEDIUM",
                    detail=f"Top-level field '{key}' missing from task.toml.",
                    location=str(path),
                ))

        sections = {
            "task": REQUIRED_TASK,
            "metadata": REQUIRED_METADATA,
            "verifier": REQUIRED_VERIFIER,
            "agent": REQUIRED_AGENT,
            "environment": REQUIRED_ENVIRONMENT,
        }
        for section, keys in sections.items():
            block = data.get(section, {})
            if not block:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="toml_missing_section",
                    severity="MEDIUM",
                    detail=f"Section '[{section}]' missing from task.toml.",
                    location=str(path),
                ))
                continue
            for key in keys:
                if key not in block:
                    result.findings.append(TaskFinding(
                        task=task_name,
                        check="toml_missing_field",
                        severity="MEDIUM",
                        detail=f"Field '{section}.{key}' missing from task.toml.",
                        location=str(path),
                    ))

        # -- task name consistency with directory name --------------------
        declared_name = data.get("task", {}).get("name", "")
        if declared_name and declared_name != task_name:
            # Allow prefix like "cybergym-e2e/" but flag it.
            if "/" in declared_name:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="task_name_prefix",
                    severity="LOW",
                    detail=(
                        f"Declared name '{declared_name}' uses a prefix. "
                        f"Other tasks use bare directory name."
                    ),
                    location=str(path),
                ))
            else:
                result.findings.append(TaskFinding(
                    task=task_name,
                    check="task_name_mismatch",
                    severity="MEDIUM",
                    detail=(
                        f"Declared name '{declared_name}' does not match "
                        f"directory name '{task_name}'."
                    ),
                    location=str(path),
                ))

        # -- allow_internet consistency -----------------------------------
        allow_net = data.get("environment", {}).get("allow_internet")
        if allow_net is True:
            result.findings.append(TaskFinding(
                task=task_name,
                check="internet_allowed",
                severity="MEDIUM",
                detail=(
                    "allow_internet=true. Agent may access the network "
                    "during vulnerability discovery."
                ),
                location=str(path),
            ))

    # ------------------------------------------------------------------
    # cross-task
    # ------------------------------------------------------------------

    def _cross_task_checks(self) -> None:
        """Detect duplicate PoC files across tasks."""
        poc_map: dict[str, list[str]] = {}
        for r in self._results:
            if r.poc_hash:
                poc_map.setdefault(r.poc_hash, []).append(r.task)

        for h, tasks in poc_map.items():
            if len(tasks) > 1:
                for task in tasks:
                    result = next(r for r in self._results if r.task == task)
                    result.findings.append(TaskFinding(
                        task=task,
                        check="duplicate_poc",
                        severity="MEDIUM",
                        detail=(
                            f"solution/poc.bin is byte-identical across "
                            f"tasks: {', '.join(tasks)} "
                            f"(sha256: {h[:16]}...)."
                        ),
                        location=str(result.task_dir / "solution" / "poc.bin"),
                    ))
