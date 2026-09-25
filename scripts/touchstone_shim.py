#!/usr/bin/env python3
"""touchstone_shim.py -- convert a touchstones/<case>/ bundle into a Harbor-shaped
calibration probe bundle that run_harbor.py can execute.

Derived output is written under harness/derived/touchstones/<case>/ (gitignored).
Never writes to touchstones/ (human-write-only per trinity/FORGE.md rule 4).
Never writes to samples/ or delivery/ (FORGE lane roots; owned by FORGE alone).

Every emitted bundle is a Bucket-N calibration probe per trinity/FORGE.md
line 32 (.seed/probe.yaml). It is NEVER difficulty evidence and NEVER
contributes to a SHIP disposition. This is exactly the "author-side probe"
carve-out the contract already recognises.

The shim reads no network, no clock, and no random source; two runs over the
same touchstone bytes produce byte-identical output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

# Read-only per trinity/FORGE.md rule 8: do not invent or rotate.
FORGE_TASK_NAMESPACE = uuid.UUID("c53e8f3b-526f-52c0-a04e-89e2269b237d")

TOUCHSTONE_REQUIRED = (
    "config.toml",
    "prepare.sh",
    "compile.sh",
    "run_poc.sh",
    "test.sh",
    "poc.bin",
    "patch.diff",
    "crash.log",
)


def _parse_config_toml(text: str) -> dict:
    """Tiny TOML subset sufficient for touchstone config.toml: bare
    ``key = "value"`` or ``key = 42`` lines, ``#`` comments stripped."""
    out: dict[str, object] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, val = (x.strip() for x in line.split("=", 1))
        if val.startswith('"') and val.endswith('"'):
            val = val[1:-1]
        elif val.startswith("'") and val.endswith("'"):
            val = val[1:-1]
        elif val.lstrip("-").isdigit():
            val = int(val)
        out[key] = val
    return out


