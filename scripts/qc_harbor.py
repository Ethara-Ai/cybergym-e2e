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
import tarfile
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The stage-name table is shared with run_harbor.py (scripts/stage_names.py):
# a task the gate accepts is, by construction, a task the runner can map.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_names import (  # noqa: E402
    STAGE_TEST_NAMES, ALL_STAGE_NAMES, load_task_stage_map, stage_table, task_mode,
)

# Weights are nonzero integers with |w| <= MAX_ABS_WEIGHT.  Both scales in
# use (the template's 15/15/10/8 and the hand-written 5/3/1) satisfy this;
# the old fixed set {-5,-3,-1,1,3,5} rejected every template-generated task.
MAX_ABS_WEIGHT = 20


def weight_ok(v):
    return (isinstance(v, int) and not isinstance(v, bool)
            and v != 0 and abs(v) <= MAX_ABS_WEIGHT)


def strip_comments(text):
    """Drop `# ...` comments (outside quotes) so a substring check cannot be
    satisfied by a comment.  Shell, Dockerfile and Python share the syntax."""
    out = []
    for line in text.splitlines():
        in_s = in_d = False
        cut = None
        for i, ch in enumerate(line):
            if ch == "'" and not in_d:
                in_s = not in_s
            elif ch == '"' and not in_s:
                in_d = not in_d
            elif ch == "#" and not in_s and not in_d:
                cut = i
                break
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def test_func_bodies(py_text):
    """{test_name: body_text} so a check can look past the function name."""
    bodies = {}
    for m in re.finditer(r"^def (test_\w+)\s*\([^)]*\):\n((?:[ \t]+.*\n|\n)*)", py_text, re.M):
        bodies[m.group(1)] = m.group(2)
    return bodies


