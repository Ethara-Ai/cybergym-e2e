"""harbor_runner.py -- drive one attempt of a bundle through the pinned Harbor release.

run_harbor.py used to reimplement the whole trial with raw ``docker`` calls
(agent container, in-container lockdown, socat relay, fresh verifier
container).  With ``--runner harbor`` (the default) that orchestration is
delegated to ``harbor run``: Harbor starts the environment, installs and runs
the agent under a phase-scoped network policy, collects the declared
artifacts, grades in a **separate** verifier environment and writes the trial
result.  This module owns the three pieces the delegation needs:

1. ``stage_task``: a Harbor-ready copy of the bundle.  Bundles ship in the
   Harbor format already; what they lack is the runner-side policy the legacy
   path applied at runtime (which image to run, which host the agent may reach,
   which files to carry into the verifier, the verifier image).  Those are
   written into a staged ``task.toml`` / ``tests/Dockerfile``; the bundle on
   disk is never modified and the overlay is recorded next to the run for
   audit.
2. ``run_trial``: the ``harbor run`` subprocess (streamed, killable, with an
   outer wall-clock kill as a safety net over Harbor's own per-phase budgets).
3. ``import_trial``: map Harbor's trial directory back onto the run layout
   the rest of run_harbor.py (judge, deliverables, finance, pass@k) reads.

Everything here is stdlib-only except ``tomli_w`` (already a declared
dependency); Harbor itself is only ever invoked as a CLI.
"""

import json
import os
import re
import secrets
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import tomli_w

HARNESS_ROOT = Path(__file__).resolve().parent.parent
HARBOR_LOCK_PATH = HARNESS_ROOT / "harbor.lock"

# Paths fixed by Harbor's environment layout (harbor.models.trial.paths).
ENV_ARTIFACTS_DIR = "/logs/artifacts"
ENV_AGENT_DIR = "/logs/agent"
ENV_TESTS_DIR = "/tests"
# Where the collect hook snapshots the agent's final files.  Harbor downloads
# everything under /logs/artifacts (its convention directory) into the trial's
# artifacts/ tree, mirrored at the absolute container path
# (artifacts/logs/artifacts/kakashi/), so the hook never needs a declared
# artifact of its own.
KAKASHI_ARTIFACTS_SUBDIR = "kakashi"


def kakashi_snapshot_dir(host_artifacts_dir):
    """Host path of the collect hook's snapshot inside a trial's artifacts/."""
    host_artifacts_dir = Path(host_artifacts_dir)
    mirrored = host_artifacts_dir / "logs" / "artifacts" / KAKASHI_ARTIFACTS_SUBDIR
    if mirrored.is_dir():
        return mirrored
    return host_artifacts_dir / KAKASHI_ARTIFACTS_SUBDIR

# Legacy verifier location.  The runner used to copy tests/ to /verifier and
# run `bash /verifier/test.sh`; Harbor runs /tests/test.sh.  The verifier
# image links the two so a bundle (or generate_report.py) that hardcodes the
# old path keeps working.
LEGACY_VERIFIER_DIR = "/verifier"

# Marker file the wrapper test.sh looks for: the bundle's own script, renamed
# so the wrapper can sit at the path Harbor executes.
BUNDLE_TEST_SH = "kakashi_bundle_test.sh"

# Agent-phase exception types Harbor records when the agent itself failed but
# the trial went on to verification (mirrors run_claude_code_agent's "timed
# out -- collecting partial output" path).  NonZeroAgentExitCodeError has a
# family of ApiError subclasses (harbor.agents.installed.base) that Harbor
# raises when it recognises a provider error in the CLI output; every one of
# them is the agent process exiting non-zero.
AGENT_PHASE_EXCEPTIONS = {"AgentTimeoutError", "NonZeroAgentExitCodeError"}
AGENT_PHASE_EXCEPTION_SUFFIXES = ("ApiError", "ExceededError", "ExitCodeError")


def is_agent_phase_exception(exc_type):
    return bool(exc_type) and (exc_type in AGENT_PHASE_EXCEPTIONS
                               or exc_type.endswith(AGENT_PHASE_EXCEPTION_SUFFIXES))

# Harbor exception types that mean the sandbox policy could not be applied.
ISOLATION_EXCEPTION_PATTERN = re.compile(
    r"network|egress|allowlist|dynamic_network_policy|policy", re.IGNORECASE)


class HarborRunnerError(RuntimeError):
    """A runner-side failure (not an agent or verifier outcome)."""


# ---------------------------------------------------------------------------
# Harbor CLI / pin
# ---------------------------------------------------------------------------

def read_harbor_lock(path=HARBOR_LOCK_PATH):
    """harbor.lock as a dict; {} when the file is missing."""
    path = Path(path)
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def harbor_cli():
    """The `harbor` executable: the one beside the running interpreter first
    (the harness venv), then PATH.  Raises HarborRunnerError with the install
    command when neither exists."""
    # Not resolve(): a venv interpreter is a symlink into the uv/pyenv install,
    # whose bin/ holds no harbor.  The venv's own bin/ is what matters.
    candidates = [Path(sys.executable).parent / "harbor"]
    on_path = shutil.which("harbor")
    if on_path:
        candidates.append(Path(on_path))
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    raise HarborRunnerError(
        "harbor CLI not found beside the interpreter or on PATH. Install the pinned "
        "release into the harness environment:\n"
        "    uv venv --python 3.12 && uv pip install --python .venv/bin/python -r pyproject.toml\n"
        "then run with .venv/bin/python run_harbor.py ...")


def installed_harbor_version(cli):
    r = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=60)
    m = re.search(r"(\d+\.\d+\.\d+)", (r.stdout or "") + (r.stderr or ""))
    if not m:
        raise HarborRunnerError(
            f"could not read the harbor version from `{cli} --version`: "
            f"{((r.stdout or '') + (r.stderr or '')).strip()[-200:]}")
    return m.group(1)


def check_harbor_pin(cli, lock=None):
    """Refuse to run under a Harbor release other than harbor.lock's.

    harbor.lock is the pin the Trinity contract counts; a run under a different
    release would be scored by a verifier stack the record does not describe.
    Returns the pinned release string."""
    lock = read_harbor_lock() if lock is None else lock
    pinned = str(lock.get("harbor_release") or "").strip()
    if not pinned:
        raise HarborRunnerError(
            f"{HARBOR_LOCK_PATH} declares no harbor_release; the runner needs an exact pin")
    installed = installed_harbor_version(cli)
    if installed != pinned:
        raise HarborRunnerError(
            f"installed harbor {installed} does not match harbor.lock harbor_release={pinned}; "
            f"re-pin both pyproject.toml and harbor.lock together, or install the pinned release")
    return pinned


# Harbor's own egress-control gate (harbor.environments.docker.DockerEnvironment
# ._egress_control_kernel_support): the daemon's kernel must offer nftables fib
# rules or Harbor turns the sidecar off and rejects every non-public policy.
# Same pinned probe image and script, so the answer matches Harbor's.
EGRESS_PROBE_IMAGE = "alpine:3.23.4@sha256:5b10f432ef3da1b8d4c7eb6c487f2f5a8f096bc91145e68878dd4a5019afde11"
EGRESS_PROBE_SCRIPT = ("if [ ! -f /proc/config.gz ]; then exit 0; fi; "
                       "zcat /proc/config.gz 2>/dev/null | grep -qE '^CONFIG_NFT_FIB_INET=[ym]'")