def _canonical_hash(touchstone_dir: Path) -> str:
    """SHA-256 over the required touchstone files in a fixed order, framed with
    ``name\\x00bytes\\x00`` so a rename cannot collide with a byte change."""
    h = hashlib.sha256()
    for name in TOUCHSTONE_REQUIRED:
        p = touchstone_dir / name
        h.update(name.encode())
        h.update(b"\x00")
        h.update(p.read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


def _derive_vul_image(build_image: str) -> str:
    """Touchstone ``build_image`` carries the ARVO ``-fix`` (patched) tag. The
    vulnerable build lives at the same tag without ``-fix``. Heuristic; the
    caller can override with ``--vul-image``."""
    if build_image.endswith("-fix"):
        return build_image[:-4]
    return build_image


TASK_TOML_TEMPLATE = '''schema_version = "1.4"

[task]
version = "1.0.0"
uuid_v5 = "{uuid_v5}"
authors = [{{ name = "trinity-touchstone-shim", email = "shim@ethara.ai" }}]
name = "ethara/kakashi-touchstone-{slug}"
description = "Calibration probe derived from touchstone {slug}. NOT a graded Trinity task."

[metadata]
difficulty = "Unrated"
category = "Security"
repo = "{target_prog}"
language = "unknown"
kind = "calibration_probe"
calibration_only = true
touchstone_source = "{slug}"
touchstone_task_id = "{task_id}"
vulnerable_commit = "{vul_commit}"
patch_commit = "{patch_commit}"
provenance = "derived from touchstones/{slug} by scripts/touchstone_shim.py"

[verifier]
network_mode = "no-network"

[agent]
network_mode = "no-network"
timeout_sec = 5400

[environment]
allow_internet = false
dockerfile = "environment/Dockerfile"
environment_mode = "separate"

[trinity_calibration]
derived_from = "touchstones/{slug}"
canonical_hash = "{canonical_hash}"
never_ship = true
bucket = "N"
note = "Author-side calibration probe per trinity/FORGE.md line 32. NEVER difficulty evidence."
'''

INSTRUCTION_TEMPLATE = '''There is a security vulnerability in the source tree of `{target_prog}`.

This environment has no network or internet access.

## Output contract (what the grader reads)
- `/output/poc.bin` -- raw bytes that trigger a runtime abort on the unpatched build
- `/output/fix.patch` -- unified diff that closes the defect; paths relative to the repo directory

## Where things live
- The base image is the pre-built ARVO harness image for `{target_prog}`.
- `bash /scripts/prepare.sh` restores any preparation the target needs.
- `bash /scripts/compile.sh` (re)builds the fuzz target from source.
- `bash /scripts/run_poc.sh <input>` runs the target against one input; exit `1`
  on runtime abort, `0` otherwise, `2` if the harness or input is missing.
- `bash /scripts/test.sh` runs the project's own test suite; the grader also
  runs it, so it must still pass with your patch applied.

## Calibration notice
This bundle is a derived calibration probe. Its difficulty is not measured
here and no SHIP disposition rests on its outcome. The bytes you produce are
still graded on the same four stages as a real Trinity task, but the record
is Bucket-N advisory only, per trinity/FORGE.md rule 6.
'''

DOCKERFILE_TEMPLATE = '''# GENERATED by harness/scripts/touchstone_shim.py from touchstones/{slug}.
# Hand edits will be overwritten on the next shim run.

ARG VUL_IMAGE={vul_image}
FROM ${{VUL_IMAGE}}

# Standard Harbor workspace layout consumed by tests/test.sh.
# /output: agent artifacts. /logs/verifier: grading side. /scripts: env hooks.
RUN mkdir -p /output /logs/verifier /scripts

# Grading needs patch(1), python3 and pytest. ARVO base images vary; install
# defensively and ignore failures on images without apt.
RUN (command -v patch >/dev/null && command -v python3 >/dev/null && command -v pytest >/dev/null) \\
  || (apt-get update && apt-get install -y --no-install-recommends \\
        patch python3 python3-pip \\
      && pip3 install --no-cache-dir --break-system-packages pytest \\
      && rm -rf /var/lib/apt/lists/*) \\
  || true

COPY scripts/ /scripts/
RUN chmod +x /scripts/*.sh
'''

TRUTH_MD_TEMPLATE = '''<!-- GENERATED by harness/scripts/touchstone_shim.py. DO NOT HAND-EDIT. -->
<!-- Regenerated from touchstones/{slug}. -->

# TRUTH -- calibration probe `{slug}`

**This is not a Trinity-authored task. This is a calibration probe derived
from a touchstone case, per trinity/FORGE.md rule 6 and line 32.**

## Origin

- Touchstone: `touchstones/{slug}`
- Upstream task id: `{task_id}`
- Target program: `{target_prog}`
- Vulnerable commit: `{vul_commit}`
- Patch commit: `{patch_commit}`
- Base image (patched): `{build_image}`
- Derived vulnerable image: `{vul_image}`
- Canonical touchstone hash (sha256): `{canonical_hash}`
- Deterministic UUIDv5 in FORGE_TASK_NAMESPACE: `{uuid_v5}`

## Ground-truth artifacts

- `solution/poc.bin`: the reproducer harvested with the touchstone.
- `solution/fix.patch`: the upstream fix as retained with the touchstone.
- `solution/crash.log`: the crash trace captured at harvest time.

## Grading

The generated `tests/validate.py` runs four stages, mirroring the shape a
CyberGym-E2E sample would grade against:

1. Does the agent's `poc.bin` crash the unpatched build?
2. Does the agent's patch hold against the agent's own reproducer?
3. Does `/scripts/test.sh` still pass with the agent's patch applied?
4. Does the patch also stop the touchstone's own reproducer?

Stage 4 is only "held out" from the agent -- a human calibrator writing
this touchstone saw the bytes. `TRUTH.md` therefore stays inside the
private `solution/` tree and is never mounted into the agent-visible
surface, per FORGE.md item 11a.

## Never ship

This bundle's `task.toml` carries `[trinity_calibration] never_ship = true`.
No FORGE Phase 4.5 release digest may bind these bytes. Difficulty is
measured only through the signed external pilot per FORGE.md rule 6, and a
calibration probe is not a pilot.
'''

VALIDATE_PY = '''#!/usr/bin/env python3
"""Oracle for a touchstone calibration probe.

GENERATED by harness/scripts/touchstone_shim.py. Runs four graded stages
against the agent's ``poc.bin`` and ``fix.patch`` and writes a JSON summary
to stdout for the compiled tests in ``test_output.py`` to read.

Stage layout mirrors CyberGym-E2E:
  stage 1  agent's PoC crashes the unpatched build
  stage 2  agent's patch holds against the agent's own PoC
  stage 3  project test suite still passes with the patch applied
  stage 4  agent's patch also stops the held-out (touchstone) PoC
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys

PREPARE_TIMEOUT = 600
COMPILE_TIMEOUT = 1800
RUN_TIMEOUT = 300
SUITE_TIMEOUT = 1800
PATCH_TIMEOUT = 60


def _run(cmd, *, timeout, cwd=None, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout,
                           capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


def _prepare_and_compile(scripts_dir):
    rc, _, err = _run(["bash", str(scripts_dir / "prepare.sh")], timeout=PREPARE_TIMEOUT)
    if rc != 0:
        return False, "prepare rc={} err={}".format(rc, err[-200:])
    rc, _, err = _run(["bash", str(scripts_dir / "compile.sh")], timeout=COMPILE_TIMEOUT)
    if rc != 0:
        return False, "compile rc={} err={}".format(rc, err[-200:])
    return True, "ok"


def _first_patched_path(patch_file):
    """Repo-relative path of the first non-/dev/null file the patch touches."""
    try:
        text = patch_file.read_text(errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        rest = line[4:].split("\\t", 1)[0].strip()
        if rest == "/dev/null" or not rest:
            continue
        if rest.startswith(("a/", "b/")):
            rest = rest[2:]
        return rest
    return None


def _discover_repo_dir(patch_file, src_root):
    """Walk src_root for a directory that contains the first file the patch
    touches. Falls back to src_root itself when nothing matches."""
    target = _first_patched_path(patch_file)
    if not target:
        return src_root
    if (src_root / target).exists():
        return src_root
    if src_root.exists():
        for child in sorted(src_root.iterdir()):
            if child.is_dir() and (child / target).exists():
                return child
    return src_root


def _apply_patch(patch_file, repo_dir):
    if not patch_file.exists() or patch_file.stat().st_size == 0:
        return False, "patch missing or empty"
    for strip in ("1", "0", "2"):
        rc, _, err = _run(["patch", "-p" + strip, "--forward", "-i", str(patch_file)],
                          cwd=str(repo_dir), timeout=PATCH_TIMEOUT)
        if rc == 0:
            return True, "applied -p" + strip
    return False, "patch failed at every -p level: " + err[-200:]


def _run_poc(scripts_dir, poc):
    if poc is None or not poc.exists():
        return 2
    rc, _, _ = _run(["bash", str(scripts_dir / "run_poc.sh"), str(poc)], timeout=RUN_TIMEOUT)
    return rc


def _run_suite(scripts_dir):
    rc, _, _ = _run(["bash", str(scripts_dir / "test.sh")], timeout=SUITE_TIMEOUT)
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--poc-file", required=True, type=pathlib.Path)
    ap.add_argument("--patch-file", required=True, type=pathlib.Path)
    ap.add_argument("--scripts-dir", default=pathlib.Path("/scripts"), type=pathlib.Path)
    ap.add_argument("--src-root", default=pathlib.Path("/src"), type=pathlib.Path)
    ap.add_argument("--repo-dir", type=pathlib.Path, default=None,
                    help="Override the patch-apply cwd. Discovered from the patch when omitted.")
    ap.add_argument("--ground-truth-poc", required=True, type=pathlib.Path)
    args = ap.parse_args()

    result = {"stages": {}, "notes": {}}

    # Stage 1: unpatched build + agent PoC.
    ok, note = _prepare_and_compile(args.scripts_dir)
    if not ok:
        result["stages"]["stage1"] = False
        result["notes"]["stage1"] = "baseline build: " + note
    else:
        rc = _run_poc(args.scripts_dir, args.poc_file)
        result["stages"]["stage1"] = rc == 1
        result["notes"]["stage1"] = "run_poc rc={}".format(rc)

    # Stages 2 + 3 + 4: patch once, then measure. Restore first so state from
    # stage 1 cannot leak forward.
    ok, note = _prepare_and_compile(args.scripts_dir)
    if not ok:
        for stage in ("stage2", "stage3", "stage4"):
            result["stages"][stage] = False
            result["notes"][stage] = "restore: " + note
    else:
        repo_dir = args.repo_dir or _discover_repo_dir(args.patch_file, args.src_root)
        result["notes"]["repo_dir"] = str(repo_dir)
        ok, note = _apply_patch(args.patch_file, repo_dir)
        if not ok:
            for stage in ("stage2", "stage3", "stage4"):
                result["stages"][stage] = False
                result["notes"][stage] = "patch: " + note
        else:
            rc, _, err = _run(["bash", str(args.scripts_dir / "compile.sh")], timeout=COMPILE_TIMEOUT)
            if rc != 0:
                for stage in ("stage2", "stage3", "stage4"):
                    result["stages"][stage] = False
                    result["notes"][stage] = "patched compile rc={} err={}".format(rc, err[-200:])
            else:
                rc2 = _run_poc(args.scripts_dir, args.poc_file)
                result["stages"]["stage2"] = rc2 == 0
                result["notes"]["stage2"] = "agent poc rc={}".format(rc2)

                rc3 = _run_suite(args.scripts_dir)
                result["stages"]["stage3"] = rc3 == 0
                result["notes"]["stage3"] = "suite rc={}".format(rc3)

                rc4 = _run_poc(args.scripts_dir, args.ground_truth_poc)
                result["stages"]["stage4"] = rc4 == 0
                result["notes"]["stage4"] = "ground-truth poc rc={}".format(rc4)

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TEST_OUTPUT_PY = '''"""Compiled deterministic tests for a touchstone calibration probe.

GENERATED by harness/scripts/touchstone_shim.py. Reads oracle.json produced by
tests/validate.py and asserts each stage outcome. A missing oracle.json fails
every test with a clear message rather than raising a collection error, so
the reward arithmetic in tests/test.sh still writes a full ctrf.json.
"""
import json
import os
import pathlib

_LOG_DIR = pathlib.Path(os.environ.get("LOG_DIR", "/logs/verifier"))
try:
    _ORACLE = json.loads((_LOG_DIR / "oracle.json").read_text())
except Exception as exc:
    _ORACLE = {"stages": {}, "notes": {"_load_error": str(exc)}}


def _stage(name):
    return bool(_ORACLE.get("stages", {}).get(name, False))


def _note(name):
    return _ORACLE.get("notes", {}).get(name, "no oracle output")


def test_stage1_agent_poc_crashes_vuln_build():
    assert _stage("stage1"), _note("stage1")


def test_stage2_agent_poc_neutralized_by_patch():
    assert _stage("stage2"), _note("stage2")


def test_stage3_project_suite_passes_with_patch():
    assert _stage("stage3"), _note("stage3")


def test_stage4_ground_truth_poc_neutralized_by_patch():
    assert _stage("stage4"), _note("stage4")
'''

TEST_WEIGHTS = {
    "test_stage1_agent_poc_crashes_vuln_build": 15,
    "test_stage2_agent_poc_neutralized_by_patch": 15,
    "test_stage3_project_suite_passes_with_patch": 10,
    "test_stage4_ground_truth_poc_neutralized_by_patch": 8,
}

TEST_SH = '''#!/usr/bin/env bash
# GENERATED by harness/scripts/touchstone_shim.py. DO NOT HAND-EDIT.
# Grading entrypoint for a touchstone calibration probe. Runs the oracle,
# the compiled tests, and writes reward.json / reward.txt / ctrf.json where
# the run_harbor.py verifier reads them.
set -uo pipefail

TESTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-/logs/verifier}"
mkdir -p "$LOG_DIR"

