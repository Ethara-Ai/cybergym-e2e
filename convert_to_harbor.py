#!/usr/bin/env python3
"""
convert_to_harbor.py — transform a CyberGym-E2E task into a Harbor task directory
with weighted scoring.

A CyberGym task = a RECIPE (projects/<proj>/<task>/: config.toml + 4 scripts + patch.diff)
plus a PAYLOAD (data/projects/<proj>/<task>/: src.tgz + poc.bin + crash.log).

This emits a self-contained Harbor task:

    tasks/<proj>__<task>/
    ├── task.toml
    ├── instruction.md
    ├── environment/            # agent-visible sandbox (Dockerfile + assets)
    │   ├── Dockerfile
    │   ├── src.tgz
    │   ├── scripts/{prepare,compile,run_poc,test}.sh
    │   ├── config/config.toml  # sanitized (repo_to_patch + immutable_files only)
    │   └── given/              # (patch-only) crash.log + poc.bin handed to the agent
    ├── tests/                  # verifier (HIDDEN from agent)
    │   ├── test.sh             # weighted runner -> /logs/verifier/reward.txt
    │   ├── test_output.py      # 13 weighted test functions (4-stage oracle)
    │   ├── test_weights.json   # per-test weights (positive + negative)
    │   ├── rubric.json         # trajectory evaluation criteria (R1-R12)
    │   ├── validate.py         # the judge, copied unchanged
    │   └── data/poc.bin        # ground-truth PoC (Stage 4)
    └── solution/               # reference answer (optional)
        ├── fix.patch
        └── poc.bin

Scoring: reward = sum(passed_weights) / sum(positive_weights), clamped to [-1, 1].
Negative-weight tests penalize cheating (network access, empty patch, GT copy).

Usage:
    python convert_to_harbor.py <proj>/<task> [--mode e2e|patch-only]
    python convert_to_harbor.py --all --mode e2e            # convert every task with data
"""
import argparse
import shutil
import sys
from pathlib import Path

import tomli

ROOT = Path(__file__).parent.resolve()
TEMPLATES = ROOT / "templates"


def render(template_path, **subs):
    text = template_path.read_text()
    for k, v in subs.items():
        text = text.replace(f"%%{k}%%", str(v))
    return text


def load_config(src_repo, task):
    proj = task.split("/")[0]
    task_dir = src_repo / "projects" / task
    cfg = {}
    proj_toml = src_repo / "projects" / proj / "project.toml"
    if proj_toml.exists():
        cfg.update(tomli.loads(proj_toml.read_text()))
    task_toml = task_dir / "config.toml"
    if task_toml.exists():
        cfg.update(tomli.loads(task_toml.read_text()))
    return cfg, task_dir