# Harbor's egress sidecar proxies every agent request through gost, and gost's
# default read timeout is 15s.  An LLM turn's time-to-first-byte grows with the
# prompt, so once a trajectory's context passes roughly 100 KB the model needs
# longer than that to start answering and gost severs the connection with zero
# bytes returned.  The client sees "Server disconnected without sending a
# response" and retries the same oversized request, which fails identically, so
# the agent never recovers -- while the LLM bridge logs only 200s, because it is
# still legitimately waiting upstream when gost gives up.  gost.yaml ships
# inside the pinned Harbor wheel, so a fresh `uv sync` reinstates the 15s
# default: patch it at startup rather than leaving it to a manual step.
GOST_CONFIG_REL = "environments/docker/harbor-docker-egress-control-sidecar/gost.yaml"
GOST_READ_TIMEOUT = os.environ.get("KAKASHI_GOST_READ_TIMEOUT", "").strip() or "900s"


def gost_config_path():
    """Absolute path to the installed sidecar's gost.yaml, or None."""
    try:
        import harbor
    except Exception:
        return None
    p = Path(harbor.__file__).resolve().parent / GOST_CONFIG_REL
    return p if p.is_file() else None


def ensure_gost_read_timeout():
    """Give gost a read timeout long enough for an LLM turn.

    Idempotent: returns (status, detail) where status is "ok" (already long
    enough), "patched" (rewritten now) or "skipped" (nothing to patch / not
    writable).  Never raises -- a failure here must not abort the run, it is
    reported so the caller can warn.
    """
    p = gost_config_path()
    if p is None:
        return "skipped", "installed gost.yaml not found"
    try:
        text = p.read_text()
    except OSError as e:
        return "skipped", f"unreadable: {e}"
    if "readTimeout" in text:
        return "ok", f"read timeout already set ({p})"
    # Anchor on the handler's last metadata line AND the listener that follows
    # it, so a layout change fails the match instead of inserting blind.  All
    # three blocks are written: the observed cut was on the upstream read
    # ("read response: read tcp ... i/o timeout"), which the connector owns,
    # so a handler-only timeout is not the configuration that was verified.
    marker = "        sniffing.fallback: true\n    listener:\n"
    if marker not in text:
        return "skipped", f"unrecognised gost.yaml layout ({p}); patch by hand"
    patched = text.replace(marker, (
        "        sniffing.fallback: true\n"
        f"        readTimeout: {GOST_READ_TIMEOUT}\n"
        "    dialer:\n"
        "      type: direct\n"
        "      metadata:\n"
        "        timeout: 60s\n"
        "    connector:\n"
        "      type: direct\n"
        "      metadata:\n"
        f"        readTimeout: {GOST_READ_TIMEOUT}\n"
        "    listener:\n"), 1)
    try:
        p.write_text(patched)
    except OSError as e:
        return "skipped", f"not writable ({e}); export KAKASHI_GOST_READ_TIMEOUT and patch by hand"
    return "patched", f"read timeout set to {GOST_READ_TIMEOUT} in {p}"


