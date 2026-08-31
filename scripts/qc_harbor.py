#!/usr/bin/env python3
"""
qc_harbor.py — Pre-flight QC for harbor task directories.

Runs deterministic structural checks + task correctness validation against a
harbor task directory and reports pass/fail for each.

Exit code 0 = all blocking checks passed.
Exit code 1 = one or more blocking failures.

Usage:
    python scripts/qc_harbor.py tasks/p11-kit__CVE-2026-2100
    python scripts/qc_harbor.py tasks/*                        # glob multiple
    python scripts/qc_harbor.py --all                          # all tasks/
    python scripts/qc_harbor.py --verbose tasks/exiv2__arvo_45993
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ALLOWED_WEIGHTS = {-5, -3, -1, 1, 3, 5}

STAGE_TEST_NAMES = {
    "stage1": [
        "test_stage1_poc_crashes_without_patch",
        "test_agent_poc_crashes_vuln_build",
    ],
    "stage2": [
        "test_stage2_poc_ok_with_patch",
        "test_agent_poc_neutralized_by_patch",
    ],
    "stage3": [
        "test_stage3_tests_pass_with_patch",
        "test_project_suite_passes_with_patch",
    ],
    "stage4": [
        "test_stage4_gt_poc_ok_with_patch",
        "test_ground_truth_poc_neutralized_by_patch",
    ],
}
ALL_STAGE_NAMES = {n for names in STAGE_TEST_NAMES.values() for n in names}

REQUIRED_FILES = {
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "environment/src.tgz",
    "environment/scripts/prepare.sh",
    "environment/scripts/compile.sh",
    "environment/scripts/run_poc.sh",
    "environment/scripts/test.sh",
    "environment/config/config.toml",
    "tests/test.sh",
    "tests/test_output.py",
    "tests/test_weights.json",
    "tests/rubric.json",
    "tests/pytest.json",
    "tests/validate.py",
    "tests/data/poc.bin",
    "solution/fix.patch",
    "solution/poc.bin",
}

JUNK_PATTERNS = [
    "__pycache__", ".pyc", ".DS_Store", ".swp", ".bak", "~",
    ".git", "Thumbs.db",
]

RED_FLAG_PHRASES = [
    "reconstructed", "HONEST STATUS", "has NOT been built",
    "needs_manual", "should work", "offline if primed",
    "placeholder", "not a fuzzer-verified",
    "not present in the payload",
]

BLOCKING = {
    "QC-03", "QC-04", "QC-06", "QC-08", "QC-09", "QC-10", "QC-12", "QC-21",
    "TC-01", "TC-02", "TC-03", "TC-04", "TC-05",
}

BAD_PACKAGES = {"libzlib1g-dev": "zlib1g-dev"}

EXPECTED_BASE_IMAGE = {
    "jvm": "base-builder-jvm",
    "java": "base-builder-jvm",
    "python": "base-builder-python",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_text(path):
    try:
        return path.read_text(errors="replace")
    except Exception:
        return ""


def read_bytes(path):
    try:
        return path.read_bytes()
    except Exception:
        return b""


def find_test_funcs(py_text):
    return re.findall(r"^def (test_\w+)\s*\(", py_text, re.MULTILINE)


def detect_fuzzing_language(compile_sh_text):
    m = re.search(r"FUZZING_LANGUAGE\s*=\s*(\S+)", compile_sh_text)
    return m.group(1).strip("'\"") if m else None


def detect_from_image(dockerfile_text):
    m = re.search(r"^FROM\s+(\S+)", dockerfile_text, re.MULTILINE)
    return m.group(1) if m else ""


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Phase 1: Structural QC (21 checks from qc_harbor.md)
# ---------------------------------------------------------------------------

def run_structural_qc(task_dir, results):
    """21 deterministic structural checks."""

    def check(qc_id, weight, desc, passed, detail=""):
        results.append({
            "id": qc_id, "weight": weight, "desc": desc,
            "passed": passed, "detail": detail,
            "blocking": qc_id in BLOCKING,
        })

    all_files = set()
    for root, dirs, files in os.walk(task_dir):
        for f in files:
            rel = os.path.relpath(os.path.join(root, f), task_dir)
            all_files.add(rel)
        for d in dirs:
            rel = os.path.relpath(os.path.join(root, d), task_dir)
            all_files.add(rel + "/")

    dockerfile = read_text(task_dir / "environment" / "Dockerfile")
    test_sh = read_text(task_dir / "tests" / "test.sh")
    test_output_py = read_text(task_dir / "tests" / "test_output.py")
    compile_sh = read_text(task_dir / "environment" / "scripts" / "compile.sh")
    instr = read_text(task_dir / "instruction.md")

    test_sh_calls_validate = "validate.py" in test_sh
    task_toml_text = read_text(task_dir / "task.toml")
    if "artifacts" in task_toml_text:
        is_patch_only = "/output/poc.bin" not in task_toml_text
    else:
        is_patch_only = "patch-only" in instr.lower() or "cg-verify" in instr
    from_image = detect_from_image(dockerfile)

    # --- QC-01: Harbor folder structure ---
    conditionally_required = set()
    if not test_sh_calls_validate:
        conditionally_required.add("environment/validate.py")
    if "base-builder" in from_image:
        conditionally_required.add("environment/install_validate_deps.sh")

    effective_required = REQUIRED_FILES - conditionally_required
    missing = sorted(r for r in effective_required if not (task_dir / r).exists())
    detail_01 = ""
    if missing:
        detail_01 = f"missing: {missing}"
    cond_missing = sorted(
        r for r in conditionally_required & REQUIRED_FILES
        if r in REQUIRED_FILES and not (task_dir / r).exists()
    )
    if cond_missing:
        detail_01 += ("; " if detail_01 else "") + f"optional-missing: {cond_missing}"
    check("QC-01", 5, "Harbor folder structure complete",
          len(missing) == 0, detail_01)

    # --- QC-02: No junk files + red-flag phrase scan ---
    junk = []
    for f in sorted(all_files):
        for pat in JUNK_PATTERNS:
            if pat in f:
                junk.append(f)
                break
    if "environment/scripts/crash.log" in all_files:
        junk.append("environment/scripts/crash.log (should be in environment/given/)")

    red_flags = []
    scan_exts = (".sh", ".py", ".md", ".toml", ".json", ".cfg", ".txt")
    for f in sorted(all_files):
        if not any(f.endswith(e) for e in scan_exts):
            continue
        if f.startswith("environment/scripts/"):
            continue
        content = read_text(task_dir / f)
        for phrase in RED_FLAG_PHRASES:
            if phrase.lower() in content.lower():
                line_no = next(
                    (i + 1 for i, line in enumerate(content.splitlines())
                     if phrase.lower() in line.lower()), 0
                )
                red_flags.append(f"{f}:{line_no} '{phrase}'")

    detail_02 = ""
    if junk:
        detail_02 = f"junk: {junk}"
    if red_flags:
        detail_02 += ("; " if detail_02 else "") + f"red-flags: {red_flags}"
    check("QC-02", 3, "No junk files, no red-flag phrases",
          len(junk) == 0 and len(red_flags) == 0, detail_02)

    # --- QC-03: No CRLF ---
    crlf_files = []
    for f in sorted(all_files):
        if f.endswith((".sh", ".py", ".md", ".toml", ".json")):
            data = read_bytes(task_dir / f)
            if b"\r\n" in data:
                crlf_files.append(f)
    check("QC-03", 5, "No CRLF line endings",
          len(crlf_files) == 0,
          f"CRLF in: {crlf_files}" if crlf_files else "")

    # --- QC-04: Dockerfile creates required dirs ---
    required_dirs = {"/output": False, "/logs/verifier": False}
    for line in dockerfile.splitlines():
        if "mkdir" not in line.lower():
            continue
        for d in required_dirs:
            if re.search(re.escape(d) + r"(?:\s|$|/)", line):
                required_dirs[d] = True
    missing_dirs = [d for d, found in required_dirs.items() if not found]
    check("QC-04", 5, "Dockerfile creates /output and /logs/verifier",
          len(missing_dirs) == 0,
          f"missing mkdir for: {missing_dirs}" if missing_dirs else "")

    # --- QC-05: Base image + packages ---
    issues_05 = []
    for bad, good in BAD_PACKAGES.items():
        if bad in dockerfile:
            issues_05.append(f"bad package: {bad} -> {good}")

    lang = detect_fuzzing_language(compile_sh)
    if lang and lang.lower() in EXPECTED_BASE_IMAGE:
        expected = EXPECTED_BASE_IMAGE[lang.lower()]
        if expected not in from_image:
            issues_05.append(
                f"FUZZING_LANGUAGE={lang} but FROM={from_image} "
                f"(expected image containing '{expected}')"
            )

    if not re.search(r"^WORKDIR\s+(/src|(\$\{?SRC\}?))\s*$", dockerfile, re.MULTILINE):
        issues_05.append("Dockerfile missing WORKDIR /src")

    tests_use_sudo = "sudo" in test_output_py or "sudo" in read_text(
        task_dir / "tests" / "validate.py"
    )
    base_has_sudo = "base-builder" in from_image
    dockerfile_installs_sudo = bool(re.search(r"apt.*install.*\bsudo\b", dockerfile))
    if tests_use_sudo and not base_has_sudo and not dockerfile_installs_sudo:
        issues_05.append(
            "tests/ use sudo but Dockerfile does not install it "
            "(base image is not base-builder)"
        )

    check("QC-05", 3, "Dockerfile base image and packages correct",
          len(issues_05) == 0,
          "; ".join(issues_05) if issues_05 else
          (f"FROM={from_image}, lang={lang}" if lang else f"FROM={from_image}"))

    # --- QC-06: install_validate_deps.sh does not use uv ---
    ivd_path = task_dir / "environment" / "install_validate_deps.sh"
    ivd_text = read_text(ivd_path) if ivd_path.exists() else ""
    uses_uv = "astral.sh/uv" in ivd_text or "uv venv" in ivd_text or "uv pip" in ivd_text
    is_base_builder = "base-builder" in from_image
    missing_ivd = not ivd_path.exists() and not is_base_builder
    if missing_ivd:
        check("QC-06", 5, "install_validate_deps.sh present and correct",
              False, "missing for non-base-builder image")
    elif ivd_path.exists() and uses_uv:
        check("QC-06", 5, "install_validate_deps.sh does not use uv",
              False, "uses uv (astral.sh) — must use python3 -m venv")
    else:
        check("QC-06", 5, "install_validate_deps.sh correct",
              True, "")

    # --- QC-07: Dockerfile COPY sources exist + build.sh ---
    issues_07 = []
    for m in re.finditer(r"^COPY\s+(\S+)", dockerfile, re.MULTILINE):
        src = m.group(1)
        if src.startswith("--"):
            continue
        src_path = task_dir / "environment" / src
        if not src_path.exists():
            issues_07.append(f"COPY {src}: not found")

    calls_compile_wrapper = bool(
        re.search(r"^\s*compile\s*$", compile_sh, re.MULTILINE)
        or re.search(r"^\s*(sudo\s+)?(-E\s+)?compile\b", compile_sh, re.MULTILINE)
    )
    build_sh = task_dir / "environment" / "scripts" / "build.sh"
    if calls_compile_wrapper and not build_sh.exists():
        issues_07.append(
            "compile.sh calls `compile` (OSS-Fuzz wrapper) but "
            "environment/scripts/build.sh does not exist"
        )

    run_poc_sh = read_text(task_dir / "environment" / "scripts" / "run_poc.sh")
    build_sh_text = read_text(build_sh) if build_sh.exists() else ""
    bin_match = re.search(r'(?:\$OUT_DIR|/out)/(\S+)', run_poc_sh)
    if bin_match:
        binary_name = bin_match.group(1).strip('"\'')
        if binary_name and binary_name not in compile_sh and binary_name not in build_sh_text:
            issues_07.append(
                f"run_poc.sh runs '{binary_name}' but it does not appear in compile.sh or build.sh"
            )

    check("QC-07", 3, "Dockerfile COPY sources exist, build chain intact",
          len(issues_07) == 0,
          "; ".join(issues_07) if issues_07 else "")

    # --- QC-08: test.sh calls validate.py + grading artifacts ---
    issues_08 = []
    uses_cg_verify = "cg-verify" in test_output_py or "verify.json" in test_output_py
    drives_stages = "compile.sh" in test_output_py and "run_poc.sh" in test_output_py
    if not test_sh_calls_validate and not uses_cg_verify and not drives_stages:
        issues_08.append("test.sh does not call validate.py, and test_output.py neither "
                         "drives the stage scripts nor reads a cg-verify record")

    if "reward.json" not in test_sh and "reward_json" not in test_sh:
        issues_08.append("test.sh does not write reward.json")
    if "ctrf.json" not in test_sh and "ctrf_json" not in test_sh:
        issues_08.append("test.sh does not write ctrf.json (stages will be null)")

    MASK_TAILS = ("|| true", "|| exit 0", "2>/dev/null")
    BUILD_KWS = ("compile", "make ", "cmake", "build", "gcc", "clang", "javac", "mvn", "gradle")
    CLEANUP_KWS = ("make clean", "make distclean", "rm ", "kill ")
    for script_name in ("compile.sh", "run_poc.sh", "test.sh"):
        spath = task_dir / "environment" / "scripts" / script_name
        stext = read_text(spath) if spath.exists() else ""
        for i, line in enumerate(stext.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            has_mask = any(stripped.endswith(t) for t in MASK_TAILS)
            if has_mask:
                low = stripped.lower()
                is_build = any(kw in low for kw in BUILD_KWS)
                is_cleanup = any(kw in low for kw in CLEANUP_KWS)
                if is_build and not is_cleanup:
                    issues_08.append(
                        f"environment/scripts/{script_name}:{i} masks build failure"
                    )

    if "set +e" in compile_sh:
        plus_e_count = len(re.findall(r"^\s*set\s+\+e", compile_sh, re.MULTILINE))
        minus_e_count = len(re.findall(r"^\s*set\s+-e", compile_sh, re.MULTILINE))
        if plus_e_count > minus_e_count:
            issues_08.append("compile.sh has unpaired 'set +e' (errors silently ignored)")

    check("QC-08", 5, "test.sh calls validate.py and emits grading artifacts",
          len(issues_08) == 0,
          "; ".join(issues_08) if issues_08 else "")

    # --- QC-09: test_output.py reads oracle.json, not report.json ---
    reads_report = ("report.json" in test_output_py
                    and "oracle.json" not in test_output_py
                    and "verify.json" not in test_output_py)
    check("QC-09", 5, "test_output.py reads oracle.json, not report.json",
          not reads_report,
          "reads report.json which is never generated in verifier flow" if reads_report else "")

    # --- QC-10: Stage function names match ---
    test_funcs = find_test_funcs(test_output_py)
    matched_stages = {}
    for stage, valid_names in STAGE_TEST_NAMES.items():
        for fn in test_funcs:
            if fn in valid_names:
                matched_stages[stage] = fn
                break
    missing_stages = [s for s in STAGE_TEST_NAMES if s not in matched_stages]
    if is_patch_only:
        check("QC-10", 5, "Stage function names match STAGE_TEST_NAMES", True,
              "patch-only task: graded from the driver record, no stage oracle")
    else:
        check("QC-10", 5, "Stage function names match STAGE_TEST_NAMES",
              len(missing_stages) == 0,
              f"missing stages: {missing_stages}, matched: {matched_stages}"
              if missing_stages else f"matched: {matched_stages}")

    # --- QC-11: test_weights.json values ---
    weights_path = task_dir / "tests" / "test_weights.json"
    weights = {}
    bad_values = []
    if weights_path.exists():
        try:
            weights = json.loads(weights_path.read_text())
            for k, v in weights.items():
                if not isinstance(v, (int, float)):
                    bad_values.append(f"{k}={type(v).__name__}")
                elif v not in ALLOWED_WEIGHTS:
                    bad_values.append(f"{k}={v}")
        except json.JSONDecodeError:
            bad_values.append("invalid JSON")
    pos_sum = sum(v for v in weights.values() if isinstance(v, (int, float)) and v > 0)
    if not bad_values and weights and pos_sum <= 0:
        bad_values.append(f"positive weight sum is {pos_sum} (grader denominator would be 0)")
    check("QC-11", 3, "test_weights.json values valid, positive sum > 0",
          len(bad_values) == 0,
          f"bad values: {bad_values}" if bad_values else "")

    # --- QC-12: Bijection check ---
    weight_keys = set(weights.keys())
    func_set = set(test_funcs)
    orphaned = sorted(weight_keys - func_set)
    unweighted = sorted(func_set - weight_keys)
    check("QC-12", 5, "test_weights.json <-> test_output.py bijection",
          len(orphaned) == 0 and len(unweighted) == 0,
          f"orphaned weights: {orphaned}, unweighted funcs: {unweighted}"
          if orphaned or unweighted else "")

    # --- QC-13: Max 20 test functions ---
    check("QC-13", 1, "At most 20 test functions",
          len(test_funcs) <= 20,
          f"found {len(test_funcs)} test functions"
          if len(test_funcs) > 20 else f"{len(test_funcs)} functions")

    # --- QC-14: Negative-weight naming + run_poc.sh PoC reference ---
    issues_14 = []
    for k, v in weights.items():
        if isinstance(v, (int, float)) and v < 0 and "negative_weight" not in k:
            issues_14.append(f"{k}={v} (negative weight without 'negative_weight' in name)")

    poc_refs = ("poc.bin", "$1", "POC_PATH", "${1:-")
    if run_poc_sh and not any(r in run_poc_sh for r in poc_refs):
        issues_14.append("run_poc.sh does not reference the PoC file")

    check("QC-14", 3, "Negative-weight naming + run_poc.sh references PoC",
          len(issues_14) == 0,
          "; ".join(issues_14) if issues_14 else "")

    # --- QC-15: instruction.md output paths ---
    has_output_poc = "/output/poc.bin" in instr
    has_output_patch = "/output/fix.patch" in instr
    issues_15 = []
    if is_patch_only:
        if "poc.bin" not in dockerfile:
            issues_15.append("patch-only task: Dockerfile stages no poc.bin for the agent")
        elif "poc.bin" not in instr:
            issues_15.append("patch-only task: instruction.md never tells the agent where the PoC is")
    else:
        if not has_output_poc:
            issues_15.append("missing /output/poc.bin reference")
        if not has_output_patch:
            issues_15.append("missing /output/fix.patch reference")
    check("QC-15", 3, "instruction.md references /output/ paths",
          len(issues_15) == 0,
          "; ".join(issues_15) if issues_15 else "")

    # --- QC-16: config.toml ---
    config_text = read_text(task_dir / "environment" / "config" / "config.toml")
    has_repo = "repo_to_patch" in config_text
    leaks = [f for f in ["vul_commit", "patch_commit", "target_prog"] if f in config_text]
    check("QC-16", 1, "config/config.toml has required fields, no leaks",
          has_repo and len(leaks) == 0,
          f"leaks: {leaks}" if leaks else ("missing repo_to_patch" if not has_repo else ""))

    # --- QC-17: Ground-truth PoC + reference patch ---
    gt_tests = task_dir / "tests" / "data" / "poc.bin"
    gt_solution = task_dir / "solution" / "poc.bin"
    fix_patch = task_dir / "solution" / "fix.patch"
    issues_17 = []
    if not gt_tests.exists() or gt_tests.stat().st_size == 0:
        issues_17.append("tests/data/poc.bin missing or empty")
    if not gt_solution.exists() or gt_solution.stat().st_size == 0:
        issues_17.append("solution/poc.bin missing or empty")
    if gt_tests.exists() and gt_solution.exists():
        if read_bytes(gt_tests) != read_bytes(gt_solution):
            issues_17.append("solution/poc.bin != tests/data/poc.bin (should be identical)")
    if not fix_patch.exists() or fix_patch.stat().st_size == 0:
        issues_17.append("solution/fix.patch missing or empty")
    elif fix_patch.exists():
        patch_text = read_text(fix_patch)
        has_diff_header = (
            bool(re.search(r"^--- ", patch_text, re.MULTILINE)
                 and re.search(r"^\+\+\+ ", patch_text, re.MULTILINE))
            or patch_text.startswith("diff ")
        )
        if not has_diff_header:
            issues_17.append("solution/fix.patch is not a unified diff (no --- / +++ headers)")
    check("QC-17", 3, "Ground-truth PoC and reference patch valid",
          len(issues_17) == 0,
          "; ".join(issues_17) if issues_17 else "")

    # --- QC-18: rubric.json and pytest.json valid ---
    json_issues = []
    for fname in ("rubric.json", "pytest.json"):
        fp = task_dir / "tests" / fname
        if fp.exists():
            try:
                data = json.loads(fp.read_text())
                if not isinstance(data, list):
                    json_issues.append(f"{fname}: not a JSON array")
                elif fname == "rubric.json":
                    for idx, elem in enumerate(data):
                        if not isinstance(elem, dict):
                            json_issues.append(f"rubric.json[{idx}]: not a dict")
                            break
                        if "criterion" not in elem:
                            json_issues.append(f"rubric.json[{idx}]: missing 'criterion' key")
                            break
            except json.JSONDecodeError as e:
                json_issues.append(f"{fname}: invalid JSON ({e})")
        else:
            json_issues.append(f"{fname}: missing")
    check("QC-18", 1, "rubric.json and pytest.json are valid JSON arrays",
          len(json_issues) == 0,
          "; ".join(json_issues) if json_issues else "")

    # --- QC-19: Shell scripts have shebangs ---
    no_shebang = []
    for sh_rel in all_files:
        if sh_rel.endswith(".sh"):
            data = read_bytes(task_dir / sh_rel)
            if not data.startswith(b"#!/"):
                no_shebang.append(sh_rel)
    check("QC-19", 1, "Shell scripts have shebangs",
          len(no_shebang) == 0,
          f"missing shebang: {no_shebang}" if no_shebang else "")

    # --- QC-20: No answer leakage ---
    leakage = []
    gt_poc_bytes = read_bytes(gt_tests)
    skip_prefixes = ("environment/validate.py", "environment/install_validate_deps.sh",
                     "environment/given/")
    for f in sorted(all_files):
        if not f.startswith("environment/"):
            continue
        if any(f.startswith(s) or f == s for s in skip_prefixes):
            continue
        fp = task_dir / f
        if fp.is_file() and gt_poc_bytes and fp.suffix == ".bin":
            if read_bytes(fp) == gt_poc_bytes:
                leakage.append(f"{f} is identical to tests/data/poc.bin (GT PoC leaked to agent)")
    sol_patch = read_bytes(fix_patch) if fix_patch.exists() else b""
    for f in sorted(all_files):
        if f.startswith("environment/") and f.endswith(".patch"):
            if sol_patch and read_bytes(task_dir / f) == sol_patch:
                leakage.append(f"{f} is identical to solution/fix.patch (answer leaked)")
    check("QC-20", -5, "No secrets or answer leakage in environment/",
          len(leakage) == 0,
          "; ".join(leakage) if leakage else "")

    # --- QC-21: fix.patch applies to src.tgz ---
    patch_issues = []
    src_tgz = task_dir / "environment" / "src.tgz"
    if fix_patch.exists() and src_tgz.exists() and fix_patch.stat().st_size > 0:
        patch_content = read_text(fix_patch)
        repo_match = re.search(r'repo_to_patch\s*=\s*"([^"]+)"', config_text)
        repo_name = repo_match.group(1) if repo_match else ""
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                tar_rc = subprocess.run(
                    ["tar", "xzf", str(src_tgz), "-C", tmpdir],
                    capture_output=True, timeout=30,
                )
                if tar_rc.returncode != 0:
                    patch_issues.append(
                        f"src.tgz extraction failed: {tar_rc.stderr.decode(errors='replace')[:200]}"
                    )
                else:
                    # Try applying from repo subdir first, then from a parent
                    # that mirrors the Docker layout (src.tgz extracts flat
                    # into /src/<repo_name>/, patch paths are <repo_name>/file).
                    applied = False
                    attempts = []

                    repo_dir = os.path.join(tmpdir, repo_name) if repo_name else None
                    if repo_dir and os.path.isdir(repo_dir):
                        attempts.append(("repo_subdir", repo_dir))
                    attempts.append(("tmpdir", tmpdir))
                    # Mirror Docker layout: src.tgz extracts flat, Dockerfile
                    # puts it into /src/<repo>. Patch uses <repo>/file paths.
                    if repo_name and not (repo_dir and os.path.isdir(repo_dir)):
                        parent = os.path.join(tmpdir, "_parent")
                        target = os.path.join(parent, repo_name)
                        os.makedirs(parent, exist_ok=True)
                        os.symlink(tmpdir, target)
                        attempts.append(("parent_with_repo_link", parent))

                    for label, cwd in attempts:
                        dry = subprocess.run(
                            ["patch", "--dry-run", "--batch", "-p1"],
                            input=patch_content.encode(),
                            capture_output=True, cwd=cwd, timeout=30,
                        )
                        out = dry.stdout.decode(errors="replace")
                        bad_lines = [
                            l for l in out.splitlines()
                            if "failed" in l.lower() or "ignored" in l
                            or "No file to patch" in l
                        ]
                        if dry.returncode == 0 and not bad_lines:
                            applied = True
                            break

                    if not applied:
                        last_out = dry.stdout.decode(errors="replace")
                        bad_lines = [
                            l for l in last_out.splitlines()
                            if "failed" in l.lower() or "ignored" in l
                            or "No file to patch" in l
                        ]
                        if bad_lines:
                            patch_issues.append(
                                f"fix.patch does not apply ({len(bad_lines)} "
                                f"problem(s): {bad_lines[0][:200]})"
                            )
                        else:
                            patch_issues.append("fix.patch does not apply to src.tgz")
            except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                patch_issues.append(f"patch dry-run error: {exc}")
    check("QC-21", 5, "fix.patch applies cleanly to src.tgz",
          len(patch_issues) == 0,
          "; ".join(patch_issues) if patch_issues else "")

    return test_funcs, weights, is_patch_only


# ---------------------------------------------------------------------------
# Phase 2: Task Correctness Validation (TC checks)
# ---------------------------------------------------------------------------

def run_task_correctness(task_dir, results, test_funcs, weights, is_patch_only):
    """Validate the task is correct, fair, and not cheatable."""

    def check(tc_id, weight, desc, passed, detail=""):
        results.append({
            "id": tc_id, "weight": weight, "desc": desc,
            "passed": passed, "detail": detail,
            "blocking": tc_id in BLOCKING,
        })

    test_output_py = read_text(task_dir / "tests" / "test_output.py")
    test_sh_verifier = read_text(task_dir / "tests" / "test.sh")
    instr = read_text(task_dir / "instruction.md")
    dockerfile = read_text(task_dir / "environment" / "Dockerfile")
    config_text = read_text(task_dir / "environment" / "config" / "config.toml")

    # --- TC-01: Anti-cheat — negative-weight tests present ---
    neg_tests = [k for k, v in weights.items() if isinstance(v, (int, float)) and v < 0]
    # Only require immutable cheat detector if the task declares immutable files
    immutable_match = re.search(r"immutable_files\s*=\s*\[([^\]]*)\]", config_text)
    has_immutable_files = (immutable_match is not None
                           and immutable_match.group(1).strip() != "")
    cheat_categories = {
        "network": False,
        "empty_patch": False,
    }
    if has_immutable_files:
        cheat_categories["immutable"] = False
    for t in neg_tests:
        low = t.lower()
        if "network" in low or "internet" in low or "uses_network" in low:
            cheat_categories["network"] = True
        if "empty" in low or "empty_patch" in low:
            cheat_categories["empty_patch"] = True
        if "immutable" in low:
            cheat_categories["immutable"] = True

    missing_cheats = [k for k, v in cheat_categories.items() if not v]
    check("TC-01", 5, "Anti-cheat: negative-weight tests cover key cheat vectors",
          len(missing_cheats) == 0,
          f"missing cheat detectors: {missing_cheats}; have: {neg_tests}"
          if missing_cheats else f"cheat detectors: {neg_tests}")

    # --- TC-02: GT PoC not accessible to agent ---
    issues_tc02 = []
    # Check Dockerfile doesn't COPY tests/data/ into agent-visible location
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.match(r"COPY\s+.*data/poc\.bin", stripped):
            if "/verifier/" not in stripped:
                issues_tc02.append(f"Dockerfile copies GT poc.bin to agent-visible path: {stripped}")
    # Check instruction.md doesn't reference GT paths
    gt_path_leaks = []
    for bad_path in ["/verifier/data", "/tests/data/poc.bin", "tests/data/poc.bin",
                     "/verifier/poc.bin"]:
        if bad_path in instr:
            gt_path_leaks.append(bad_path)
    if gt_path_leaks:
        issues_tc02.append(f"instruction.md leaks GT paths: {gt_path_leaks}")
    check("TC-02", 5, "Ground-truth PoC not accessible to agent",
          len(issues_tc02) == 0,
          "; ".join(issues_tc02) if issues_tc02 else "")

    # --- TC-03: Solution patch not leaked to agent ---
    issues_tc03 = []
    sol_patch = task_dir / "solution" / "fix.patch"
    sol_patch_bytes = read_bytes(sol_patch) if sol_patch.exists() else b""
    sol_hash = sha256_bytes(sol_patch_bytes) if sol_patch_bytes else ""
    # Check no file in environment/ has the same hash
    env_dir = task_dir / "environment"
    if env_dir.is_dir():
        for f in sorted(os.listdir(env_dir)):
            fp = env_dir / f
            if fp.is_file() and f.endswith((".patch", ".diff")):
                if read_bytes(fp) == sol_patch_bytes:
                    issues_tc03.append(f"environment/{f} is identical to solution/fix.patch")
    # Check instruction.md doesn't contain the actual patch content
    if sol_patch.exists():
        patch_text = read_text(sol_patch)
        # Extract unique diff lines (actual code changes)
        changed_lines = [l for l in patch_text.splitlines()
                         if (l.startswith("+") and not l.startswith("+++"))
                         or (l.startswith("-") and not l.startswith("---"))]
        if changed_lines:
            # Check if >50% of changed lines appear in instruction.md
            found = sum(1 for l in changed_lines if l[1:].strip() in instr)
            if len(changed_lines) > 3 and found > len(changed_lines) * 0.5:
                issues_tc03.append(
                    f"instruction.md contains {found}/{len(changed_lines)} "
                    "changed lines from the solution patch")
    check("TC-03", 5, "Solution patch not leaked to agent",
          len(issues_tc03) == 0,
          "; ".join(issues_tc03) if issues_tc03 else "")

    # --- TC-04: Network lockdown enforced ---
    issues_tc04 = []
    has_net_lockdown = ("iptables" in dockerfile or "NET_ADMIN" in dockerfile
                        or "network" in dockerfile.lower())
    # The harness (run_harbor.py) does lockdown via lockdown_agent_network(),
    # so the Dockerfile doesn't need it. But instruction.md should warn.
    if "network" not in instr.lower() and "internet" not in instr.lower():
        issues_tc04.append("instruction.md does not mention network/internet restriction")
    # Check that no script downloads during agent phase
    for script_name in ("compile.sh", "run_poc.sh", "test.sh", "prepare.sh"):
        spath = task_dir / "environment" / "scripts" / script_name
        stext = read_text(spath) if spath.exists() else ""
        for i, line in enumerate(stext.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for dl_cmd in ("curl ", "wget ", "git clone", "pip install", "apt-get install",
                           "apt install", "npm install"):
                if dl_cmd in stripped:
                    issues_tc04.append(
                        f"environment/scripts/{script_name}:{i} "
                        f"downloads at runtime: '{stripped[:80]}'")
    check("TC-04", 5, "No runtime downloads in agent scripts (network locked)",
          len(issues_tc04) == 0,
          "; ".join(issues_tc04) if issues_tc04 else "")

    # --- TC-05: Immutable files declared and enforced ---
    # Only blocking when task declares immutable files but has no test for it.
    # Tasks with no immutable files (empty or absent) don't need this test.
    issues_tc05 = []
    has_immutable_test = any("immutable" in t.lower() for t in test_funcs)
    if has_immutable_files:
        if not has_immutable_test:
            issues_tc05.append(
                f"immutable_files declared but no test enforces it")
    check("TC-05", 5, "Immutable files declared and enforced",
          len(issues_tc05) == 0,
          "; ".join(issues_tc05) if issues_tc05 else
          ("no immutable_files declared (OK)" if not has_immutable_files
           else "declared and enforced"))

    # --- TC-06: Instruction quality ---
    issues_tc06 = []
    # Must mention validate.py usage
    if "validate.py" not in instr:
        issues_tc06.append("instruction.md doesn't mention validate.py for self-testing")
    # Must mention the project directory
    if "/src/" not in instr:
        issues_tc06.append("instruction.md doesn't reference /src/ project directory")
    # Must mention output contract
    if "/output/" not in instr:
        issues_tc06.append("instruction.md doesn't reference /output/ directory")
    # Should not contain CVE/bug ID that would let agent search for it
    # (this is an info leak, not blocking)
    check("TC-06", 3, "Instruction quality and completeness",
          len(issues_tc06) == 0,
          "; ".join(issues_tc06) if issues_tc06 else "")

    # --- TC-07: Verifier isolation (test.sh in tests/) ---
    issues_tc07 = []
    # Verifier test.sh should write to /logs/verifier/
    if "/logs/verifier" not in test_sh_verifier:
        issues_tc07.append("tests/test.sh does not write to /logs/verifier/")
    # Verifier should import test_output.py
    if "test_output" not in test_sh_verifier:
        issues_tc07.append("tests/test.sh does not import test_output.py")
    # test_output.py should reference /verifier/data for GT poc
    if "/verifier/data" not in test_output_py and "DATA_DIR" not in test_output_py:
        issues_tc07.append("test_output.py does not reference /verifier/data for GT PoC")
    check("TC-07", 3, "Verifier isolation (reads from /verifier/, writes to /logs/)",
          len(issues_tc07) == 0,
          "; ".join(issues_tc07) if issues_tc07 else "")

    # --- TC-08: Scoring sanity ---
    issues_tc08 = []
    pos_sum = sum(v for v in weights.values() if isinstance(v, (int, float)) and v > 0)
    neg_sum = sum(v for v in weights.values() if isinstance(v, (int, float)) and v < 0)
    stage_weight_sum = 0
    for k, v in weights.items():
        for stage, names in STAGE_TEST_NAMES.items():
            if k in names:
                stage_weight_sum += v
    if pos_sum == 0:
        issues_tc08.append("no positive weights (reward always 0)")
    if neg_sum == 0 and not is_patch_only:
        issues_tc08.append("no negative weights (no cheat penalty)")
    # Stage tests should carry significant weight
    stage_ratio = stage_weight_sum / pos_sum if pos_sum > 0 else 0
    if stage_ratio < 0.5:
        issues_tc08.append(
            f"4-stage tests carry only {stage_ratio:.0%} of positive weight "
            f"({stage_weight_sum}/{pos_sum})")
    check("TC-08", 3, "Scoring sanity (stages weighted, cheats penalized)",
          len(issues_tc08) == 0,
          "; ".join(issues_tc08) if issues_tc08 else
          f"positive={pos_sum}, negative={neg_sum}, stage_ratio={stage_ratio:.0%}")

    # --- TC-09: Patch complexity assessment ---
    fix_patch = task_dir / "solution" / "fix.patch"
    patch_text = read_text(fix_patch) if fix_patch.exists() else ""
    changed_lines = [l for l in patch_text.splitlines()
                     if (l.startswith("+") and not l.startswith("+++"))
                     or (l.startswith("-") and not l.startswith("---"))]
    files_changed = set(re.findall(r"^\+\+\+ b/(.+)$", patch_text, re.MULTILINE))
    n_changed = len(changed_lines)
    n_files = len(files_changed)

    if n_changed == 0:
        difficulty = "INVALID"
    elif n_changed <= 10:
        difficulty = "EASY"
    elif n_changed <= 50:
        difficulty = "MEDIUM"
    elif n_changed <= 200:
        difficulty = "HARD"
    else:
        difficulty = "VERY HARD"
    check("TC-09", 0, "Patch complexity assessment",
          True,
          f"difficulty={difficulty}, changed_lines={n_changed}, files={n_files}, "
          f"files_changed={sorted(files_changed)}")

    # --- TC-10: PoC plausibility ---
    issues_tc10 = []
    gt_poc = task_dir / "tests" / "data" / "poc.bin"
    poc_size = gt_poc.stat().st_size if gt_poc.exists() else 0
    if poc_size == 0:
        issues_tc10.append("GT PoC is empty (0 bytes)")
    elif poc_size > 10 * 1024 * 1024:
        issues_tc10.append(f"GT PoC is very large ({poc_size / 1024 / 1024:.1f} MB)")
    # Check run_poc.sh feeds the poc to the right binary
    run_poc = read_text(task_dir / "environment" / "scripts" / "run_poc.sh")
    if not run_poc:
        issues_tc10.append("run_poc.sh is empty or missing")
    check("TC-10", 1, "PoC plausibility (size, feeding mechanism)",
          len(issues_tc10) == 0,
          "; ".join(issues_tc10) if issues_tc10 else f"poc_size={poc_size} bytes")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(task_dir, results, verbose=False):
    print(f"\n{'=' * 72}")
    print(f"  QC REPORT: {task_dir.name}")
    print(f"{'=' * 72}")

    blocking_fails = []
    warning_fails = []
    pass_count = 0
    total = len(results)

    # Group by section
    qc_results = [r for r in results if r["id"].startswith("QC-")]
    tc_results = [r for r in results if r["id"].startswith("TC-")]

    for section_name, section_results in [
        ("Structural Checks (QC)", qc_results),
        ("Task Correctness (TC)", tc_results),
    ]:
        if not section_results:
            continue
        print(f"\n  --- {section_name} ---")
        for r in section_results:
            status = "PASS" if r["passed"] else ("BLOCK" if r["blocking"] else "WARN")
            marker = "+" if r["passed"] else "X"
            line = f"  [{marker}] {r['id']} ({status:5s}) {r['desc']}"
            print(line)
            if r["detail"] and (verbose or not r["passed"]):
                for detail_line in r["detail"].split("; "):
                    print(f"        {detail_line[:300]}")
            if r["passed"]:
                pass_count += 1
            elif r["blocking"]:
                blocking_fails.append(r["id"])
            else:
                warning_fails.append(r["id"])

    print(f"\n{'─' * 72}")
    verdict = "PASS" if not blocking_fails else "FAIL"
    print(f"  QC {verdict}: {pass_count}/{total} passed, "
          f"{len(blocking_fails)} blocking, {len(warning_fails)} warnings")
    if blocking_fails:
        print(f"  Blocking: {', '.join(blocking_fails)}")
    if warning_fails:
        print(f"  Warnings: {', '.join(warning_fails)}")
    print(f"{'=' * 72}\n")

    return len(blocking_fails) == 0


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------

def write_json_report(task_dir, results, out_path):
    report = {
        "task": task_dir.name,
        "path": str(task_dir),
        "checks": results,
        "blocking_failures": [r["id"] for r in results if not r["passed"] and r["blocking"]],
        "warnings": [r["id"] for r in results if not r["passed"] and not r["blocking"]],
        "passed": all(r["passed"] for r in results if r["blocking"]),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_full_qc(task_dir, verbose=False):
    task_dir = Path(task_dir).resolve()
    results = []
    test_funcs, weights, is_patch_only = run_structural_qc(task_dir, results)
    run_task_correctness(task_dir, results, test_funcs, weights, is_patch_only)
    return results


def main():
    ap = argparse.ArgumentParser(
        description="QC validator for CyberGym-E2E harbor tasks")
    ap.add_argument("task_dirs", nargs="*",
                    help="Task directory paths to validate")
    ap.add_argument("--all", action="store_true",
                    help="Validate all tasks in tasks/")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Show details for passing checks too")
    ap.add_argument("--json", metavar="DIR",
                    help="Write JSON reports to DIR (one per task)")
    ap.add_argument("--summary", action="store_true",
                    help="Print summary table at the end")
    args = ap.parse_args()

    if args.all:
        tasks_root = Path(__file__).parent.parent / "tasks"
        task_dirs = sorted(
            d for d in tasks_root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    elif args.task_dirs:
        task_dirs = [Path(d) for d in args.task_dirs]
    else:
        ap.error("provide task directories or --all")

    all_pass = True
    summary_rows = []

    for task_dir in task_dirs:
        if not task_dir.is_dir():
            print(f"ERROR: {task_dir} is not a directory")
            all_pass = False
            continue

        results = run_full_qc(task_dir, verbose=args.verbose)
        ok = print_report(task_dir, results, verbose=args.verbose)

        if args.json:
            json_path = Path(args.json) / f"{task_dir.name}.json"
            write_json_report(task_dir, results, json_path)

        blocking = [r["id"] for r in results if not r["passed"] and r["blocking"]]
        warnings = [r["id"] for r in results if not r["passed"] and not r["blocking"]]
        passed = sum(1 for r in results if r["passed"])
        total = len(results)

        # Extract difficulty from TC-09
        tc09 = next((r for r in results if r["id"] == "TC-09"), None)
        difficulty = ""
        if tc09 and tc09["detail"]:
            m = re.search(r"difficulty=(\w+)", tc09["detail"])
            if m:
                difficulty = m.group(1)

        summary_rows.append({
            "task": task_dir.name,
            "passed": passed,
            "total": total,
            "blocking": len(blocking),
            "warnings": len(warnings),
            "verdict": "PASS" if ok else "FAIL",
            "difficulty": difficulty,
        })

        if not ok:
            all_pass = False

    if args.summary and len(summary_rows) > 1:
        print(f"\n{'=' * 80}")
        print(f"  SUMMARY ({len(summary_rows)} tasks)")
        print(f"{'=' * 80}")
        print(f"  {'Task':<45} {'Result':>6} {'Pass':>5} {'Block':>6} {'Warn':>5} {'Diff':>10}")
        print(f"  {'─' * 45} {'─' * 6} {'─' * 5} {'─' * 6} {'─' * 5} {'─' * 10}")
        for row in summary_rows:
            print(f"  {row['task']:<45} {row['verdict']:>6} "
                  f"{row['passed']:>3}/{row['total']:<2} "
                  f"{row['blocking']:>5} {row['warnings']:>5} "
                  f"{row['difficulty']:>10}")
        fail_count = sum(1 for r in summary_rows if r["verdict"] == "FAIL")
        pass_count = sum(1 for r in summary_rows if r["verdict"] == "PASS")
        print(f"\n  Total: {pass_count} PASS, {fail_count} FAIL")
        print(f"{'=' * 80}\n")

    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
