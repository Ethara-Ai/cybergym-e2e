<!-- GENERATED SECTION. DO NOT HAND-EDIT. -->
<!-- This file is regenerated from the harness code by scripts/render_readme.py. -->
<!-- Any hand edit will be overwritten on the next run. -->
<!-- Regenerate with: python3 scripts/render_readme.py (from the harness root). -->
<!-- Emitter sha256: 156b3758371958372c8f20a697942c5dfd05116c6eee5300c03ed0111a1f009d -->

# harness

This is the Trinity `harness/` submodule, the benchmark extension an external runner uses to run inference and evaluation over resident task bundles under the parent project's `samples/` and `delivery/` lane roots. Per trinity/FORGE.md the harness is generated and reconciled by FORGE alone, is never executed by FORGE against a solver, delegates every scoring decision to each bundle's own `tests/` under a pinned Harbor release, and mounts neither `solution/` nor `trajectories/` for any path it exposes to the agent.

This README is generated. It reflects the bytes on disk at the moment `scripts/render_readme.py` was last run. If a section here disagrees with the code the code is correct and the README needs regenerating.

## Docker host requirements

`--runner harbor` (the default) enforces the agent-phase egress allowlist with Harbor's nftables sidecar, so the docker daemon's kernel must carry `CONFIG_NFT_FIB_INET`. The harness probes for it and refuses to start rather than run an unisolated agent.

| Host | Works with `--runner harbor` |
| --- | --- |
| Linux, stock docker | Yes. The canonical configuration `harness-config.json` is written against. |
| macOS + OrbStack | Yes. `docker context use orbstack`. |
| macOS + Colima | Yes. `colima start && docker context use colima`. |
| macOS + Docker Desktop | No. Its linuxkit kernel lacks the symbol; harbor refuses at startup. |

On a Mac the fix is another local daemon, not a Linux box: switch the docker context to OrbStack or Colima and the harbor runner works unchanged. `--runner legacy` is the deprecated raw-docker fallback that still isolates on Docker Desktop but only runs the `claude-code` agent (no `openhands-sdk`). `--no-lockdown` leaves the agent phase on the public network and is flagged in `summary.json`; it is for debugging and never for a run you intend to report.

Harbor's sidecar proxies every agent request through gost, whose stock 15s read timeout severs an in-flight LLM call once the prompt grows enough that time-to-first-byte exceeds it. `gost.yaml` ships inside the pinned Harbor wheel, so a fresh `uv sync` reinstates that default; the harness rewrites it at startup (`scripts/harbor_runner.py:ensure_gost_read_timeout`) and prints `Egress proxy: ...` when it does. Override the value with `KAKASHI_GOST_READ_TIMEOUT`.

## Entry points

- `generate_report.py`: Generate report.json for report-based CyberGym-E2E tasks.
- `harness.py`: Trinity harness top-level CLI dispatcher (FORGE.md:184).
- `run_harbor.py`: run_harbor.py - Run a Harbor-formatted CyberGym-E2E task end-to-end.

## Scripts

Contents of `scripts/`, sorted with files first and then subdirectories:

- `scripts/assert_no_oracle.sh`: Assert that a built agent image carries no readable copy of a task's target library outside /src. See ORACLE_LEAK_FIX.md §7. A task whose agent image ships a released copy of its own target hands the agent the injected CWE via a single `diff`, invalidating the discovery phase. Run this against every agent image before trusting a score. Usage: assert_no_oracle.sh <image> <name> [<name> ...] Python target: pass the IMPORT name e.g. ... harbor-<uuid>:<tag>-agent jwt C/Go target: pass SOURCE file names e.g. ... <image> sqlite3.c sqlite3.h Matching is by name, over both directories and files. For a non-Python target, a bare project name is the wrong probe -- an interpreter ships a stdlib package of the same name (python3.x/sqlite3) that reveals nothing about a C-library CVE -- so pass the source filenames you actually care about. Exit 0 = clean, 1 = leak found, 2 = usage error.
- `scripts/batch_run.sh`: DEPRECATED: legacy runner path (run_agent.py). It scores on a binary scale, writes no reward/ctrf/rubric files and applies no network lockdown. Use run_harbor.py for anything you intend to report. Unified batch runner for cybergym-e2e agents Usage: bash scripts/batch_run.sh [tasks_file] [max_parallel] [OPTIONS] Options (via environment variables or positional args): AGENT=claude-code|openhands Agent backend (default: claude-code) MODE=e2e|patch-only Mode (default: e2e) MAX_PARALLEL=N Parallel jobs (default: 4) MAX_ATTEMPTS=N Retry attempts (default: 1) MODEL_PROVIDER=anthropic|bedrock|litellm Model provider (default: anthropic) LITELLM_MODEL_ID=... LiteLLM Model ID BEDROCK_MODEL_ID=... Bedrock Model ID ANTHROPIC_MODEL_ID=... Anthropic model ID (used with MODEL_PROVIDER=anthropic) AWS_PROFILE=... AWS profile AWS_REGION=... AWS region (default: us-west-2) AGENT_OUTPUT_DIR=... Output directory TIMEOUT=N Agent timeout in seconds (default: 5400) Examples: # Claude Code (default) bash scripts/batch_run.sh scripts/tasks_30.txt 50 # OpenHands AGENT=openhands bash scripts/batch_run.sh scripts/tasks_30.txt 4 # With custom settings AWS_PROFILE=my-profile MAX_ATTEMPTS=3 bash scripts/batch_run.sh tasks.txt # Stop all running jobs bash scripts/batch_run.sh --stop
- `scripts/batch_validate.sh`: Batch validator for cybergym-e2e datasets Usage: bash scripts/batch_validate.sh [tasks_file] [max_parallel] Options: MAX_PARALLEL=N Parallel jobs (default: 20) Examples: # Validate with default settings bash scripts/batch_validate.sh scripts/tasks_30.txt 4 # With custom parallel count bash scripts/batch_validate.sh scripts/tasks_30.txt 10 # Stop all running validation jobs bash scripts/batch_validate.sh --stop
- `scripts/bundle_meta.py`: What a task bundle's oracle declares, read for the QC gate and the judge.
- `scripts/dataset_validate.py`: (no module docstring)
- `scripts/deliverables.py`: Client deliverables tree, one project per task:
- `scripts/finance_client.py`: finance_client.py - Post trajectory usage to the Ethara Finance API (Odoo).
- `scripts/glm_bridge.py`: Lifecycle owner for the vendored zbridge subprocess (GLM on a Z.ai Coding Plan).
- `scripts/harbor_runner.py`: harbor_runner.py -- drive one attempt of a bundle through the pinned Harbor release.
- `scripts/install_bridge_deps.sh`: Host-side deps for the three relay bridges: the Claude Code subscription bridge (scripts/claude_oauth), the codex judge bridge (scripts/codex_oauth) and the Z.ai Coding Plan bridge (scripts/zbridge, started by glm_bridge.py). They all run on the HOST (not in the task container) using the same Python that runs run_harbor.py, so install these into that interpreter. httpx is usually already present; fastapi + uvicorn power the Anthropic-compatible proxy servers. For a full harness install prefer `uv sync` at the harness root against pyproject.toml + uv.lock; this script is the pip fallback for environments where uv is not available.
- `scripts/install_codex.sh`: adapted from https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/agents/installed_agents/codex/codex-setup.sh.j2
- `scripts/install_gemini_cli.sh`: adapted from https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/agents/installed_agents/gemini_cli/gemini-cli-setup.sh.j2
- `scripts/install_opencode.sh`: Install the OpenCode agent (https://github.com/sst/opencode) inside the task container. Mirrors the codex / gemini-cli install pattern (nvm + npm global).
- `scripts/install_openhands.sh`: adapted from https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/agents/installed_agents/openhands/openhands-setup.sh.j2
- `scripts/install_openhands_sdk.sh`: Install the OpenHands Software Agent SDK (lighter alternative to full openhands-ai). Expects the SDK source to have been copied to /opt/software-agent-sdk/ by the caller.
- `scripts/install_validate_deps.sh`: Creates /scripts/.venv (Python 3 venv with tomli) for the in-container self-test (validate.py). This is the same form every shipped task uses and the only form the QC gate accepts (QC-06 rejects uv). It fails loudly: the instruction hands the agent /scripts/.venv/bin/python, so a silent miss here would kill the self-test loop the methodology depends on.
- `scripts/judge.py`: judge.py -- run the rubric judge on its own, apart from a task run.
- `scripts/judge_container.py`: judge_container.py -- run the rubric judge in a sealed, throw-away container.
- `scripts/judge_lib.py`: judge_lib.py -- the LLM rubric judge, extracted from run_harbor.py.
- `scripts/pack.sh`: Pack source trees into a reproducible src.tgz for a task payload. * tracked files only (`git ls-files -c`): untracked leftovers such as a poc.bin, crash.log or build tree never ship to the agent; * a deny-list for artefact names, as a second line of defence; * uniform mtime / owner / ordering, so the archive carries no metadata that reveals which files were edited last (an injected defect is otherwise a one-command `tar tvzf` tell).
- `scripts/pin_docker_images.sh`: pin_docker_images.sh - Pull unpinned Docker images and print pinned FROM lines. This script does NOT modify Dockerfiles automatically. It pulls each unpinned image, resolves the sha256 digest, and prints the pinned FROM line you can paste into the Dockerfile. Usage: bash scripts/pin_docker_images.sh # pin all unpinned images bash scripts/pin_docker_images.sh --apply # pin and update Dockerfiles in-place bash scripts/pin_docker_images.sh --dry-run # just list what would be pinned Requires: docker CLI with access to pull from Docker Hub / gcr.io
- `scripts/pull_images.py`: Pre-download all Docker images referenced by project and task configs.
- `scripts/rejudge_sync.py`: Propagate a re-judged rubric_score.json into every derived verifier file.
- `scripts/render_readme.py`: render_readme.py: regenerate harness/README.md from the harness bytes.
- `scripts/run_agent.py`: Unified agent runner for cybergym-e2e.
- `scripts/run_sdk_agent.py`: Run a cybergym agent using the OpenHands Software Agent SDK.
- `scripts/scrub_oracle.sh`: Remove a task's target library from the agent runtime venv, then prove it is gone. See ORACLE_LEAK_FIX.md. Why this exists: the agent image layers an OpenHands SDK venv on top of the clean task image. That venv's dependency closure ships readable, released copies of third-party libraries -- including, for a PyJWT task, a PyJWT four minor versions ahead of the vulnerable baseline at /src. A single `diff` against /src then hands the agent the injected CWE for free, collapsing the discovery phase. Removing the target from the agent runtime closes that. Usage: scrub_oracle.sh <dist-name> e.g. scrub_oracle.sh pyjwt SCOPE: this touches /opt/openhands-sdk-venv ONLY. The task's own editable install under the system interpreter (/usr/local/lib/pythonX.Y/site-packages, pointing at /src) is the baseline the verifier needs -- never remove it.
- `scripts/stage_names.py`: Single source of truth for the verifier test-name -> stage mapping.
- `scripts/tasks.txt`
- `scripts/touchstone_shim.py`: touchstone_shim.py -- convert a touchstones/<case>/ bundle into a Harbor-shaped calibration probe bundle that run_harbor.py can execute.
- `scripts/trajectory.py`: Claude Code logs -> ATIF-v1.7 trajectory.json.
- `scripts/utils.py`: Shared utilities for cybergym-e2e scripts.
- `scripts/validate.py`: Unified validation script for cybergym-e2e.
- `scripts/claude_oauth/` (directory)
- `scripts/codex_oauth/` (directory)
- `scripts/harbor_agents/` (directory)
- `scripts/zbridge/` (directory)

## Library modules

- `lib/__init__.py`: Trinity harness library (FORGE.md:184).
- `lib/base.py`: Abstract Benchmark base class (FORGE.md:184 three-duty interface).
- `lib/cli.py`: Shared harness command-line base (FORGE.md:184).
- `lib/registry.py`: Benchmark plugin registry (FORGE.md:184).
- `lib/validate.py`: Unified validation script for cybergym-e2e.

## Projects corpus

No `projects/` directory found.

## Environment variables

Keys documented in `.env.example`:

- `JUDGE_MODEL`
- `JUDGE_CALIBRATION_MODEL`
- `WCB_CC_ACCOUNT_POOL`
- `WCB_CC_ROTATION_STATE`

Runtime credentials are read from `.env` (gitignored) and the real environment; a real `.env` never travels to `main`. See each entry point's docstring for the full behaviour of every variable it reads.

## Runtime state

The following patterns are gitignored per `.gitignore` and reflect runtime output the harness produces or consumes rather than tracked bytes:

- `.env`
- `agent_output/`
- `run_logs/`
- `tasks/`
- `data/`
- `.venv/`
- `__pycache__/`
- `*.pyc`
- `.DS_Store`
- `*.log`
- `dataset/`
- `jobs/`
- `.serena/`
- `raw_data/`
- `evidence/`
- `deliverables/`
- `/tests/`
- `docs/`
- `input/`
- `judge/`
- `derived/`

## Dependency pinning

Pinning artifacts present at the harness root:

- `pyproject.toml` (sha256 prefix `b625d8d92fe238e2`)
- `uv.lock` (sha256 prefix `29fb48edd76fdde1`)
- `harbor.lock` (sha256 prefix `5ecfa2087e5a912f`)
- `.python-version` (sha256 prefix `7b55f8e67b5623c4`)

## Configuration manifests

Root-level configuration and image sources present at the harness root:

- `Dockerfile` (sha256 prefix `af593318fa52c68a`)
- `.dockerignore` (sha256 prefix `1ae7bdd349f874a5`)
- `harness-config.json` (sha256 prefix `ad64b1506047926f`)

## Benchmarks

Self-describing benchmark directories under `benchmarks/`, per trinity/FORGE.md:184:

- `benchmarks/cybergym_e2e/benchmark.toml` (sha256 prefix `2a080b2e411de454`)
- `benchmarks/touchstone_calibration/benchmark.toml` (sha256 prefix `7686c7e4170945d5`)

## External configuration surface

Files under `config/`, the externalization surface for the agent under test:

- `config/adapters-registry.json`
- `config/agent.example.toml`

## Contract boundaries

- FORGE alone reconciles bytes inside this submodule; any other instrument writing here is a scaffold defect that trinity/FORGE.md records as a named coverage gap.
- Scoring is delegated from the rollout log to each bundle's own `tests/` under the pinned Harbor release. The extension carries no grading logic of its own that a bundle does not override.
- Neither `solution/` nor `trajectories/` is mounted on any path this extension exposes to the agent under test, so the private-boundary leak gate holds.
- FORGE never executes this extension against a solver. An author-side run is recorded as a `.seed/probe.yaml` entry, Bucket N and never difficulty evidence; every measured rollout comes from the out-of-band pilot runner.
- This file is regenerated every run; hand edits are overwritten.

## License

See `LICENSE` (sha256 prefix `c71d239df91726fc`). Bundle contents may carry their own upstream licenses recorded in each bundle's manifest.

## Regeneration

Regenerate this README from the harness root:

```
python3 scripts/render_readme.py
```

Check for drift without writing:

```
python3 scripts/render_readme.py --check
```

The emitter reads no network, no clock, and no random source; two runs over the same tree produce byte-identical output.