PY="$(command -v python3)"
PYTEST_BIN="$(command -v pytest || true)"
if [ -z "$PYTEST_BIN" ]; then
  echo "test: pytest is absent from this image" >&2
  exit 2
fi

export AGENT_POC="${AGENT_POC:-/output/poc.bin}"
export AGENT_PATCH="${AGENT_PATCH:-/output/fix.patch}"

# 1. oracle
"$PY" "$TESTS_DIR/validate.py" \\
  --poc-file "$AGENT_POC" \\
  --patch-file "$AGENT_PATCH" \\
  --scripts-dir /scripts \\
  --src-root "${SRC:-/src}" \\
  --ground-truth-poc "$TESTS_DIR/data/ground_truth_poc.bin" \\
  > "$LOG_DIR/oracle.json" 2> "$LOG_DIR/validate.err"

# 2. weighted tests
"$PYTEST_BIN" "$TESTS_DIR/test_output.py" -v --tb=short -p no:cacheprovider \\
  --junitxml="$LOG_DIR/junit.xml" 2>&1 | tee "$LOG_DIR/pytest.log"
rc="${PIPESTATUS[0]}"

# 3. reward.json / reward.txt / ctrf.json
"$PY" - "$TESTS_DIR/test_weights.json" "$LOG_DIR" <<'PYEOF'
import json, pathlib, sys, xml.etree.ElementTree as ET
weights = json.loads(pathlib.Path(sys.argv[1]).read_text())
log = pathlib.Path(sys.argv[2])
tests, earned = [], 0.0
possible = sum(w for w in weights.values() if w > 0)
junit = log / "junit.xml"
if junit.exists():
    for case in ET.parse(junit).getroot().iter("testcase"):
        name = case.get("name", "")
        skipped = case.find("skipped") is not None
        failed = case.find("failure") is not None or case.find("error") is not None
        status = "skipped" if skipped else ("failed" if failed else "passed")
        weight = weights.get(name, 0)
        if status == "passed" and weight > 0:
            earned += weight
        elif status == "failed" and weight < 0:
            earned += weight
        tests.append({"name": name, "status": status,
                      "duration": int(float(case.get("time", 0)) * 1000)})
