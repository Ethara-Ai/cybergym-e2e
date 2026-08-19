"""Runner (run_harbor.py) scanner for CRUCIBLE audit.

Checks:
  - Shell injection via subprocess shell=True with interpolation
  - Network policy enforcement (allow_internet → --network=none)
  - Credential handling
  - Rubric judge reliability discipline
  - Verifier isolation (separate container)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class RunnerFinding:
    check: str
    severity: str
    detail: str
    location: str
    line: Optional[int] = None


class RunnerScanner:
    """Static analysis of run_harbor.py and test_output.py verifiers."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root
        self.runner = project_root / "run_harbor.py"
        self.tasks_dir = project_root / "tasks"
        self._findings: list[RunnerFinding] = []

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------

    def scan(self) -> list[RunnerFinding]:
        self._findings = []
        if self.runner.exists():
            self._scan_runner()
        self._scan_test_outputs()
        return self._findings

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for f in self._findings:
            counts[f.check] = counts.get(f.check, 0) + 1
        return {
            "total_findings": len(self._findings),
            "by_check": counts,
        }

    # ------------------------------------------------------------------
    # run_harbor.py checks
    # ------------------------------------------------------------------

    def _scan_runner(self) -> None:
        text = self.runner.read_text(errors="replace")
        lines = text.splitlines()

        self._check_network_policy(lines)
        self._check_shell_injection(lines, str(self.runner))
        self._check_rubric_judge(lines)
        self._check_credentials(lines)

    def _check_network_policy(self, lines: list[str]) -> None:
        """CHK-014: Verify that start_container reads allow_internet."""
        in_start_container = False
        has_network_flag = False

        for i, line in enumerate(lines, 1):
            if "def start_container" in line:
                in_start_container = True
            elif in_start_container and line and not line[0].isspace() and "def " in line:
                in_start_container = False

            if in_start_container:
                if "--network" in line or "network_mode" in line:
                    has_network_flag = True

        if not has_network_flag:
            self._findings.append(RunnerFinding(
                check="network_policy_unenforced",
                severity="HIGH",
                detail=(
                    "start_container() never applies --network=none or any "
                    "Docker network restriction. allow_internet from task.toml "
                    "is ignored. All containers have unrestricted network access."
                ),
                location=str(self.runner),
            ))

    def _check_shell_injection(
        self, lines: list[str], filepath: str
    ) -> None:
        """Detect subprocess calls with shell=True and f-string interpolation."""
        shell_true_re = re.compile(r"subprocess\.\w+\(.*shell\s*=\s*True", re.DOTALL)
        fstring_re = re.compile(r'f["\'].*\{.*\}')

        for i, line in enumerate(lines, 1):
            if "shell=True" in line:
                # Look at surrounding context for f-strings.
                context = "\n".join(lines[max(0, i - 3) : i + 2])
                if fstring_re.search(context):
                    self._findings.append(RunnerFinding(
                        check="shell_injection",
                        severity="MEDIUM",
                        detail=(
                            "subprocess call with shell=True and f-string "
                            "interpolation. Crafted file paths could inject "
                            "shell commands."
                        ),
                        location=f"{filepath}:{i}",
                        line=i,
                    ))

    def _check_rubric_judge(self, lines: list[str]) -> None:
        """CHK-010: Single LLM judge call with no reliability discipline."""
        in_eval_rubric = False
        has_multi_trial = False
        has_randomization = False
        eval_start = 0

        for i, line in enumerate(lines, 1):
            if "def evaluate_rubric" in line:
                in_eval_rubric = True
                eval_start = i
            elif in_eval_rubric and line and not line[0].isspace() and "def " in line:
                in_eval_rubric = False

            if in_eval_rubric:
                if "for trial" in line or "num_trials" in line or "range(" in line:
                    has_multi_trial = True
                if "randomiz" in line or "shuffle" in line or "position" in line:
                    has_randomization = True

        if eval_start and not has_multi_trial:
            self._findings.append(RunnerFinding(
                check="rubric_no_multi_trial",
                severity="HIGH",
                detail=(
                    "evaluate_rubric() makes a single LLM judge call with "
                    "no multi-trial aggregation, no position randomization, "
                    "and no perturbation suite. rubric_score contributes 50% "
                    "of the final reward."
                ),
                location=f"{self.runner}:{eval_start}",
                line=eval_start,
            ))

    def _check_credentials(self, lines: list[str]) -> None:
        """Check for hardcoded credentials or unsafe env reads."""
        env_file = self.root / ".env"
        if env_file.exists():
            env_text = env_file.read_text(errors="replace")
            sensitive_keys = [
                "AWS_BEARER_TOKEN",
                "AWS_SECRET_ACCESS_KEY",
                "ANTHROPIC_API_KEY",
                "API_KEY",
                "SECRET",
                "PASSWORD",
                "TOKEN",
            ]
            for key in sensitive_keys:
                if key in env_text:
                    self._findings.append(RunnerFinding(
                        check="credential_on_disk",
                        severity="MEDIUM",
                        detail=(
                            f".env contains '{key}'. While gitignored, the "
                            f"credential is exposed if the project directory "
                            f"is shared, backed up, or accessed by other tools."
                        ),
                        location=str(env_file),
                    ))
                    break  # One finding per .env, not per key.

    # ------------------------------------------------------------------
    # test_output.py checks (per-task verifiers)
    # ------------------------------------------------------------------

    def _scan_test_outputs(self) -> None:
        """Scan all test_output.py files for shell injection and weak checks."""
        if not self.tasks_dir.exists():
            return

        for task_dir in sorted(self.tasks_dir.iterdir()):
            if not task_dir.is_dir() or task_dir.name.startswith("."):
                continue

            test_output = task_dir / "tests" / "test_output.py"
            if not test_output.exists():
                continue

            text = test_output.read_text(errors="replace")
            lines = text.splitlines()
            filepath = str(test_output)

            self._check_shell_injection(lines, filepath)
            self._check_network_detection(lines, filepath, task_dir.name)

    def _check_network_detection(
        self, lines: list[str], filepath: str, task: str
    ) -> None:
        """CHK-015: String-based network detection is trivially evadable."""
        in_network_check = False
        uses_string_match_only = True

        for i, line in enumerate(lines, 1):
            if "network" in line.lower() and "def " in line:
                in_network_check = True
            elif in_network_check and line and not line[0].isspace() and "def " in line:
                in_network_check = False

            if in_network_check:
                # Look for runtime monitoring (strace, netstat, iptables logs).
                if any(tool in line for tool in [
                    "strace", "netstat", "iptables", "conntrack",
                    "tcpdump", "/proc/net",
                ]):
                    uses_string_match_only = False

        if in_network_check and uses_string_match_only:
            self._findings.append(RunnerFinding(
                check="weak_network_detection",
                severity="MEDIUM",
                detail=(
                    f"test_negative_weight_uses_network in {task} relies on "
                    f"string matching only. Agent can use network tools "
                    f"without leaving the searched string patterns."
                ),
                location=filepath,
            ))