def convert(task, mode, src_repo, data_root, out_root, agent_timeout, verifier_timeout):
    cfg, task_dir = load_config(src_repo, task)
    if not task_dir.exists():
        raise SystemExit(f"ERROR: recipe not found: {task_dir}")

    repo_to_patch = cfg.get("repo_to_patch", "")
    build_image = cfg.get("build_image", "gcr.io/oss-fuzz-base/base-builder")
    immutable = cfg.get("immutable_files", [])
    task_id = cfg.get("task_id", task)
    language = cfg.get("language", "C/C++")
    proj = task.split("/")[0]

    slug = task.replace("/", "__")
    out = out_root / slug
    if out.exists():
        shutil.rmtree(out)
    (out / "environment" / "scripts").mkdir(parents=True)
    (out / "environment" / "config").mkdir(parents=True)
    (out / "tests" / "data").mkdir(parents=True)
    (out / "solution").mkdir(parents=True)

    warnings = []

    # --- environment: build/run/test scripts (agent-visible) ---
    for s in ["prepare.sh", "compile.sh", "run_poc.sh", "test.sh"]:
        f = task_dir / s
        if f.exists():
            shutil.copy(f, out / "environment" / "scripts" / s)
        else:
            warnings.append(f"missing script: {s}")

    # --- environment: validate.py + install_validate_deps.sh (for in-container self-test) ---
    shutil.copy(ROOT / "lib" / "validate.py", out / "environment" / "validate.py")
    ivd = src_repo / "scripts" / "install_validate_deps.sh"
    if not ivd.exists():
        ivd = ROOT / "scripts" / "install_validate_deps.sh"
    shutil.copy(ivd, out / "environment" / "install_validate_deps.sh")

    # --- environment: sanitized config (NO vul_commit/patch_commit/target_prog) ---
    san = f'repo_to_patch = "{repo_to_patch}"\nimmutable_files = {immutable!r}\n'
    (out / "environment" / "config" / "config.toml").write_text(san)

    # --- payload: src.tgz + ground-truth poc.bin + crash.log ---
    data_dir = data_root / task
    src_tgz = data_dir / "src.tgz"
    gt_poc = data_dir / "poc.bin"
    crash_log = data_dir / "crash.log"

    if src_tgz.exists():
        shutil.copy(src_tgz, out / "environment" / "src.tgz")
    else:
        warnings.append(f"MISSING PAYLOAD src.tgz ({data_dir}) — download via `hf download` then re-run")
        (out / "environment" / "MISSING_src.tgz.txt").write_text(
            f"src.tgz not found at {src_tgz}\nDownload the dataset payload and re-run the converter.\n")

    if gt_poc.exists():
        shutil.copy(gt_poc, out / "tests" / "data" / "poc.bin")
        shutil.copy(gt_poc, out / "solution" / "poc.bin")
    else:
        warnings.append("MISSING PAYLOAD poc.bin (ground truth) — Stage 4 cannot run")

    # ARVO / cybergym base images ship their OWN /src tree. Clear it before
    # extracting the vulnerable snapshot, same as setup_workspace() does.
    src_reset = ""
    if "arvo" in build_image or "cybergym" in build_image:
        src_reset = (
            "\n# Clear the base image's own source tree and prebuilt binaries so the\n"
            "# vulnerable snapshot below is the only source present (mirrors setup_workspace).\n"
            "RUN find /src -mindepth 1 -maxdepth 1 ! -name honggfuzz ! -name libfuzzer "
            "! -name afl ! -name aflplusplus ! -name fuzztest -exec rm -rf {} + 2>/dev/null || true\n"
            "RUN rm -rf /out /work /bin/arvo /usr/bin/arvo /tmp/poc && mkdir -p /out /work\n"
        )

    patchonly_copy = ""
    if mode == "patch-only":
        given = out / "environment" / "given"
        given.mkdir(exist_ok=True)
        if crash_log.exists():
            shutil.copy(crash_log, given / "crash.log")
        if gt_poc.exists():
            shutil.copy(gt_poc, given / "poc.bin")
        patchonly_copy = (
            "# patch-only: crash log + ground-truth PoC are GIVEN to the agent\n"
            "COPY given/crash.log /src/crash.log\n"
            "COPY given/poc.bin /src/poc.bin\n"
        )

    # --- reference solution ---
    patch = task_dir / "patch.diff"
    if patch.exists():
        shutil.copy(patch, out / "solution" / "fix.patch")

    # --- verifier: validate.py (the judge, unchanged) ---
    shutil.copy(ROOT / "lib" / "validate.py", out / "tests" / "validate.py")

    # --- verifier: weighted scoring files ---
    shutil.copy(TEMPLATES / "test_output.py.tmpl", out / "tests" / "test_output.py")
    shutil.copy(TEMPLATES / "test_weights.json.tmpl", out / "tests" / "test_weights.json")
    shutil.copy(TEMPLATES / "rubric.json.tmpl", out / "tests" / "rubric.json")

    # --- rendered files ---
    desc = f"Find, exploit, and patch the vulnerability in {proj} ({task_id}) [{mode}]."
    (out / "task.toml").write_text(render(
        TEMPLATES / "task.toml.tmpl",
        TASK_NAME=slug, DESCRIPTION=desc, MODE=mode, LANGUAGE=language,
        AGENT_TIMEOUT=agent_timeout, VERIFIER_TIMEOUT=verifier_timeout))

    instr_tmpl = "instruction.e2e.md" if mode == "e2e" else "instruction.patch-only.md"
    (out / "instruction.md").write_text(render(
        TEMPLATES / instr_tmpl, REPO_TO_PATCH=repo_to_patch))

    (out / "environment" / "Dockerfile").write_text(render(
        TEMPLATES / "Dockerfile.tmpl",
        TASK_NAME=slug, MODE=mode, BUILD_IMAGE=build_image,
        SRC_RESET=src_reset, PATCHONLY_COPY=patchonly_copy))

    test_tmpl = "test.sh.e2e.tmpl" if mode == "e2e" else "test.sh.patch-only.tmpl"
    (out / "tests" / "test.sh").write_text((TEMPLATES / test_tmpl).read_text())

    return out, warnings


def main():
    ap = argparse.ArgumentParser(description="Convert CyberGym-E2E task(s) to Harbor format (weighted scoring)")
    ap.add_argument("task", nargs="?", help="proj/task (e.g. ghostscript/arvo_44406)")
    ap.add_argument("--all", action="store_true", help="convert every task that has a payload")
    ap.add_argument("--mode", choices=["e2e", "patch-only"], default="e2e")
    ap.add_argument("--src-repo", default=str(ROOT))
    ap.add_argument("--data-root", default=str(ROOT / "data" / "projects"))
    ap.add_argument("--out", default=str(ROOT / "tasks"))
    ap.add_argument("--agent-timeout", type=int, default=5400)
    ap.add_argument("--verifier-timeout", type=int, default=7200)
    ap.add_argument("--with-payload-only", action="store_true",
                    help="with --all, skip tasks whose src.tgz has not been downloaded")
    args = ap.parse_args()

    src_repo = Path(args.src_repo).resolve()
    data_root = Path(args.data_root).resolve()
    out_root = Path(args.out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.all:
        tasks = []
        for proj_dir in sorted((src_repo / "projects").iterdir()):
            if not proj_dir.is_dir():
                continue
            for task_dir in sorted(proj_dir.iterdir()):
                if task_dir.is_dir() and (task_dir / "config.toml").exists():
                    name = f"{proj_dir.name}/{task_dir.name}"
                    if args.with_payload_only and not (data_root / name / "src.tgz").exists():
                        continue
                    tasks.append(name)
    elif args.task:
        tasks = [args.task]
    else:
        ap.error("provide a task (proj/task) or --all")

    total = 0
    for t in tasks:
        try:
            out, warnings = convert(t, args.mode, src_repo, data_root, out_root,
                                    args.agent_timeout, args.verifier_timeout)
        except SystemExit as e:
            print(e); continue
        total += 1
        print(f"[ok] {t} -> {out.relative_to(out_root.parent)}")
        for w in warnings:
            print(f"     ! {w}")
    print(f"\nConverted {total} task(s) into {out_root}")


if __name__ == "__main__":
    main()