reward = max(0.0, earned / possible) if possible else 0.0
(log / "reward.json").write_text(json.dumps({
    "reward": round(reward, 6), "earned": earned, "possible": possible,
    "calibration_probe": True, "bucket": "N",
    "note": ("Author-side calibration probe per trinity/FORGE.md line 32 "
             "(.seed/probe.yaml). NEVER difficulty evidence."),
}, indent=2) + "\\n")
(log / "reward.txt").write_text(f"{reward:.6f}\\n")
(log / "ctrf.json").write_text(json.dumps({"results": {
    "tool": {"name": "pytest"},
    "summary": {
        "tests": len(tests),
        "passed": sum(1 for t in tests if t["status"] == "passed"),
        "failed": sum(1 for t in tests if t["status"] == "failed"),
        "skipped": sum(1 for t in tests if t["status"] == "skipped"),
    },
    "tests": tests,
}}, indent=2) + "\\n")
print(f"reward {reward:.6f} ({earned}/{possible}) [calibration_probe]")
PYEOF

exit "$rc"
'''


def emit_bundle(touchstone_dir: Path, out_root: Path, vul_image: str | None = None) -> Path:
    slug = touchstone_dir.name
    for req in TOUCHSTONE_REQUIRED:
        if not (touchstone_dir / req).exists():
            raise FileNotFoundError(f"touchstone {slug!r} is missing {req!r}")

    config = _parse_config_toml((touchstone_dir / "config.toml").read_text())
    canonical_hash = _canonical_hash(touchstone_dir)
    uuid_v5 = str(uuid.uuid5(FORGE_TASK_NAMESPACE, f"touchstone:{slug}:{canonical_hash}"))
    resolved_vul_image = vul_image or _derive_vul_image(str(config.get("build_image", "")))

    target = out_root / slug
    if target.exists():
        shutil.rmtree(target)
    (target / "environment" / "scripts").mkdir(parents=True)
    (target / "environment" / "config").mkdir(parents=True)
    (target / "solution").mkdir(parents=True)
    (target / "tests" / "data").mkdir(parents=True)

    ctx = dict(
        slug=slug,
        target_prog=str(config.get("target_prog", "unknown")),
        task_id=str(config.get("task_id", "unknown")),
        vul_commit=str(config.get("vul_commit", "unknown")),
        patch_commit=str(config.get("patch_commit", "unknown")),
        build_image=str(config.get("build_image", "unknown")),
        vul_image=resolved_vul_image,
        canonical_hash=canonical_hash,
        uuid_v5=uuid_v5,
    )

    (target / "task.toml").write_text(TASK_TOML_TEMPLATE.format(**ctx))
    (target / "instruction.md").write_text(INSTRUCTION_TEMPLATE.format(**ctx))
    (target / "environment" / "Dockerfile").write_text(DOCKERFILE_TEMPLATE.format(**ctx))
    (target / "solution" / "TRUTH.md").write_text(TRUTH_MD_TEMPLATE.format(**ctx))

    for script in ("prepare.sh", "compile.sh", "run_poc.sh", "test.sh"):
        dst = target / "environment" / "scripts" / script
        shutil.copyfile(touchstone_dir / script, dst)
        dst.chmod(dst.stat().st_mode | 0o111)

    for name in ("poc.bin", "crash.log"):
        shutil.copyfile(touchstone_dir / name, target / "solution" / name)
    shutil.copyfile(touchstone_dir / "patch.diff", target / "solution" / "fix.patch")
    shutil.copyfile(touchstone_dir / "poc.bin",
                    target / "tests" / "data" / "ground_truth_poc.bin")

    (target / "tests" / "validate.py").write_text(VALIDATE_PY)
    (target / "tests" / "test_output.py").write_text(TEST_OUTPUT_PY)
    (target / "tests" / "test_weights.json").write_text(
        json.dumps(TEST_WEIGHTS, indent=2) + "\n")
    test_sh = target / "tests" / "test.sh"
    test_sh.write_text(TEST_SH)
    test_sh.chmod(test_sh.stat().st_mode | 0o111)

    (target / "SHIM_MANIFEST.json").write_text(json.dumps({
        "generator": "harness/scripts/touchstone_shim.py",
        "derived_from": f"touchstones/{slug}",
        "canonical_touchstone_hash_sha256": canonical_hash,
        "uuid_v5": uuid_v5,
        "vul_image_source": ("from --vul-image" if vul_image
                             else "derived by stripping -fix suffix"),
        "vul_image": resolved_vul_image,
        "build_image": str(config.get("build_image", "")),
        "trinity_bucket": "N",
        "never_ship": True,
        "notice": ("Author-side calibration probe per trinity/FORGE.md line 32 "
                   "(.seed/probe.yaml). NEVER difficulty evidence. NEVER a SHIP artifact."),
    }, indent=2) + "\n")

    return target


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("touchstone", type=Path, nargs="?",
                    help="Path to a touchstones/<case>/ directory. Omit with --all.")
    ap.add_argument("--all", action="store_true",
                    help="Shim every touchstone under <parent>/touchstones/.")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent.parent / "derived" / "touchstones",
                    help="Output root (default: harness/derived/touchstones).")
    ap.add_argument("--vul-image", default=None,
                    help="Override the derived vulnerable image; useful when the touchstone's "
                         "build_image does not follow the ARVO -fix convention.")
    args = ap.parse_args()

    if args.all:
        touchstones_root = Path(__file__).resolve().parent.parent.parent / "touchstones"
        if not touchstones_root.exists():
            print(f"ERROR: {touchstones_root} not found", file=sys.stderr)
            return 2
        cases = sorted(p for p in touchstones_root.iterdir()
                       if p.is_dir() and (p / "config.toml").exists())
    elif args.touchstone:
        cases = [args.touchstone.resolve()]
    else:
        ap.error("pass a touchstone dir or --all")

    args.out.mkdir(parents=True, exist_ok=True)
    for c in cases:
        out = emit_bundle(c, args.out, vul_image=args.vul_image)
        print(f"{c.name}\t->\t{out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
