#!/usr/bin/env python3
"""
CRUCIBLE audit harness CLI for CyberGym-E2E.

Programmatic entry point for scanning, verifying, and reporting on the
CyberGym-E2E benchmark. Implements the Phase 1/2 instrument pipeline
described in trinity/CRUCIBLE.md.

Usage:
    python -m audit.crucible scan
    python -m audit.crucible verify --findings audit/findings.yaml --context audit/evidence.yaml
    python -m audit.crucible report
    python -m audit.crucible status
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import typer
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

AUDIT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = AUDIT_DIR.parent
TASKS_DIR = PROJECT_ROOT / "tasks"
RUNNER_PATH = PROJECT_ROOT / "run_harbor.py"
FINDINGS_PATH = AUDIT_DIR / "findings.yaml"
EVIDENCE_PATH = AUDIT_DIR / "evidence.yaml"
SCOPE_PATH = AUDIT_DIR / "scope.yaml"
SCOPE_APPROVED_PATH = AUDIT_DIR / "scope.approved"
VERDICT_PATH = PROJECT_ROOT / "VERDICT.md"

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="crucible",
    help="CRUCIBLE audit harness for CyberGym-E2E.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Return hex SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f) or {}


def _count_findings(findings_data: dict) -> dict[str, int]:
    """Return finding counts by severity."""
    counts: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for f in findings_data.get("findings", []):
        sev = f.get("severity", "MEDIUM").upper()
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def _get_disposition(findings_data: dict) -> str:
    return findings_data.get("disposition", "UNKNOWN").upper()


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------

@app.command()
def scan(
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Write scan results to this YAML file."),
    ] = AUDIT_DIR / "results" / "scan.yaml",
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Print detailed findings."),
    ] = False,
) -> None:
    """Run checks against the project: task.toml, Docker images, PoCs, runner."""
    from audit.scanners.docker_scanner import DockerScanner
    from audit.scanners.task_scanner import TaskScanner
    from audit.scanners.runner_scanner import RunnerScanner

    typer.echo("CRUCIBLE scan — CyberGym-E2E")
    typer.echo(f"  project root : {PROJECT_ROOT}")
    typer.echo(f"  tasks dir    : {TASKS_DIR}")
    typer.echo(f"  runner       : {RUNNER_PATH}")
    typer.echo()

    all_findings: list[dict] = []
    summaries: dict[str, dict] = {}

    # -- Docker scanner --------------------------------------------------
    typer.echo("[1/3] Docker image scanner ...")
    ds = DockerScanner(TASKS_DIR)
    ds_results = ds.scan()
    cross = ds.cross_task_consistency()
    summaries["docker"] = ds.summary()
    for r in ds_results:
        for f in r.findings:
            entry = {
                "scanner": "docker",
                "task": f.task,
                "check": f.check,
                "severity": f.severity,
                "detail": f.detail,
                "location": f.location,
            }
            all_findings.append(entry)
            if verbose:
                typer.echo(f"  [{f.severity}] {f.task}: {f.detail}")
    for f in cross:
        all_findings.append({
            "scanner": "docker",
            "task": f.task,
            "check": f.check,
            "severity": f.severity,
            "detail": f.detail,
            "location": f.location,
        })

    # -- Task scanner ----------------------------------------------------
    typer.echo("[2/3] Task bundle scanner ...")
    ts = TaskScanner(TASKS_DIR)
    ts_results = ts.scan()
    summaries["task"] = ts.summary()
    for r in ts_results:
        for f in r.findings:
            entry = {
                "scanner": "task",
                "task": f.task,
                "check": f.check,
                "severity": f.severity,
                "detail": f.detail,
                "location": f.location,
            }
            all_findings.append(entry)
            if verbose:
                typer.echo(f"  [{f.severity}] {f.task}: {f.detail}")

    # -- Runner scanner --------------------------------------------------
    typer.echo("[3/3] Runner scanner ...")
    rs = RunnerScanner(PROJECT_ROOT)
    rs_results = rs.scan()
    summaries["runner"] = rs.summary()
    for f in rs_results:
        entry = {
            "scanner": "runner",
            "task": "",
            "check": f.check,
            "severity": f.severity,
            "detail": f.detail,
            "location": f.location,
        }
        all_findings.append(entry)
        if verbose:
            typer.echo(f"  [{f.severity}] {f.detail}")

    # -- Write results ---------------------------------------------------
    output.parent.mkdir(parents=True, exist_ok=True)
    scan_data = {
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "summaries": summaries,
        "findings": all_findings,
    }
    with open(output, "w") as f:
        yaml.dump(scan_data, f, default_flow_style=False, sort_keys=False)

    # -- Summary ---------------------------------------------------------
    total = len(all_findings)
    by_sev: dict[str, int] = {}
    for fi in all_findings:
        s = fi["severity"]
        by_sev[s] = by_sev.get(s, 0) + 1

    typer.echo()
    typer.echo(f"Scan complete: {total} findings")
    for sev in ("HIGH", "MEDIUM", "LOW"):
        if by_sev.get(sev, 0):
            typer.echo(f"  {sev}: {by_sev[sev]}")
    typer.echo(f"Results written to {output}")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

@app.command()
def verify(
    findings: Annotated[
        Path,
        typer.Option(help="Path to findings.yaml"),
    ] = FINDINGS_PATH,
    context: Annotated[
        Path,
        typer.Option(help="Path to evidence.yaml"),
    ] = EVIDENCE_PATH,
) -> None:
    """Verify findings against evidence. Exit 0 only if the gate passes."""
    typer.echo("CRUCIBLE verify — CyberGym-E2E")

    errors: list[str] = []

    # -- Scope approval ---------------------------------------------------
    if not SCOPE_PATH.exists():
        errors.append("audit/scope.yaml is missing.")
    if not SCOPE_APPROVED_PATH.exists():
        errors.append(
            "audit/scope.approved is missing. "
            "Run: shasum -a 256 audit/scope.yaml > audit/scope.approved"
        )
    elif SCOPE_PATH.exists():
        expected = _sha256_file(SCOPE_PATH)
        approved = SCOPE_APPROVED_PATH.read_text().strip().split()[0]
        if expected != approved:
            errors.append(
                f"Scope approval mismatch.\n"
                f"  scope.yaml SHA-256 : {expected}\n"
                f"  scope.approved     : {approved}\n"
                f"Re-approve after reviewing the updated scope."
            )

    # -- Findings exist and parse -----------------------------------------
    if not findings.exists():
        errors.append(f"{findings} not found.")
    else:
        findings_data = _load_yaml(findings)
        disposition = _get_disposition(findings_data)
        counts = _count_findings(findings_data)

        # Gate: no unacknowledged HIGH findings for SHIP.
        if disposition == "SHIP" and counts.get("HIGH", 0) > 0:
            errors.append(
                f"Disposition is SHIP but {counts['HIGH']} HIGH findings remain."
            )

        # All findings must reference valid evidence.
        if context.exists():
            evidence_data = _load_yaml(context)
            evidence_ids = {
                c.get("id") for c in evidence_data.get("checks", [])
            }
            for f in findings_data.get("findings", []):
                ev = f.get("evidence", "")
                if ev and ev.startswith("CHK-") and ev not in evidence_ids:
                    errors.append(
                        f"Finding {f['id']} references evidence '{ev}' "
                        f"which is not in {context}."
                    )

        # Coverage gaps cap at HOLD.
        gaps = findings_data.get("coverage_gaps", [])
        if gaps and disposition == "SHIP":
            errors.append(
                f"Disposition is SHIP but {len(gaps)} coverage gaps remain."
            )

    # -- Evidence exists --------------------------------------------------
    if not context.exists():
        errors.append(f"{context} not found.")

    # -- Verdict ----------------------------------------------------------
    if errors:
        typer.echo()
        typer.echo("VERIFY FAILED")
        for e in errors:
            typer.echo(f"  ✗ {e}")
        raise typer.Exit(code=1)

    typer.echo()
    typer.echo("VERIFY PASSED")
    typer.echo(f"  Disposition: {disposition}")
    typer.echo(f"  Findings: {sum(counts.values())} ({counts})")
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@app.command()
def report(
    findings: Annotated[
        Path,
        typer.Option(help="Path to findings.yaml"),
    ] = FINDINGS_PATH,
) -> None:
    """Generate or update VERDICT.md from findings.yaml."""
    if not findings.exists():
        typer.echo(f"Error: {findings} not found.", err=True)
        raise typer.Exit(code=1)

    data = _load_yaml(findings)
    disposition = _get_disposition(data)
    counts = _count_findings(data)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    scope_digest = _sha256_file(SCOPE_PATH) if SCOPE_PATH.exists() else "MISSING"

    flag = {"SHIP": "🟢", "HOLD": "🟡", "BLOCK": "🔴"}.get(disposition, "⚠️")

    lines = [
        f"# VERDICT — CyberGym-E2E {flag}",
        "",
        f"**Disposition**: {disposition}",
        f"**Generated**: {now}",
        f"**Scope digest**: `{scope_digest[:16]}...`",
        "",
        "## Summary",
        "",
        f"| Severity | Count |",
        f"|----------|-------|",
    ]
    for sev in ("HIGH", "MEDIUM", "LOW"):
        lines.append(f"| {sev} | {counts.get(sev, 0)} |")

    lines.extend([
        "",
        "## Findings",
        "",
    ])

    for f in data.get("findings", []):
        sev = f.get("severity", "MEDIUM")
        icon = {"HIGH": "⛔", "MEDIUM": "⚠️", "LOW": "✅"}.get(sev, "⚠️")
        lines.append(
            f"- **{f['id']}** {icon} [{sev}] {f.get('title', 'untitled')}"
        )
        desc = f.get("description", "").strip()
        if desc:
            lines.append(f"  {desc[:200]}")
        lines.append("")

    # Coverage gaps.
    gaps = data.get("coverage_gaps", [])
    if gaps:
        lines.extend(["## Coverage Gaps", ""])
        for g in gaps:
            lines.append(
                f"- **{g['id']}** ⚠️ {g.get('description', '')} (cap: {g.get('cap', 'HOLD')})"
            )
        lines.append("")

    lines.extend([
        "---",
        f"*Generated by `audit/crucible.py report` at {now}*",
    ])

    verdict_text = "\n".join(lines) + "\n"
    VERDICT_PATH.write_text(verdict_text)
    typer.echo(f"VERDICT.md written ({len(lines)} lines, disposition: {disposition})")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@app.command()
def status() -> None:
    """Show current audit disposition and finding counts."""
    typer.echo("CRUCIBLE status — CyberGym-E2E")
    typer.echo(f"  project: {PROJECT_ROOT}")
    typer.echo()

    # Scope.
    if SCOPE_PATH.exists():
        scope = _load_yaml(SCOPE_PATH)
        typer.echo(f"  scope.yaml        : present")
        run = scope.get("run", {})
        typer.echo(f"    phase           : {run.get('phase', '?')}")
        typer.echo(f"    kind            : {run.get('kind', '?')}")
        proj = scope.get("project", {})
        typer.echo(f"    git SHA         : {proj.get('git_sha', '?')[:12]}")
        typer.echo(f"    dirty           : {proj.get('dirty', '?')}")
    else:
        typer.echo(f"  scope.yaml        : MISSING")

    # Scope approval.
    if SCOPE_APPROVED_PATH.exists():
        approved = SCOPE_APPROVED_PATH.read_text().strip().split()[0]
        current = _sha256_file(SCOPE_PATH) if SCOPE_PATH.exists() else ""
        match = "MATCH" if approved == current else "MISMATCH"
        typer.echo(f"  scope.approved    : {match}")
    else:
        typer.echo(f"  scope.approved    : MISSING")

    # Findings.
    if FINDINGS_PATH.exists():
        data = _load_yaml(FINDINGS_PATH)
        disposition = _get_disposition(data)
        counts = _count_findings(data)
        total = sum(counts.values())
        gaps = len(data.get("coverage_gaps", []))

        flag = {"SHIP": "🟢", "HOLD": "🟡", "BLOCK": "🔴"}.get(disposition, "⚠️")
        typer.echo(f"  disposition       : {flag} {disposition}")
        typer.echo(f"  findings          : {total} total")
        for sev in ("HIGH", "MEDIUM", "LOW"):
            if counts.get(sev, 0):
                typer.echo(f"    {sev:8s}       : {counts[sev]}")
        typer.echo(f"  coverage gaps     : {gaps}")
    else:
        typer.echo(f"  findings.yaml     : MISSING")

    # Evidence.
    if EVIDENCE_PATH.exists():
        ev = _load_yaml(EVIDENCE_PATH)
        n_checks = len(ev.get("checks", []))
        typer.echo(f"  evidence checks   : {n_checks}")
    else:
        typer.echo(f"  evidence.yaml     : MISSING")

    # Tasks.
    if TASKS_DIR.exists():
        tasks = [
            d for d in TASKS_DIR.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        ]
        typer.echo(f"  tasks on disk     : {len(tasks)}")
    else:
        typer.echo(f"  tasks/            : MISSING")

    # VERDICT.md.
    if VERDICT_PATH.exists():
        typer.echo(f"  VERDICT.md        : present ({VERDICT_PATH.stat().st_size} bytes)")
    else:
        typer.echo(f"  VERDICT.md        : MISSING")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app()