def body_is_trivial(body):
    code = strip_comments(body)
    code = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", code)
    return not re.search(r"\b(assert|raise)\b", code)


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
    "QC-01", "QC-03", "QC-04", "QC-06", "QC-08", "QC-09", "QC-10", "QC-11",
    "QC-12", "QC-17", "QC-20", "QC-21",
    "TC-01", "TC-02", "TC-03", "TC-04", "TC-05", "TC-08",
    "RR-01",
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

    dockerfile_code = strip_comments(dockerfile)
    test_sh_code = strip_comments(test_sh)
    test_output_code = strip_comments(test_output_py)
    test_sh_calls_validate = "validate.py" in test_sh_code
    is_patch_only = task_mode(task_dir) == "patch-only"   # same function as the runner
    from_image = detect_from_image(dockerfile_code)

    # --- QC-01: Harbor folder structure ---
    conditionally_required = set()
    if not test_sh_calls_validate:
        conditionally_required.add("environment/validate.py")
        conditionally_required.add("tests/validate.py")
    if "base-builder" in from_image:
        conditionally_required.add("environment/install_validate_deps.sh")
    if is_patch_only:
        conditionally_required.add("solution/poc.bin")

    effective_required = REQUIRED_FILES - conditionally_required
    missing = sorted(r for r in effective_required if not (task_dir / r).exists())
    detail_01 = ""
    if missing:
        detail_01 = f"missing: {missing}"
    cond_missing = sorted(r for r in conditionally_required if not (task_dir / r).exists())
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
    for line in dockerfile_code.splitlines():
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
        check("QC-06", 5, "install_validate_deps.sh present and correct",
              False, "uses uv (astral.sh) — must use python3 -m venv")
    else:
        check("QC-06", 5, "install_validate_deps.sh present and correct",
              True, "")

    # --- QC-07: Dockerfile COPY sources exist + build.sh ---
    issues_07 = []
    for m in re.finditer(r"^COPY\s+(.+)$", dockerfile_code, re.MULTILINE):
        tokens = [t for t in m.group(1).split() if not t.startswith("--")]
        for src in tokens[:-1]:          # every source; the last token is the destination
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
    uses_cg_verify = "cg-verify" in test_output_code or "verify.json" in test_output_code
    drives_stages = "compile.sh" in test_output_code and "run_poc.sh" in test_output_code
    if not test_sh_calls_validate and not uses_cg_verify and not drives_stages:
        issues_08.append("test.sh does not call validate.py, and test_output.py neither "
                         "drives the stage scripts nor reads a cg-verify record")

    if "reward.json" not in test_sh_code and "reward_json" not in test_sh_code:
        issues_08.append("test.sh does not write reward.json")
    if "ctrf.json" not in test_sh_code and "ctrf_json" not in test_sh_code:
        issues_08.append("test.sh does not write ctrf.json (stages will be null)")

    MASK_TAILS = ("|| true", "|| exit 0", "2>/dev/null")
    BUILD_KWS = ("compile", "make ", "cmake", "build", "gcc", "clang", "javac", "mvn", "gradle")
    CLEANUP_RE = re.compile(r"(^|[;&|\s(])(make\s+(dist)?clean|rm|kill)\s")
    scan_targets = [("environment/scripts/" + n, task_dir / "environment" / "scripts" / n)
                    for n in ("compile.sh", "run_poc.sh", "test.sh")]
    scan_targets.append(("tests/test.sh", task_dir / "tests" / "test.sh"))
    for label, spath in scan_targets:
        stext = strip_comments(read_text(spath)) if spath.exists() else ""
        for i, line in enumerate(stext.splitlines(), 1):
            stripped = line.strip()
            if not stripped:
                continue
            has_mask = any(stripped.endswith(t) for t in MASK_TAILS)
            if has_mask:
                low = stripped.lower()
                is_build = any(kw in low for kw in BUILD_KWS)
                is_cleanup = bool(CLEANUP_RE.search(low))
                if is_build and not is_cleanup:
                    issues_08.append(f"{label}:{i} masks build failure")

    if "set +e" in compile_sh:
        plus_e_count = len(re.findall(r"^\s*set\s+\+e", compile_sh, re.MULTILINE))
        minus_e_count = len(re.findall(r"^\s*set\s+-e", compile_sh, re.MULTILINE))
        if plus_e_count > minus_e_count:
            issues_08.append("compile.sh has unpaired 'set +e' (errors silently ignored)")

    check("QC-08", 5, "test.sh calls validate.py and emits grading artifacts",
          len(issues_08) == 0,
          "; ".join(issues_08) if issues_08 else "")

    # --- QC-09: test_output.py reads oracle.json, not report.json ---
    reads_report = ("report.json" in test_output_code
                    and "oracle.json" not in test_output_code
                    and "verify.json" not in test_output_code)
    check("QC-09", 5, "test_output.py reads oracle.json, not report.json",
          not reads_report,
          "reads report.json which is never generated in verifier flow" if reads_report else "")

    # --- QC-10: Stage function names match ---
    test_funcs = find_test_funcs(test_output_py)
    task_map = load_task_stage_map(task_dir)
    bad_map = sorted(n for names in task_map.values() for n in names if n not in test_funcs)
    matched_stages = {}
    for stage, valid_names in stage_table(task_map).items():
        for fn in test_funcs:
            if fn in valid_names:
                matched_stages[stage] = fn
                break
    # The stages a task of this mode must map.  A task that maps NONE would
    # grade to `stages = {}` in the runner and could never succeed.
    needed = {"stage3", "stage4"} if is_patch_only else set(STAGE_TEST_NAMES)
    missing_stages = sorted(st for st in needed if st not in matched_stages)
    unmapped = sorted(fn for fn in test_funcs
                      if re.match(r"test_stage\d", fn) and fn not in ALL_STAGE_NAMES)
    detail_10 = (f"missing stages: {missing_stages} (add names to scripts/stage_names.py or "
                 f"ship tests/stage_map.json); " if missing_stages else "") + \
                (f"stage-named tests unknown to scripts/stage_names.py: {unmapped}; " if unmapped else "") + \
                (f"stage_map.json names not defined in test_output.py: {bad_map}; " if bad_map else "") + \
                f"matched: {matched_stages}"
    check("QC-10", 5, "Stage tests resolve under the shared stage table (or tests/stage_map.json)",
          not missing_stages and not unmapped and not bad_map, detail_10)

    # --- QC-11: test_weights.json values ---
    weights_path = task_dir / "tests" / "test_weights.json"
    weights = {}
    bad_values = []
    if weights_path.exists():
        try:
            weights = json.loads(weights_path.read_text())
            for k, v in weights.items():
                if not weight_ok(v):
                    bad_values.append(f"{k}={v!r} (must be a nonzero integer, |w| <= {MAX_ABS_WEIGHT})")
        except json.JSONDecodeError:
            bad_values.append("invalid JSON")
    pos_sum = sum(v for v in weights.values() if isinstance(v, (int, float)) and v > 0)
    if not bad_values and weights and pos_sum <= 0:
        bad_values.append(f"positive weight sum is {pos_sum} (grader denominator would be 0)")
    check("QC-11", 3, f"test_weights.json values are nonzero integers (|w| <= {MAX_ABS_WEIGHT}), positive sum > 0",
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
    if not is_patch_only and (not gt_solution.exists() or gt_solution.stat().st_size == 0):
        issues_17.append("solution/poc.bin missing or empty")
    if gt_tests.exists() and gt_solution.exists() \
            and read_bytes(gt_tests) == read_bytes(gt_solution):
        # A byte-identical reference PoC is fine UNLESS the task penalises
        # exactly that: then its own reference solution can never score 1.0.
        gt_copy_tests = [k for k, v in weights.items()
                         if isinstance(v, (int, float)) and v < 0
                         and re.search(r"gt_copy|is_gt|ground_truth|gt_poc", k)]
        if gt_copy_tests:
            issues_17.append(
                f"solution/poc.bin is byte-identical to tests/data/poc.bin while "
                f"{gt_copy_tests} penalises that: the reference solution cannot score 1.0 "
                f"(ship a distinct reference PoC or drop the test)")
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
                        need = [k for k in ("number", "criterion", "score", "is_positive")
                                if k not in elem]
                        if need:
                            json_issues.append(f"rubric.json[{idx}]: missing keys {need} "
                                               f"(judge_lib requires them)")
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

    # --- QC-22: leakage that the byte-identity scan cannot see ---
    issues_22 = []
    src_tgz_path = task_dir / "environment" / "src.tgz"
    if src_tgz_path.exists():
        try:
            with tarfile.open(src_tgz_path, "r:*") as tf:
                members = tf.getmembers()
            names = [m.name for m in members]
            bad_re = re.compile(r"(^|/)(\.pytest_cache|\.git|__pycache__)(/|$)|"
                                r"(^|/)(poc\.bin|crash\.log|fix\.patch|patch\.diff|report\.json)$")
            bad = sorted(n for n in names if bad_re.search(n))[:10]
            if bad:
                issues_22.append(f"src.tgz ships answer/metadata members: {bad}")
            mtimes = {m.mtime for m in members if m.isfile()}
            if len(mtimes) > 1:
                issues_22.append(
                    f"src.tgz has {len(mtimes)} distinct mtimes (a defect injected after packing "
                    f"is a one-command tell; pack with scripts/pack.sh)")
            owners = {(m.uname, m.gname) for m in members} - {("", ""), ("root", "root")}
            if owners:
                issues_22.append(f"src.tgz carries packer identity {sorted(owners)[:3]}")
        except Exception as exc:  # noqa: BLE001
            issues_22.append(f"src.tgz unreadable: {exc}")
    PROSE_RE = re.compile(r"vulnerab|CVE-\d|overflow|use.after.free|uninitiali[sz]ed|heap-|"
                          r"out.of.bounds|\bfix(ed|es)?\b|patched|bug\b|crash", re.I)
    scripts_dir = task_dir / "environment" / "scripts"
    if scripts_dir.is_dir():
        for f in sorted(os.listdir(scripts_dir)):
            if not f.endswith(".sh"):
                continue
            for i, line in enumerate(read_text(scripts_dir / f).splitlines(), 1):
                st = line.strip()
                if st.startswith("#") and PROSE_RE.search(st) and not st.startswith("#!"):
                    issues_22.append(f"environment/scripts/{f}:{i} comment names the bug: '{st[:70]}'")
                elif re.search(r"\bgrep\s+(-\w+\s+)*['\"].+['\"]\s+\S+\.(c|cc|cpp|h|py|java|rs|go)\b", st):
                    issues_22.append(f"environment/scripts/{f}:{i} greps source for a string "
                                     f"(reveals the fix): '{st[:70]}'")
    check("QC-22", 3, "No answer leakage via src.tgz metadata or agent-visible script prose",
          len(issues_22) == 0,
          "; ".join(issues_22[:8]) + (f"; +{len(issues_22) - 8} more" if len(issues_22) > 8 else "")
          if issues_22 else "")

    # --- QC-21: fix.patch applies to src.tgz ---
    patch_issues = []
    src_tgz = task_dir / "environment" / "src.tgz"
    if not fix_patch.exists() or fix_patch.stat().st_size == 0:
        # An absent reference patch used to pass this check vacuously.
        patch_issues.append("solution/fix.patch missing or empty: nothing to validate")
    elif not src_tgz.exists():
        patch_issues.append("environment/src.tgz missing: cannot validate the patch")
    else:
        patch_bytes = read_bytes(fix_patch)
        repo_match = re.search(r'repo_to_patch\s*=\s*"([^"]+)"', config_text)
        repo_name = repo_match.group(1) if repo_match else ""
        if repo_name and (os.path.isabs(repo_name) or ".." in Path(repo_name).parts):
            patch_issues.append(f"repo_to_patch={repo_name!r} must be a relative path without '..'")
            repo_name = ""
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                tar_rc = subprocess.run(
                    ["tar", "xzf", str(src_tgz), "-C", tmpdir],
                    capture_output=True, timeout=120,
                )
                if tar_rc.returncode != 0:
                    patch_issues.append(
                        f"src.tgz extraction failed: {tar_rc.stderr.decode(errors='replace')[:200]}"
                    )
                else:
                    # Candidate working directories, mirroring the container
                    # layout (/src/<repo> for `git apply`, /src for -p1 with
                    # <repo>/ prefixed paths).
                    attempts = []
                    repo_dir = os.path.join(tmpdir, repo_name) if repo_name else None
                    if repo_dir and os.path.isdir(repo_dir):
                        attempts.append(("repo_subdir", repo_dir))
                    attempts.append(("tmpdir", tmpdir))
                    if repo_name and not (repo_dir and os.path.isdir(repo_dir)):
                        parent = os.path.join(tmpdir, "_parent")
                        target = os.path.join(parent, repo_name)
                        os.makedirs(parent, exist_ok=True)
                        if not os.path.lexists(target):
                            os.symlink(tmpdir, target)       # stays inside tmpdir
                        attempts.append(("parent_with_repo_link", parent))

                    # Same applier the template verifier uses first (git apply,
                    # no fuzz), then GNU patch as the verifier's own fallback.
                    appliers = [
                        ("git apply --check", ["git", "apply", "--check", "-"]),
                        ("patch -p1 --dry-run", ["patch", "--dry-run", "--batch", "--forward", "-p1"]),
                    ]
                    applied_with = None
                    last_err = ""
                    for label, cwd in attempts:
                        for aname, argv in appliers:
                            try:
                                dry = subprocess.run(argv, input=patch_bytes, capture_output=True,
                                                     cwd=cwd, timeout=60)
                            except FileNotFoundError:
                                continue
                            out = (dry.stdout + dry.stderr).decode(errors="replace")
                            bad = [l for l in out.splitlines()
                                   if "failed" in l.lower() or "ignored" in l
                                   or "No file to patch" in l or "error:" in l]
                            if dry.returncode == 0 and not bad:
                                applied_with = f"{aname} in {label}"
                                break
                            if bad:
                                last_err = bad[0]
                            elif out.strip():
                                last_err = out.strip().splitlines()[-1]
                            else:
                                last_err = f"exit {dry.returncode}"
                        if applied_with:
                            break
                    if not applied_with:
                        patch_issues.append(f"fix.patch does not apply to src.tgz ({str(last_err)[:200]})")
            except Exception as exc:  # noqa: BLE001 -- one bad task must not abort the batch
                patch_issues.append(f"patch validation error: {type(exc).__name__}: {exc}")
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
    bodies_all = test_func_bodies(test_output_py)
    for t in neg_tests:
        low = t.lower()
        body = strip_comments(bodies_all.get(t, "")).lower()
        if re.search(r"network|internet|download|fetch", low):
            cheat_categories["network"] = True
        if re.search(r"empty|zero_lines|no_change|nochange|blank", low) or "changed == 0" in body:
            cheat_categories["empty_patch"] = True
        # Hand-written tasks name this detector many ways; what matters is
        # that a negative test reads the declared immutable_files list.
        if re.search(r"immutable|protected|harness|evidence|infrastructure", low) \
                or "immutable_files" in body:
            cheat_categories["immutable"] = True

    missing_cheats = [k for k, v in cheat_categories.items() if not v]
    bodies = test_func_bodies(test_output_py)
    trivial = [t for t in neg_tests if t in bodies and body_is_trivial(bodies[t])]
    check("TC-01", 5, "Anti-cheat: negative-weight tests cover key cheat vectors",
          len(missing_cheats) == 0 and not trivial,
          (f"missing cheat detectors: {missing_cheats}; " if missing_cheats else "")
          + (f"trivial bodies (no assert/raise): {trivial}; " if trivial else "")
          + f"have: {neg_tests}")

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
    if env_dir.is_dir() and sol_patch_bytes:
        for root, _dirs, files in os.walk(env_dir):
            for f in sorted(files):
                fp = Path(root) / f
                if f.endswith((".patch", ".diff")) or fp.stat().st_size == len(sol_patch_bytes):
                    if read_bytes(fp) == sol_patch_bytes:
                        rel = fp.relative_to(task_dir)
                        issues_tc03.append(f"{rel} is identical to solution/fix.patch")
    # Check instruction.md doesn't contain the actual patch content
    if sol_patch.exists():
        patch_text = read_text(sol_patch)
        # Extract unique diff lines (actual code changes)
        changed_lines = [l for l in patch_text.splitlines()
                         if (l.startswith("+") and not l.startswith("+++"))
                         or (l.startswith("-") and not l.startswith("---"))]
        # Only lines with real content count; `+}` matches almost any prose.
        signal = [l[1:].strip() for l in changed_lines if len(l[1:].strip()) >= 12]
        if signal:
            found = sum(1 for x in signal if x in instr)
            if len(signal) > 3 and found > len(signal) * 0.5:
                issues_tc03.append(
                    f"instruction.md contains {found}/{len(signal)} "
                    "changed lines from the solution patch")
    check("TC-03", 5, "Solution patch not leaked to agent",
          len(issues_tc03) == 0,
          "; ".join(issues_tc03) if issues_tc03 else "")

    # --- TC-04: Network lockdown enforced ---
    issues_tc04 = []
    # The runner (run_harbor.py) enforces the lockdown; the task's job is to
    # tell the agent and to not need the network at runtime itself.
    if "network" not in instr.lower() and "internet" not in instr.lower():
        issues_tc04.append("instruction.md does not mention network/internet restriction")
    runtime_scripts = [("environment/scripts/" + n, task_dir / "environment" / "scripts" / n)
                       for n in ("compile.sh", "run_poc.sh", "test.sh", "prepare.sh")]
    runtime_scripts.append(("environment/validate.py", task_dir / "environment" / "validate.py"))
    for label, spath in runtime_scripts:
        stext = strip_comments(read_text(spath)) if spath.exists() else ""
        for i, line in enumerate(stext.splitlines(), 1):
            stripped = line.strip()
            for dl_cmd in ("curl ", "wget ", "git clone", "pip install", "pip3 install",
                           "apt-get install", "apt install", "npm install"):
                if dl_cmd in stripped:
                    issues_tc04.append(f"{label}:{i} downloads at runtime: '{stripped[:80]}'")
    check("TC-04", 5, "No runtime downloads in agent scripts (network locked)",
          len(issues_tc04) == 0,
          "; ".join(issues_tc04) if issues_tc04 else "")

    # --- TC-05: Immutable files declared and enforced ---
    # Only blocking when task declares immutable files but has no test for it.
    # Tasks with no immutable files (empty or absent) don't need this test.
    issues_tc05 = []
    bodies_tc05 = test_func_bodies(test_output_py)
    immutable_tests = [t for t in test_funcs
                       if re.search(r"immutable|protected|infrastructure", t.lower())
                       or "immutable_files" in strip_comments(bodies_tc05.get(t, ""))]
    has_immutable_test = bool(immutable_tests)
    if has_immutable_files:
        if not has_immutable_test:
            issues_tc05.append(
                f"immutable_files declared but no test enforces it")
        else:
            bodies = test_func_bodies(test_output_py)
            for t in immutable_tests:
                if t in bodies and body_is_trivial(bodies[t]):
                    issues_tc05.append(f"{t} has no assertion")
                # A detector that hard-codes the protected paths instead of
                # reading immutable_files still enforces them; note it only.
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
    check("TC-06", 0, "Instruction quality and completeness (informational)",
          True, "; ".join(issues_tc06) if issues_tc06 else "")

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
    check("TC-07", 0, "Verifier isolation (informational)",
          True, "; ".join(issues_tc07) if issues_tc07 else "")

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
    stage_ratio = stage_weight_sum / pos_sum if pos_sum > 0 else 0
    # "no negative weights" and a low stage ratio are design warnings, reported
    # by TC-11 below; TC-08 blocks only on structural scoring defects.
    # The verifier must clamp to [-1, 1]; clamping at 0 makes negative
    # weights unable to do anything but neutralise the score.
    if re.search(r"max\(\s*0(\.0)?\s*,\s*min\(\s*1(\.0)?", test_sh_verifier):
        issues_tc08.append("tests/test.sh clamps the reward to [0, 1]; must be [-1, 1]")
    # cheat_gates.json (optional) must reference known tests with the right polarity
    gates = {}
    gates_path = task_dir / "tests" / "cheat_gates.json"
    if gates_path.exists():
        try:
            gates = json.loads(gates_path.read_text())
            for neg, gated in gates.items():
                if neg not in weights or not (isinstance(weights[neg], (int, float)) and weights[neg] < 0):
                    issues_tc08.append(f"cheat_gates.json: {neg} is not a negative-weight test")
                for g in gated:
                    if g not in weights or not (isinstance(weights[g], (int, float)) and weights[g] > 0):
                        issues_tc08.append(f"cheat_gates.json: {neg} gates non-positive test {g}")
        except Exception as exc:  # noqa: BLE001
            issues_tc08.append(f"cheat_gates.json unreadable: {exc}")
    check("TC-08", 3, "Scoring sanity (stages weighted, cheats penalized, [-1,1] clamp, gates valid)",
          len(issues_tc08) == 0,
          "; ".join(issues_tc08) if issues_tc08 else
          f"positive={pos_sum}, negative={neg_sum}, stage_ratio={stage_ratio:.0%}, gates={len(gates)}")

    # --- TC-11: cheat profitability ---
    # Without gating, a penalty only offsets: it must be at least as large as
    # the stage credit the cheat can unlock, or cheating out-scores honest
    # failure.  With a gate entry the credit is zeroed, so any penalty works.
    issues_tc11 = []
    if neg_sum == 0:
        issues_tc11.append("no negative weights (no cheat penalty)")
    if pos_sum > 0 and stage_ratio < 0.5:
        issues_tc11.append(
            f"4-stage tests carry only {stage_ratio:.0%} of positive weight "
            f"({stage_weight_sum}/{pos_sum})")
    stage_credit = {k: v for k, v in weights.items()
                    if k in ALL_STAGE_NAMES and isinstance(v, (int, float)) and v > 0}
    patch_stage_credit = sum(v for k, v in stage_credit.items()
                             if not re.match(r"test_stage1_|test_agent_poc_crashes", k))
    for neg in [k for k, v in weights.items() if isinstance(v, (int, float)) and v < 0]:
        if gates.get(neg):
            continue
        unlock = patch_stage_credit if re.search(r"empty|immutable|sanitiz|patch", neg) else 0
        if unlock and abs(weights[neg]) < unlock:
            issues_tc11.append(
                f"{neg}={weights[neg]} is smaller than the {unlock} stage credit it can unlock "
                f"and has no gate entry in tests/cheat_gates.json")
    check("TC-11", 3, "Scoring design (cheat penalties present, stages weighted, penalty covers or gates the credit it unlocks)",
          len(issues_tc11) == 0,
          "; ".join(issues_tc11) if issues_tc11 else "")

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

def run_reference_solution(task_dir, results, platform=None, min_reward=0.95):
    """RR-01 (blocking): build the environment, grade solution/ end to end.

    This is the only check that executes anything: it catches an image whose
    ENTRYPOINT swallows `sleep infinity`, a reference patch that does not
    apply with the verifier's own applier, a broken oracle, and a reference
    solution that trips its own anti-cheat -- none of which a static scan can
    see.  It is opt-in (--run-reference) because a build takes minutes.
    """
    def check(rr_id, weight, desc, passed, detail=""):
        results.append({"id": rr_id, "weight": weight, "desc": desc,
                        "passed": passed, "detail": detail, "blocking": rr_id in BLOCKING})

    platform = platform or os.environ.get("PLATFORM", "linux/amd64")   # same default as run_harbor
    tag = f"qc-ref-{task_dir.name.lower().replace('_', '-')}:qc"
    cid = None
    try:
        b = subprocess.run(["docker", "build", "--platform", platform, "-t", tag,
                            str(task_dir / "environment")], capture_output=True, text=True)
        if b.returncode != 0:
            check("RR-01", 10, "Reference solution grades end to end", False,
                  f"docker build failed: {b.stderr[-400:]}")
            return
        ep = subprocess.run(["docker", "inspect", "-f", "{{json .Config.Entrypoint}}", tag],
                            capture_output=True, text=True).stdout.strip()
        if ep not in ("", "null", "[]"):
            check("RR-01", 10, "Reference solution grades end to end", False,
                  f"image declares ENTRYPOINT {ep}: it swallows the runner's `sleep infinity`")
            return
        r = subprocess.run(["docker", "run", "-d", "--rm", "--platform", platform, "-w", "/src",
                            tag, "sleep", "infinity"], capture_output=True, text=True)
        if r.returncode != 0:
            check("RR-01", 10, "Reference solution grades end to end", False,
                  f"docker run failed: {r.stderr[-300:]}")
            return
        cid = r.stdout.strip()
        time.sleep(0.5)   # let an ENTRYPOINT-swallowed `sleep infinity` die first
        running = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", cid],
                                 capture_output=True, text=True).stdout.strip()
        if running != "true":
            logs = subprocess.run(["docker", "logs", cid], capture_output=True, text=True)
            check("RR-01", 10, "Reference solution grades end to end", False,
                  f"container exited immediately: {(logs.stdout + logs.stderr)[-300:]}")
            return
        subprocess.run(["docker", "exec", cid, "mkdir", "-p", "/output", "/verifier", "/logs/verifier"],
                       capture_output=True)
        for name in ("poc.bin", "fix.patch", "crash.log"):
            src = task_dir / "solution" / name
            if src.exists():
                subprocess.run(["docker", "cp", str(src), f"{cid}:/output/{name}"], capture_output=True)
        subprocess.run(["docker", "cp", str(task_dir / "tests") + "/.", f"{cid}:/verifier/"],
                       capture_output=True)
        t = subprocess.run(["docker", "exec", cid, "bash", "/verifier/test.sh"],
                           capture_output=True, text=True, timeout=7200)
        with tempfile.TemporaryDirectory() as td:
            cp = subprocess.run(["docker", "cp", f"{cid}:/logs/verifier/reward.json", td],
                                capture_output=True)
            if cp.returncode != 0:
                check("RR-01", 10, "Reference solution grades end to end", False,
                      f"test.sh exited {t.returncode} without reward.json: "
                      f"{(t.stderr or t.stdout)[-300:]}")
                return
            reward = json.load(open(os.path.join(td, "reward.json"))).get("reward")
        fails = [l.split()[1] for l in t.stdout.splitlines()
                 if l.startswith("[FAIL]") and len(l.split()) > 1]
        check("RR-01", 10, "Reference solution grades end to end",
              reward is not None and reward >= min_reward,
              f"reward={reward} (need >= {min_reward})"
              + (f"; failing tests: {fails[:6]}" if fails else ""))
    except subprocess.TimeoutExpired:
        check("RR-01", 10, "Reference solution grades end to end", False, "verifier timed out")
    except Exception as exc:  # noqa: BLE001
        check("RR-01", 10, "Reference solution grades end to end", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        if cid:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def run_full_qc(task_dir, verbose=False, run_reference=False):
    task_dir = Path(task_dir).resolve()
    results = []
    test_funcs, weights, is_patch_only = run_structural_qc(task_dir, results)
    run_task_correctness(task_dir, results, test_funcs, weights, is_patch_only)
    if run_reference:
        run_reference_solution(task_dir, results)
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
    ap.add_argument("--run-reference", action="store_true",
                    help="Build the environment and grade solution/ end to end (RR-01, blocking)")
    ap.add_argument("--tasks-root", default=str(Path(__file__).parent.parent / "tasks"),
                    help="Directory scanned by --all")
    args = ap.parse_args()

    if args.all:
        tasks_root = Path(args.tasks_root)
        if not tasks_root.is_dir():
            ap.error(f"--all: {tasks_root} does not exist (tasks/ is gitignored; "
                     f"generate tasks first or pass --tasks-root)")
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

        results = run_full_qc(task_dir, verbose=args.verbose, run_reference=args.run_reference)
        ok = print_report(task_dir, results, verbose=args.verbose)
        if not args.run_reference:
            print("  REFERENCE RUN SKIPPED: static checks only. Pass --run-reference to build the\n"
                  "  environment and grade solution/ end to end before trusting this task.\n")

        if args.json:
            # Name by the resolved path so same-named tasks in different
            # directories do not overwrite each other.
            rel = "__".join(p for p in task_dir.resolve().parts[-2:] if p)
            json_path = Path(args.json) / f"{rel}.json"
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