def egress_control_supported():
    """(supported, detail): can Harbor's docker provider enforce no-network /
    allowlist policies against this daemon?  Docker Desktop's linuxkit kernel
    ships without CONFIG_NFT_FIB_INET, so on a Mac the answer is usually no;
    a stock Linux kernel says yes.  A probe that cannot run is reported as
    unsupported rather than guessed."""
    try:
        r = subprocess.run(["docker", "container", "run", "--rm", EGRESS_PROBE_IMAGE,
                            "sh", "-c", EGRESS_PROBE_SCRIPT],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"kernel probe could not run: {e}"
    if r.returncode == 0:
        return True, "daemon kernel has nftables fib support (or exposes no /proc/config.gz)"
    kern = subprocess.run(["docker", "container", "run", "--rm", EGRESS_PROBE_IMAGE, "uname", "-r"],
                          capture_output=True, text=True, timeout=120)
    return False, (f"daemon kernel {kern.stdout.strip() or '?'} lacks CONFIG_NFT_FIB_INET; "
                   "Harbor's egress-control sidecar is disabled on this host")


def docker_compose_available():
    """(ok, detail).  Harbor's docker environment needs the compose plugin."""
    try:
        r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True,
                           timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"docker compose not runnable: {e}"
    if r.returncode != 0:
        return False, f"docker compose version failed: {(r.stderr or r.stdout).strip()[-200:]}"
    return True, (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else "ok"


# ---------------------------------------------------------------------------
# Agent prep image
# ---------------------------------------------------------------------------

def build_agent_image(base_image, tag, install_script, platform, need_boto3=False, timeout=1800,
                      context_files=None):
    """Layer the legacy in-container agent install on top of the bundle image.

    The legacy runner ran `install_claude_code` inside every fresh agent
    container.  Under Harbor the same script is baked into a per-task image
    instead: the agent binary keeps its provenance (npm package, pinned
    version), Harbor's own claude-code install step sees the pinned version
    and skips itself, repeat runs of a task hit the Docker cache, and the
    agent environment needs no network at all outside the agent phase.

    The verifier image is NOT built from this: grading starts from the
    pristine bundle image, exactly as the legacy fresh verifier container did.
    """
    context_files = dict(context_files or {})
    copies = "".join(f"COPY {name} {target}\n" for name, (_, target) in context_files.items())
    dockerfile = (
        "# GENERATED by kakashi harness (scripts/harbor_runner.py): the agent\n"
        "# install script, applied at image-build time for Harbor.\n"
        f"FROM {base_image}\n"
        "ARG NEED_BOTO3=0\n"
        + copies +
        "COPY install_agent.sh /tmp/kakashi_install_agent.sh\n"
        "RUN NEED_BOTO3=$NEED_BOTO3 bash /tmp/kakashi_install_agent.sh "
        "&& rm -f /tmp/kakashi_install_agent.sh\n"
    )
    with tempfile.TemporaryDirectory(prefix="kakashi-agent-image-") as ctx:
        (Path(ctx) / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        (Path(ctx) / "install_agent.sh").write_text(install_script, encoding="utf-8")
        for name, (source, _) in context_files.items():
            shutil.copy2(source, Path(ctx) / name)
        r = subprocess.run(
            ["docker", "build", "--platform", platform, "-q", "-t", tag,
             "--build-arg", f"NEED_BOTO3={'1' if need_boto3 else '0'}", ctx],
            capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise HarborRunnerError(
            f"agent prep image build failed on top of {base_image}:\n{(r.stderr or '')[-1500:]}")
    return r.stdout.strip() or tag


def tag_image(image_id, tag):
    r = subprocess.run(["docker", "tag", image_id, tag], capture_output=True, text=True)
    if r.returncode != 0:
        raise HarborRunnerError(f"docker tag {image_id} {tag} failed: {r.stderr.strip()[-200:]}")
    return tag


def untag_image(tag):
    subprocess.run(["docker", "rmi", "--no-prune", tag], capture_output=True, text=True)


def image_digest(image):
    r = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", image],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


UV_RELEASE_URL = "https://github.com/astral-sh/uv/releases/download/{version}/uv-{target}.tar.gz"
UV_TARGETS = {"linux/amd64": "x86_64-unknown-linux-gnu", "linux/arm64": "aarch64-unknown-linux-gnu"}


def uv_release_tarball(version, platform, cache_dir=None):
    """The pinned uv release for `platform` (a docker platform string), fetched
    once on the host into a cache and COPYed into the agent image.  Same
    source and cache the vendored openhands-sdk agent uses at run time, so
    the image and a run-time bootstrap would land the identical binary."""
    import urllib.request
    target = UV_TARGETS.get(platform)
    if target is None:
        raise HarborRunnerError(f"no uv release target for docker platform {platform!r}")
    if cache_dir is None:
        cache_dir = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "harbor" / "uv"
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tarball = cache_dir / f"uv-{version}-{target}.tar.gz"
    if tarball.is_file() and tarball.stat().st_size > 0:
        return tarball
    url = UV_RELEASE_URL.format(version=version, target=target)
    partial = tarball.with_suffix(".part")
    last = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                partial.write_bytes(r.read())
            break
        except OSError as e:
            last = e
            time.sleep(2 * attempt)
    else:
        raise HarborRunnerError(f"could not download uv from {url}: {last}")
    partial.replace(tarball)
    return tarball


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

def collect_hook_script(agent_repo_dir, stage_repo_dir=None):
    """Shell run in the agent container after the agent phase, before Harbor
    collects artifacts.  Snapshots what `collect_agent_artifacts` used to pull
    off the live container with `docker cp` / `docker diff`:

    * /output/*                     -> /logs/artifacts/kakashi/output/
    * files named by fix.patch      -> /logs/artifacts/kakashi/workspace/<rel>
      (final content, from the repo the agent patched)
    * changed-path listing          -> /logs/artifacts/kakashi/container_changes.txt
      (`find -newer /logs/agent`: /logs/agent is created when the environment
      starts, so this approximates `docker diff` on /src and /output)
    * egress probe                  -> /logs/artifacts/kakashi/netprobe.txt
      (does an IP-literal connect leave the container under the baseline?)

    When the task's tests read artefacts from the source tree
    (`stage_repo_dir`), poc.bin / fix.patch / crash.log are also copied there
    so the declared `<repo_dir>/…` artifacts carry them into the verifier.
    Runs under `bash -c` (Harbor's main-service exec shell); never fails the
    trial (Harbor logs a non-zero hook and moves on).
    """
    stage = ""
    if stage_repo_dir:
        stage = f"""
STAGE={shlex.quote(stage_repo_dir)}
mkdir -p "$STAGE" 2>/dev/null || true
for f in poc.bin fix.patch crash.log; do
  [ -f "/output/$f" ] && cp -a "/output/$f" "$STAGE/$f" 2>/dev/null || true
done
"""
    return f"""set -u
A={ENV_ARTIFACTS_DIR}/{KAKASHI_ARTIFACTS_SUBDIR}
REPO={shlex.quote(agent_repo_dir or "/src")}
mkdir -p "$A/output" "$A/workspace"
[ -d /output ] && cp -a /output/. "$A/output/" 2>/dev/null || true
if [ -f /output/fix.patch ]; then
  grep -E '^(\\+\\+\\+|---) ' /output/fix.patch | cut -c5- | cut -f1 | sed -e 's/[[:space:]]*$//' \\
    | grep -v '^/dev/null$' | sed -E 's#^[ab]/##' | awk '!seen[$0]++' \\
    | while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        src="$REPO/$rel"
        if [ -f "$src" ]; then
          mkdir -p "$A/workspace/$(dirname "$rel")"
          cp -a "$src" "$A/workspace/$rel" 2>/dev/null || true
        fi
      done
fi
{stage}
find /src /output -xdev -type f -newer {ENV_AGENT_DIR} 2>/dev/null | head -20000 > "$A/container_changes.txt" || true
# Egress probe under the environment baseline (the agent-phase allowlist has
# already been lifted): only a reply proves the packet left the container.
# A bare connect cannot: the egress sidecar redirects all TCP to gost on
# localhost, so the handshake succeeds against the proxy even when gost
# then refuses to forward, which read as "reachable" on every enforced run.
if timeout 8 bash -c 'exec 3<>/dev/tcp/1.1.1.1/80 || exit 1
printf "GET / HTTP/1.0\\r\\nHost: 1.1.1.1\\r\\n\\r\\n" >&3
head -c 1 <&3 | grep -q .' 2>/dev/null; then
  echo "PROBE: internet reachable (1.1.1.1:80)" > "$A/netprobe.txt"
else
  echo "PROBE: internet blocked (1.1.1.1:80)" > "$A/netprobe.txt"
fi
echo "kakashi collect hook: $(find "$A" -type f | wc -l) files"
"""


def verifier_dockerfile(base_image, wrapper=False):
    """tests/Dockerfile for Harbor's separate verifier environment.

    Harbor builds the verifier image from the task's tests/ directory and
    expects /tests/test.sh inside it; it uploads nothing at run time.  The
    image is the pristine bundle image plus the tests, exactly the file set
    the legacy verifier container saw after `docker cp tests/. /verifier/`.
    """
    lines = [
        "# GENERATED by kakashi harness (scripts/harbor_runner.py) for Harbor's",
        "# separate verifier environment.  Do not ship with the bundle.",
        f"FROM {base_image}",
        f"COPY . {ENV_TESTS_DIR}/",
        # Legacy path compatibility (see LEGACY_VERIFIER_DIR).
        f"RUN [ -e {LEGACY_VERIFIER_DIR} ] || ln -s {ENV_TESTS_DIR} {LEGACY_VERIFIER_DIR}",
        f"RUN chmod +x {ENV_TESTS_DIR}/test.sh"
        + (f" {ENV_TESTS_DIR}/{BUNDLE_TEST_SH}" if wrapper else ""),
    ]
    return "\n".join(lines) + "\n"


def wrapper_test_sh():
    """test.sh that runs the harness report generator before the bundle's own
    script (report-based tasks without a generator of their own).  Mirrors the
    legacy run_verifier sequence."""
    return f"""#!/usr/bin/env bash
# GENERATED by kakashi harness (scripts/harbor_runner.py).  Report-based task
# without its own tests/gen_report.py: build report.json with the harness
# generator (legacy run_verifier did the same), then hand over to the bundle's
# test.sh, renamed {BUNDLE_TEST_SH}.
set -uo pipefail
TESTS_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
PY=/scripts/.venv/bin/python; [ -x "$PY" ] || PY=python3
$PY -c "import tomli" 2>/dev/null || pip install -q tomli 2>/dev/null || true
if ! (cd "$TESTS_DIR" && $PY generate_report.py); then
  echo "generate_report.py exited $? (continuing to the bundle's test.sh)" >&2
fi
exec bash "$TESTS_DIR/{BUNDLE_TEST_SH}"
"""


@dataclass
class AgentNetwork:
    """Agent-phase network policy: what the agent may reach while it runs."""
    mode: str                      # "allowlist" | "public"
    allowed_hosts: list = field(default_factory=list)

    @classmethod
    def allow(cls, *hosts):
        return cls("allowlist", [h for h in hosts if h])

    @classmethod
    def public(cls):
        return cls("public", [])


@dataclass
class StagedTask:
    task_dir: Path
    staged_dir: Path
    overlay: dict
    warnings: list

    def cleanup(self):
        shutil.rmtree(self.staged_dir, ignore_errors=True)

    def record(self, dest):
        """Copy the generated files and the overlay summary to `dest` so a
        reader of the run can see exactly what Harbor was handed."""
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.staged_dir / "task.toml", dest / "task.toml")
        for rel in ("tests/Dockerfile", "tests/test.sh"):
            src = self.staged_dir / rel
            if rel == "tests/test.sh" and not self.overlay.get("wrapper_test_sh"):
                continue
            if src.exists():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / rel)
        json.dump({**self.overlay, "warnings": self.warnings},
                  open(dest / "overlay.json", "w"), indent=2)


def _copytree_no_symlinks(src, dst):
    """copytree that refuses symlinks: the staged tree is handed to Docker as a
    build context and to Harbor as a task, and neither should follow links
    out of the bundle."""
    src, dst = Path(src), Path(dst)
    for p in src.rglob("*"):
        if p.is_symlink():
            raise HarborRunnerError(f"symlink in bundle tests/ is not supported: {p}")
    shutil.copytree(src, dst, symlinks=False)


AGENT_WORKDIR = "/src"

# solution/solve.sh generated for Harbor's oracle agent when a bundle ships
# reference artefacts but no script (see stage_task).
ORACLE_SOLVE_SH = """#!/usr/bin/env bash
# GENERATED by kakashi harness (scripts/harbor_runner.py) for `--agent oracle`:
# publish the bundle's reference artefacts exactly where the instruction asks
# an agent to put its own.
set -u
mkdir -p /output
for f in poc.bin fix.patch crash.log; do
  if [ -f "/solution/$f" ]; then cp -f "/solution/$f" "/output/$f"; echo "oracle: published /output/$f"; fi
done
[ -f /output/fix.patch ] || { echo "oracle: solution/ has no fix.patch" >&2; exit 1; }
"""


def stage_task(task_dir, *, agent_image, verifier_base_image, agent_network,
               agent_timeout_sec, verifier_timeout_sec, build_timeout_sec,
               agent_repo_dir, stage_repo_dir=None, report_generator=None,
               include_solution=False, collect_timeout_sec=120, workdir=AGENT_WORKDIR):
    """Write a Harbor-ready copy of `task_dir` and return it as a StagedTask.

    The copy contains only what Harbor reads: task.toml (overlaid), the
    instruction, environment/Dockerfile (so Harbor sees a build spec and does
    not upload environment/ into the container; the image itself is
    `agent_image`, pre-built), tests/ (plus the generated Dockerfile and, for
    report-based tasks, the wrapper), and solution/ only when an oracle agent
    needs it.  Large payloads under environment/ are never copied.
    """
    task_dir = Path(task_dir).resolve()
    if not (task_dir / "task.toml").exists():
        raise HarborRunnerError(f"{task_dir} has no task.toml")
    if (task_dir / "tests" / "Dockerfile").exists():
        raise HarborRunnerError(
            f"{task_dir}/tests/Dockerfile exists: the bundle declares its own verifier image, "
            "which --runner harbor does not stage yet (use --runner legacy)")
    if not (task_dir / "tests" / "test.sh").exists():
        raise HarborRunnerError(f"{task_dir}/tests/test.sh not found")

    warnings = []
    # mkdtemp's random sequence includes '_', so ~1 name in 60 starts with one,
    # producing kakashi-harbor-_xxx.  A docker image-name component must start
    # with [a-z0-9], so that run dies further down the Harbor pipeline on an
    # invalid image name.  secrets.token_hex yields [0-9a-f] only.
    while True:
        candidate = Path(tempfile.gettempdir()) / f"kakashi-harbor-{secrets.token_hex(4)}"
        try:
            candidate.mkdir(mode=0o700, exist_ok=False)
            break
        except FileExistsError:
            continue
    staged = candidate
    for name in ("instruction.md",):
        if (task_dir / name).exists():
            shutil.copy2(task_dir / name, staged / name)
    env_src = task_dir / "environment"
    (staged / "environment").mkdir()
    if (env_src / "Dockerfile").exists():
        shutil.copy2(env_src / "Dockerfile", staged / "environment" / "Dockerfile")
    _copytree_no_symlinks(task_dir / "tests", staged / "tests")
    solve_generated = False
    if include_solution and (task_dir / "solution").is_dir():
        _copytree_no_symlinks(task_dir / "solution", staged / "solution")
        if not (staged / "solution" / "solve.sh").exists():
            # kakashi bundles keep the reference artefacts (poc.bin, fix.patch,
            # crash.log) under solution/ without a script; Harbor's oracle runs
            # solution/solve.sh, so give it one that publishes them the way an
            # agent would.  Never shipped: this copy lives in the staged dir only.
            (staged / "solution" / "solve.sh").write_text(ORACLE_SOLVE_SH, encoding="utf-8")
            (staged / "solution" / "solve.sh").chmod(0o755)
            solve_generated = True

    # Bundles whose tests hardcode the legacy /verifier path still work through
    # the symlink in the verifier image, but say so: a bundle that assumes the
    # tests live at /verifier is relying on runner behaviour, not the format.
    for p in sorted((staged / "tests").rglob("*")):
        if p.is_file() and p.suffix in {".sh", ".py"}:
            try:
                if LEGACY_VERIFIER_DIR + "/" in p.read_text(errors="replace"):
                    warnings.append(f"tests/{p.relative_to(staged / 'tests')} hardcodes "
                                    f"{LEGACY_VERIFIER_DIR}/ (served via symlink to /tests)")
            except OSError:
                pass

    wrapper = False
    if report_generator is not None:
        report_generator = Path(report_generator)
        if not report_generator.exists():
            raise HarborRunnerError(f"report generator {report_generator} not found")
        (staged / "tests" / "test.sh").rename(staged / "tests" / BUNDLE_TEST_SH)
        shutil.copy2(report_generator, staged / "tests" / "generate_report.py")
        (staged / "tests" / "test.sh").write_text(wrapper_test_sh(), encoding="utf-8")
        wrapper = True
    (staged / "tests" / "Dockerfile").write_text(
        verifier_dockerfile(verifier_base_image, wrapper=wrapper), encoding="utf-8")
    for name in ("test.sh", BUNDLE_TEST_SH):
        p = staged / "tests" / name
        if p.exists():
            p.chmod(p.stat().st_mode | 0o111)

    cfg = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    overlay = _overlay_task_config(
        cfg, agent_image=agent_image, agent_network=agent_network,
        agent_timeout_sec=agent_timeout_sec, verifier_timeout_sec=verifier_timeout_sec,
        build_timeout_sec=build_timeout_sec, agent_repo_dir=agent_repo_dir,
        stage_repo_dir=stage_repo_dir, collect_timeout_sec=collect_timeout_sec,
        workdir=workdir)
    (staged / "task.toml").write_text(tomli_w.dumps(cfg), encoding="utf-8")

    overlay.update({
        "source_task_dir": str(task_dir),
        "verifier_base_image": verifier_base_image,
        "verifier_base_image_id": image_digest(verifier_base_image),
        "agent_image_id": image_digest(agent_image),
        "wrapper_test_sh": wrapper,
        "report_generator": str(report_generator) if report_generator else None,
        "solution_included": bool(include_solution and (staged / "solution").is_dir()),
        "solve_sh_generated": solve_generated,
    })
    return StagedTask(task_dir=task_dir, staged_dir=staged, overlay=overlay, warnings=warnings)


# Harbor's [task] PackageInfo rules (harbor.constants.ORG_NAME_PATTERN and the
# Author model): registry-style `org/name`, every author named.  kakashi
# bundles were authored for a runner that never read this table, so the
# staged copy is brought into shape rather than rejected; the rewrite is
# recorded in overlay.json and the bundle itself is untouched.
HARBOR_ORG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*/[a-zA-Z0-9][a-zA-Z0-9._-]*$")
HARBOR_DEFAULT_ORG = "ethara"


def _normalize_task_section(cfg):
    """Make cfg["task"] acceptable to Harbor's PackageInfo; return a record of
    what changed (None when nothing did)."""
    task = cfg.get("task")
    if not isinstance(task, dict):
        return None
    original = json.loads(json.dumps(task))
    name = task.get("name")
    if not isinstance(name, str) or not name.strip():
        name = ""
    if name and "/" not in name:
        name = f"{HARBOR_DEFAULT_ORG}/{name}"
    if name and (not HARBOR_ORG_NAME_PATTERN.match(name) or ".." in name):
        org, _, rest = name.partition("/")
        clean = lambda part: re.sub(r"[^a-zA-Z0-9._-]+", "-", part).strip("._-") or "task"
        name = f"{clean(org) or HARBOR_DEFAULT_ORG}/{clean(rest)}"
    if name:
        task["name"] = name
    else:
        task.pop("name", None)
    authors = task.get("authors")
    if isinstance(authors, list):
        fixed = []
        for a in authors:
            if isinstance(a, str):
                a = {"name": a}
            if not isinstance(a, dict):
                continue
            a = dict(a)
            if not a.get("name"):
                email = str(a.get("email") or "")
                a["name"] = email.split("@", 1)[0] or "unknown"
            fixed.append(a)
        task["authors"] = fixed
    if not task.get("name") and not task.get("version"):
        # Nothing Harbor can package: leave the table out rather than ship a
        # half-empty PackageInfo that fails validation.
        cfg.pop("task")
        return {"original": original, "staged": None}
    return None if task == original else {"original": original, "staged": json.loads(json.dumps(task))}


def _overlay_task_config(cfg, *, agent_image, agent_network, agent_timeout_sec,
                         verifier_timeout_sec, build_timeout_sec, agent_repo_dir,
                         stage_repo_dir, collect_timeout_sec, workdir):
    """Rewrite the runner-owned sections of a bundle's task.toml in place and
    return a record of what changed.  Bundle-authored keys that Harbor ignores
    (dockerfile, environment_mode under [environment], trinity_* tables) are
    left alone; only the deprecated allow_internet is dropped because the
    baseline is set explicitly."""
    task_rewrite = _normalize_task_section(cfg)

    artifacts = ["/output/poc.bin", "/output/fix.patch", "/output/crash.log"]
    if stage_repo_dir:
        artifacts += [f"{stage_repo_dir.rstrip('/')}/{n}" for n in ("poc.bin", "fix.patch", "crash.log")]
    declared = [a for a in cfg.get("artifacts", []) if isinstance(a, str)]
    cfg["artifacts"] = list(dict.fromkeys(declared + artifacts))

    # A public agent phase (--no-lockdown) is the debug mode for hosts whose
    # docker daemon cannot run Harbor's egress sidecar; there every policy has
    # to be public or Harbor rejects the task.  Otherwise: no network outside
    # the agent phase (the agent image already carries the toolchain, see
    # build_agent_image, and the bundle declares no internet).
    lockdown = agent_network.mode != "public"
    baseline = "no-network" if lockdown else "public"

    env = cfg.setdefault("environment", {})
    env.pop("allow_internet", None)
    env.pop("allowed_hosts", None)
    env["docker_image"] = agent_image
    env["network_mode"] = baseline
    env["build_timeout_sec"] = float(build_timeout_sec)
    # The legacy runner exec'd the agent with `-w /src`; Harbor runs agent
    # commands in this directory (the OpenHands SDK takes it as its workspace).
    env["workdir"] = workdir

    agent = cfg.setdefault("agent", {})
    agent["timeout_sec"] = float(agent_timeout_sec)
    agent["network_mode"] = agent_network.mode
    if agent_network.mode == "allowlist":
        agent["allowed_hosts"] = list(agent_network.allowed_hosts)
    else:
        agent.pop("allowed_hosts", None)

    ver = cfg.setdefault("verifier", {})
    ver["timeout_sec"] = float(verifier_timeout_sec)
    ver["environment_mode"] = "separate"
    ver["network_mode"] = baseline
    ver.pop("allowed_hosts", None)
    ver_env = ver.get("environment") or {}
    ver_env.pop("docker_image", None)          # built from tests/Dockerfile
    ver_env.pop("allow_internet", None)
    ver_env.pop("allowed_hosts", None)
    ver_env["network_mode"] = baseline
    ver["environment"] = ver_env
    ver["collect"] = [{
        "service": "main",
        "timeout_sec": float(collect_timeout_sec),
        "command": collect_hook_script(agent_repo_dir, stage_repo_dir),
    }]

    return {
        "task_section": task_rewrite,
        "artifacts": cfg["artifacts"],
        "environment": {"docker_image": agent_image, "network_mode": env["network_mode"],
                        "build_timeout_sec": env["build_timeout_sec"], "workdir": workdir},
        "agent": {k: agent[k] for k in ("timeout_sec", "network_mode", "allowed_hosts") if k in agent},
        "verifier": {"timeout_sec": ver["timeout_sec"], "environment_mode": "separate",
                     "network_mode": baseline,
                     "environment": {"network_mode": baseline},
                     "collect_hook": True},
        "agent_repo_dir": agent_repo_dir,
        "stage_repo_dir": stage_repo_dir,
    }


def validate_staged_task(staged_dir, python=None, timeout=120):
    """(ok, detail): load the staged task with Harbor's own Task model, in a
    subprocess of the harness interpreter (which has harbor installed) so this
    module never imports Harbor.  The one check that matters is Harbor's:
    what `harbor run` would refuse, this refuses first."""
    code = (
        "import sys, warnings; warnings.simplefilter('ignore')\n"
        "from harbor.models.task.task import Task\n"
        "t = Task(sys.argv[1]); c = t.config\n"
        "print('ok', c.task.name if c.task else '(no [task])', c.environment.network_mode.value, "
        "c.agent.network_mode.value if c.agent.network_mode else '-', "
        "c.verifier.environment_mode.value if c.verifier.environment_mode else '-', "
        "len(t.instruction))\n"
    )
    try:
        r = subprocess.run([str(python or sys.executable), "-c", code, str(staged_dir)],
                           capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not run Harbor's task validation: {e}"
    if r.returncode != 0:
        raw = (r.stderr or r.stdout).strip()
        if "ModuleNotFoundError" in raw and "'harbor'" in raw:
            raise HarborRunnerError(
                "harbor is not importable in this Python interpreter; the CLI on PATH is not enough. "
                "Run `uv sync` at the harness root, or `pip install harbor==<pinned>` into the same "
                f"interpreter that runs run_harbor.py. Details: {raw.splitlines()[-1][-200:]}")
        err = raw.splitlines()
        keep = [l for l in err if l and not l.startswith(("Traceback", "  File", "    ", "^"))]
        return False, "\n".join(keep[-12:]) or "unknown validation failure"
    return True, r.stdout.strip()


# ---------------------------------------------------------------------------
# harbor run
# ---------------------------------------------------------------------------

def build_run_command(cli, staged, *, agent, model, jobs_dir, job_name, agent_kwargs=None,
                      agent_env=None, extra_instruction=None, setup_timeout_multiplier=None):
    cmd = [str(cli), "run",
           "-p", str(staged.staged_dir),
           "-a", agent,
           # Must be absolute: Harbor's compose --project-directory resolves a
           # relative -o against a tempdir, not our cwd.  abspath (not resolve)
           # to preserve /var rather than /private/var on macOS.
           "-o", os.path.abspath(str(jobs_dir)),
           "--job-name", job_name,
           "-n", "1",
           "-q", "-y"]
    if model:
        cmd += ["-m", model]
    for k, v in (agent_kwargs or {}).items():
        if v is None or v == "":
            continue
        cmd += ["--ak", f"{k}={v}"]
    for k, v in (agent_env or {}).items():
        if v is None or v == "":
            continue
        cmd += ["--ae", f"{k}={v}"]
    if extra_instruction:
        cmd += ["--extra-instruction", extra_instruction]
    if setup_timeout_multiplier:
        cmd += ["--agent-setup-timeout-multiplier", f"{float(setup_timeout_multiplier):g}"]
    return cmd


@dataclass
class TrialRun:
    job_dir: Path
    trial_dir: Path | None
    result: dict | None
    exit_code: int | None          # None: killed by the outer timeout
    timed_out: bool
    tail: list
    duration_sec: float
    log_path: Path

    @property
    def exception(self):
        return (self.result or {}).get("exception_info") or None


def run_trial(cmd, *, env, jobs_dir, job_name, outer_timeout, log_path, on_line=None,
              cancel_grace=60.0):
    """Run `harbor run` to completion or the outer deadline.

    Output is streamed line by line to `on_line` (and to `log_path`); the last
    60 lines are kept for error reporting.  SIGTERM then SIGKILL on timeout or
    KeyboardInterrupt, so a killed runner never leaves Harbor's containers up
    (Harbor tears them down on SIGTERM).
    """
    jobs_dir = Path(jobs_dir)
    jobs_dir.mkdir(parents=True, exist_ok=True)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    tail = []
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n")
        proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace", bufsize=1)
        timed_out = False
        try:
            assert proc.stdout is not None
            deadline = started + outer_timeout
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            eof = False
            while not eof:
                remaining = deadline - time.time()
                if remaining <= 0:
                    timed_out = True
                    break
                for _ in sel.select(timeout=min(remaining, 1.0)):
                    line = proc.stdout.readline()
                    if not line:
                        eof = True
                        break
                    log.write(line)
                    stripped = line.rstrip()
                    if stripped:
                        tail.append(stripped)
                        del tail[:-60]
                        if on_line:
                            on_line(stripped)
                if proc.poll() is not None and not eof:
                    # Drain whatever is left once the process has exited.
                    rest = proc.stdout.read()
                    if rest:
                        log.write(rest)
                        for stripped in (l.rstrip() for l in rest.splitlines()):
                            if stripped:
                                tail.append(stripped)
                                del tail[:-60]
                                if on_line:
                                    on_line(stripped)
                    eof = True
            sel.close()
            if timed_out:
                _terminate(proc, cancel_grace)
            else:
                proc.wait()
        except BaseException:
            _terminate(proc, cancel_grace)
            raise
    duration = time.time() - started
    job_dir = jobs_dir / job_name
    trial_dir = find_trial_dir(job_dir)
    result = None
    if trial_dir is not None and (trial_dir / "result.json").exists():
        try:
            result = json.load(open(trial_dir / "result.json"))
        except (OSError, ValueError) as e:
            tail.append(f"[kakashi] could not parse {trial_dir / 'result.json'}: {e}")
    return TrialRun(job_dir=job_dir, trial_dir=trial_dir, result=result,
                    exit_code=None if timed_out else proc.returncode, timed_out=timed_out,
                    tail=tail, duration_sec=duration, log_path=log_path)


def _terminate(proc, grace):
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass


def find_trial_dir(job_dir):
    """The single trial directory under a job dir (Harbor names it
    <task>__<id>); None when Harbor died before creating one."""
    job_dir = Path(job_dir)
    if not job_dir.is_dir():
        return None
    trials = [p for p in job_dir.iterdir()
              if p.is_dir() and ((p / "result.json").exists() or (p / "config.json").exists()
                                 or (p / "agent").is_dir())]
    if not trials:
        return None
    return max(trials, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Trial -> run layout
# ---------------------------------------------------------------------------

def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds_between(block):
    if not isinstance(block, dict):
        return None
    a, b = _parse_ts(block.get("started_at")), _parse_ts(block.get("finished_at"))
    if a and b:
        return max(0.0, (b - a).total_seconds())
    return None


def exception_phase(result):
    """Which trial phase the recorded exception fell in, by its timestamp:
    'environment_setup' | 'agent_setup' | 'agent_execution' | 'verifier' |
    None (no exception, or no usable timestamps)."""
    exc = (result or {}).get("exception_info") or {}
    at = _parse_ts(exc.get("occurred_at"))
    if at is None:
        return None
    for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
        block = result.get(phase) or {}
        start, end = _parse_ts(block.get("started_at")), _parse_ts(block.get("finished_at"))
        if start and start <= at and (end is None or at <= end):
            return phase
    return None


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atif_text(message):
    if message is None:
        return ""
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts = []
        for part in message:
            if isinstance(part, dict):
                parts.append(part.get("text") or part.get("content") or "")
            else:
                parts.append(str(part))
        return "\n".join(p for p in parts if p)
    return str(message)


def atif_to_stream_events(trajectory, *, error=None):
    """Render an ATIF trajectory as the Claude Code stream-json event list the
    rest of the harness already reads (judge_lib.compact_trajectory,
    classify_agent_outcome): one `assistant` event per agent step carrying
    thinking / text / tool_use blocks, one `user` event per observation with
    tool_result blocks, `system` for system steps, and a closing `result`
    event.  `error` marks the run as failed (an agent-phase exception)."""
    events = [{"type": "system", "subtype": "init",
               "session_id": trajectory.get("session_id", ""),
               "model": (trajectory.get("agent") or {}).get("model_name", ""),
               "agent": (trajectory.get("agent") or {}).get("name", ""),
               "agent_version": (trajectory.get("agent") or {}).get("version", ""),
               "atif_schema": trajectory.get("schema_version", "")}]
    last_text = ""
    for step in trajectory.get("steps") or []:
        if not isinstance(step, dict):
            continue
        source = step.get("source") or ""
        ts = step.get("timestamp")
        text = _atif_text(step.get("message"))
        if source == "agent":
            content = []
            if step.get("reasoning_content"):
                content.append({"type": "thinking", "thinking": step["reasoning_content"]})
            if text:
                content.append({"type": "text", "text": text})
                last_text = text
            for call in step.get("tool_calls") or []:
                if isinstance(call, dict):
                    content.append({"type": "tool_use", "id": call.get("tool_call_id", ""),
                                    "name": call.get("function_name", ""),
                                    "input": call.get("arguments", {})})
            events.append({"type": "assistant", "step_id": step.get("step_id"), "timestamp": ts,
                           "message": {"role": "assistant", "content": content,
                                       "model": step.get("model_name"),
                                       "usage": _atif_usage(step.get("metrics"))}})
            results = (step.get("observation") or {}).get("results") or []
            if results:
                events.append({"type": "user", "step_id": step.get("step_id"), "timestamp": ts,
                               "message": {"role": "user", "content": [
                                   {"type": "tool_result",
                                    "tool_use_id": r.get("source_call_id", ""),
                                    "content": _atif_text(r.get("content"))}
                                   for r in results if isinstance(r, dict)]}})
        elif source == "user":
            events.append({"type": "user", "step_id": step.get("step_id"), "timestamp": ts,
                           "message": {"role": "user", "content": [{"type": "text", "text": text}]}})
        elif source == "system":
            events.append({"type": "system", "subtype": "prompt", "step_id": step.get("step_id"),
                           "timestamp": ts,
                           "message": {"role": "system", "content": [{"type": "text", "text": text}]}})
    fm = trajectory.get("final_metrics") or {}
    events.append({"type": "result",
                   "subtype": "error_during_execution" if error else "success",
                   "is_error": bool(error),
                   "result": error or last_text,
                   "num_turns": sum(1 for e in events if e["type"] == "assistant"),
                   "total_cost_usd": fm.get("total_cost_usd"),
                   "usage": {"input_tokens": fm.get("total_prompt_tokens"),
                             "output_tokens": fm.get("total_completion_tokens"),
                             "cache_read_input_tokens": fm.get("total_cached_tokens")}})
    return events


def _atif_usage(metrics):
    if not isinstance(metrics, dict):
        return None
    return {"input_tokens": metrics.get("prompt_tokens"),
            "output_tokens": metrics.get("completion_tokens"),
            "cache_read_input_tokens": metrics.get("cached_tokens")}


@dataclass
class AttemptImport:
    """What the legacy attempt body produced before scoring, rebuilt from a
    Harbor trial.  Paths are inside the run directory."""
    log_file: Path
    session_dir: Path
    poc_file: Path
    patch_file: Path
    crash_file: Path
    artifacts_dir: Path
    verifier_raw_dir: Path         # Harbor's verifier/ (reward.json, ctrf.json, test-stdout.txt ...)
    trajectory_file: Path | None   # ATIF trajectory.json already in place (non-Claude agents)
    agent_time: float
    exit_code: int
    stderr: str
    agent_error: str | None        # a runner-side failure before the agent could run
    verifier_error: str | None     # verification did not produce a reward
    verifier_output: str
    harbor: dict                   # summary block for summary.json


def import_trial(trial, *, run_dir, output_dir, trajectory_dir, evidence_dir, attempt,
                 max_attempts, agent_name, staged_policy):
    """Map <trial_dir> onto the run layout run_harbor.py's scoring reads.

    Raises HarborRunnerError when Harbor left no trial at all (a runner
    failure: bad task, missing docker, ...).  Agent and verifier failures are
    returned in the AttemptImport, not raised, so the caller can classify
    them exactly like the legacy path did (agent_error / skipped /
    verifier_error).
    """
    run_dir = Path(run_dir)
    if trial.trial_dir is None or trial.result is None:
        why = "\n".join(trial.tail[-25:])
        if trial.timed_out:
            raise HarborRunnerError(f"harbor run exceeded the outer timeout before writing a "
                                    f"trial result; last output:\n{why}")
        raise HarborRunnerError(
            f"harbor run exited {trial.exit_code} without a trial result (see {trial.log_path}); "
            f"last output:\n{why}")
    t = trial.trial_dir
    result = trial.result
    multi = max_attempts > 1
    suffix = f"_attempt_{attempt}" if multi else ""

    # --- outcome classification (needed by the log synthesis below) --------------
    exc = result.get("exception_info") or {}
    exc_type = exc.get("exception_type") or ""
    exc_msg = exc.get("exception_message") or ""

    # --- agent logs ---------------------------------------------------------
    log_file = Path(trajectory_dir) / (f"attempt_{attempt}.jsonl" if multi else "agent.jsonl")
    session_dir = Path(trajectory_dir) / f"claude_session{suffix}"
    session_dir.mkdir(exist_ok=True)
    harbor_traj = t / "agent" / "trajectory.json"
    trajectory_file = None
    stream_src = t / "agent" / "claude-code.txt"
    if agent_name == "claude-code":
        # Claude Code: its own stream-json log and session transcript, converted
        # by scripts/trajectory.py exactly as under the legacy runner.
        if stream_src.exists():
            shutil.copy2(stream_src, log_file)
        else:
            log_file.write_text("")
        sessions_src = t / "agent" / "sessions" / "projects"
        if sessions_src.is_dir():
            shutil.copytree(sessions_src, session_dir, dirs_exist_ok=True)
        if harbor_traj.exists():
            shutil.copy2(harbor_traj, Path(trajectory_dir) /
                         (f"harbor_trajectory_attempt_{attempt}.json" if multi else "harbor_trajectory.json"))
    else:
        # Every other agent: Harbor's ATIF trajectory IS the trajectory, and the
        # stream-json log the judge reads is rendered from it.
        trajectory_file = Path(trajectory_dir) / (f"trajectory_attempt_{attempt}.json" if multi
                                                  else "trajectory.json")
        traj = {}
        if harbor_traj.exists():
            shutil.copy2(harbor_traj, trajectory_file)
            try:
                traj = json.load(open(harbor_traj))
            except (OSError, ValueError) as e:
                print(f"  !! harbor trajectory.json unreadable: {e}")
        else:
            json.dump({"schema_version": "ATIF-unavailable", "session_id": "", "steps": [],
                       "agent": {"name": agent_name, "version": "", "model_name": ""},
                       "final_metrics": {"total_steps": 0, "extra": {"missing": "harbor wrote no trajectory.json"}}},
                      open(trajectory_file, "w"), indent=2)
        error = (f"{exc_type}: {exc_msg}"
                 if is_agent_phase_exception(exc_type) or exception_phase(result) == "agent_execution"
                 else None)
        events = atif_to_stream_events(traj, error=error)
        with open(log_file, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")
        # The agent's own console output, for the record.
        for name in ("openhands_sdk.txt", "openhands.txt", "oracle.txt"):
            if (t / "agent" / name).exists():
                shutil.copy2(t / "agent" / name, Path(trajectory_dir) /
                             (f"agent_stdout_attempt_{attempt}.log" if multi else "agent_stdout.log"))
                break

    # --- submission ---------------------------------------------------------
    art = t / "artifacts"
    if multi:
        poc_file = Path(output_dir) / f"poc_attempt_{attempt}.bin"
        patch_file = Path(output_dir) / f"fix_attempt_{attempt}.patch"
    else:
        poc_file = Path(output_dir) / "poc.bin"
        patch_file = Path(output_dir) / "fix.patch"
    for src, dst in ((art / "output" / "poc.bin", poc_file),
                     (art / "output" / "fix.patch", patch_file)):
        if src.is_file():
            shutil.copy2(src, dst)
    Path(evidence_dir).mkdir(parents=True, exist_ok=True)
    crash_file = Path(evidence_dir) / (f"crash_attempt_{attempt}.log" if multi else "crash.log")
    snapshot = kakashi_snapshot_dir(art)
    for src in (art / "output" / "crash.log", snapshot / "output" / "crash.log"):
        if src.is_file():
            shutil.copy2(src, crash_file)
            break

    # --- artifacts (legacy layout) -------------------------------------------
    artifacts_dir = run_dir / "artifacts" / (f"attempt_{attempt}" if multi else "")
    _rebuild_artifacts(snapshot, artifacts_dir, staged_policy)

    # --- verifier --------------------------------------------------------------
    verifier_raw_dir = run_dir / "harbor" / (f"attempt_{attempt}" if multi else "trial") / "verifier"
    if (t / "verifier").is_dir():
        shutil.copytree(t / "verifier", verifier_raw_dir, dirs_exist_ok=True)
    else:
        verifier_raw_dir.mkdir(parents=True, exist_ok=True)
    verifier_output = ""
    for name in ("test-stdout.txt", "test-stderr.txt"):
        p = verifier_raw_dir / name
        if p.exists():
            verifier_output += p.read_text(errors="replace")

    # --- outcome classification -------------------------------------------------
    timings = {k: result.get(k) for k in ("environment_setup", "agent_setup", "agent_execution",
                                          "verifier")}
    agent_time = _seconds_between(timings["agent_execution"]) or 0.0
    agent_ran = bool((timings["agent_execution"] or {}).get("started_at"))
    verifier_started = bool((timings["verifier"] or {}).get("started_at"))
    exit_code, stderr, agent_error = 0, "", None
    if is_agent_phase_exception(exc_type) or exception_phase(result) == "agent_execution":
        exit_code = -1 if exc_type == "AgentTimeoutError" else 1
        m = re.search(r"exit(?:ed)?(?: with)? code (\d+)", exc_msg)
        if m and exc_type != "AgentTimeoutError":
            exit_code = int(m.group(1))
        stderr = f"{exc_type}: {exc_msg}"
    elif exc_type and not agent_ran:
        # Environment build, agent setup, network policy: the agent never
        # started, so there is nothing to score (harness_error upstream).
        agent_error = f"{exc_type}: {exc_msg}"
    elif exc_type and not verifier_started:
        # Between agent end and verification start (artifact collection,
        # verifier image build): the agent's work exists but was not graded;
        # surfaces as a verifier_error below once artefacts are known to exist.
        stderr = f"{exc_type}: {exc_msg}"

    verifier_error = None
    rewards = (result.get("verifier_result") or {}).get("rewards")
    reward_json = verifier_raw_dir / "reward.json"
    reward_txt = verifier_raw_dir / "reward.txt"
    if not (reward_json.exists() or reward_txt.exists()) or rewards is None:
        if exc_type and not is_agent_phase_exception(exc_type) and agent_ran \
                and exception_phase(result) != "agent_execution":
            verifier_error = f"{exc_type}: {exc_msg}"
        elif not (reward_json.exists() or reward_txt.exists()):
            verifier_error = ("verifier wrote no reward file "
                              f"(harbor exit {trial.exit_code}); tail:\n" + "\n".join(trial.tail[-15:]))

    netprobe = snapshot / "netprobe.txt"
    netprobe_text = netprobe.read_text(errors="replace").strip() if netprobe.exists() else ""

    harbor_block = {
        "agent": agent_name,
        "netprobe": netprobe_text or None,
        "job_dir": str(trial.job_dir),
        "trial_dir": str(t),
        "trial_name": result.get("trial_name"),
        "task_checksum": result.get("task_checksum"),
        "agent_info": result.get("agent_info"),
        "agent_result": result.get("agent_result"),
        "verifier_result": result.get("verifier_result"),
        "verifier_environment_mode": result.get("verifier_environment_mode"),
        "exception": {"type": exc_type, "message": exc_msg[:2000]} if exc_type else None,
        "timings": {k: _seconds_between(v) for k, v in timings.items()},
        "harbor_exit_code": trial.exit_code,
        "harbor_log": str(trial.log_path),
        "network_policy": staged_policy,
    }
    return AttemptImport(
        log_file=log_file, session_dir=session_dir, poc_file=poc_file, patch_file=patch_file,
        crash_file=crash_file, artifacts_dir=artifacts_dir, verifier_raw_dir=verifier_raw_dir,
        trajectory_file=trajectory_file, agent_time=agent_time, exit_code=exit_code, stderr=stderr, agent_error=agent_error,
        verifier_error=verifier_error, verifier_output=verifier_output, harbor=harbor_block)


def _rebuild_artifacts(src, dest, staged_policy, max_bytes=1_048_576):
    """The legacy artifacts/ tree from the collect hook's snapshot: output/
    and app/workspace/ with a manifest of sizes and digests, and
    container_diff.txt from the hook's change listing."""
    dest = Path(dest)
    workspace = dest / "app" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (dest / "output").mkdir(exist_ok=True)
    manifest = {
        "workspace_root": (staged_policy or {}).get("agent_repo_dir"),
        "method": "harbor [[verifier.collect]] hook: files named in fix.patch (final content) "
                  "+ /output/*; container_diff.txt lists /src and /output files newer than "
                  "the environment start (find -newer, an approximation of docker diff)",
        "max_bytes_per_file": max_bytes,
        "collected": [], "skipped": [],
    }
    src = Path(src)

    def keep(local, container_path):
        size = local.stat().st_size
        if size > max_bytes:
            local.unlink()
            manifest["skipped"].append({"path": container_path, "bytes": size,
                                        "reason": f"larger than {max_bytes} bytes"})
            return
        manifest["collected"].append({"path": container_path, "bytes": size,
                                      "sha256": _sha256_file(local),
                                      "saved_as": str(local.relative_to(dest))})

    if not src.is_dir():
        manifest["skipped"].append({"path": ENV_ARTIFACTS_DIR + "/" + KAKASHI_ARTIFACTS_SUBDIR,
                                    "reason": "collect hook produced nothing"})
    else:
        if (src / "output").is_dir():
            shutil.copytree(src / "output", dest / "output", dirs_exist_ok=True)
            for local in sorted(p for p in (dest / "output").rglob("*") if p.is_file()):
                keep(local, f"/output/{local.relative_to(dest / 'output').as_posix()}")
        repo = (manifest["workspace_root"] or "/src").rstrip("/")
        if (src / "workspace").is_dir():
            shutil.copytree(src / "workspace", workspace, dirs_exist_ok=True)
            for local in sorted(p for p in workspace.rglob("*") if p.is_file()):
                keep(local, f"{repo}/{local.relative_to(workspace).as_posix()}")
        changes = src / "container_changes.txt"
        if changes.exists():
            lines = [l for l in changes.read_text(errors="replace").splitlines() if l.strip()]
            (dest / "container_diff.txt").write_text(
                "\n".join(f"C {l}" for l in lines[:20000]) + "\n")
            manifest["container_changed_paths"] = len(lines)
    json.dump(manifest, open(dest / "manifest.json", "w"), indent=2)
    print(f"  Artifacts: {len(manifest['collected'])} files collected, "
          f"{len(manifest['skipped'])} skipped"
          + (f", {manifest['container_changed_paths']} container paths changed"
             if "container_changed_paths" in manifest else ""))
    return manifest


def is_isolation_failure(trial):
    """True when Harbor refused or lost the network policy (the run must be
    marked isolation_error, never scored)."""
    exc = trial.exception or {}
    text = f"{exc.get('exception_type', '')} {exc.get('exception_message', '')}"
    if exc and ISOLATION_EXCEPTION_PATTERN.search(text) and not is_agent_phase_exception(
            exc.get("exception_type")):
        return True
    if trial.result is None:
        joined = "\n".join(trial.tail)
        return bool(re.search(r"dynamic_network_policy|egress control|network polic", joined,
                              re.IGNORECASE))
    return False
