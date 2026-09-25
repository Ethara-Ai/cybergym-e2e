"""judge_container.py -- run the rubric judge in a sealed, throw-away container.

Where the judge runs decides what can touch its inputs and its verdict:

* the AGENT container executes the model's commands;
* the VERIFIER container builds and runs the model's patch (tests/test.sh),
  so agent-controlled code executes there too;
* the HOST runs the harness.

Neither container is a safe home for the judge, so it gets a third one: a
container started from the same pinned python:3.12-slim image the harness
image itself is built from, holding only judge_lib, a copy of the bundle's
tests/ (never solution/), the trajectory and the test results, on an internal
Docker network whose only exit is a one-port relay to the judge endpoint (the
codex bridge on the host, or an Anthropic-compatible endpoint).  No agent
code ever runs in it, its inputs are copied in before it starts, and its
verdict is copied out when it exits.  The judge's credential (the bridge
secret) enters this container only.

Stdlib only.  The judge itself (scripts/judge_lib.py) is stdlib-only too, so
the image needs no build step.
"""

import json
import os
import platform as _platform_mod
import shlex
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

HARNESS_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent

# Same pin as the harness image (Dockerfile FROM line); python:3.12-slim.
JUDGE_IMAGE = "python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9"
# One-port relay between the internal network and the judge endpoint.
RELAY_IMAGE = "alpine/socat:1.8.0.0"
RELAY_ALIAS = "bridge-relay"
# Modules the judge needs beside judge_lib.py itself.
JUDGE_MODULES = ("judge_lib.py", "bundle_meta.py")
# Bundle files the judge may read.  solution/ and environment/ never travel.
JUDGE_TASK_FILES = ("task.toml", "instruction.md", "metadata.json")
# Environment the judge reads (scripts/judge_lib.py); the endpoint variables
# are overridden with the relay address below.
JUDGE_ENV_PREFIXES = ("JUDGE_",)
JUDGE_ENV_KEYS = ("KAKASHI_CODEX_BRIDGE_SECRET",)
DEFAULT_CODEX_BRIDGE_URL = "http://127.0.0.1:8788"


def _host_docker_platform():
    m = _platform_mod.machine().lower()
    if m in ("arm64", "aarch64"):
        return "linux/arm64"
    return "linux/amd64"


DEFAULT_JUDGE_PLATFORM = _host_docker_platform()

# The driver that runs inside the container.  It mirrors what run_harbor.py
# did on the host: rubric verdict (always) and the calibration check (graded
# attempts only), each written as its own JSON so a failure of one never
# hides the other.
JUDGE_DRIVER = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, "/judge/lib")
import judge_lib

mode = sys.argv[1]
task = Path("/judge/task")
out = Path("/judge/out"); out.mkdir(exist_ok=True)
traj = Path("/judge/agent.jsonl").read_text(errors="replace")
llm_env_path = Path("/judge/llm_env.json")
llm_env = json.loads(llm_env_path.read_text()) if llm_env_path.exists() else {}
model = os.environ.get("KAKASHI_AGENT_MODEL") or None
record = {"mode": mode, "rubric_error": None, "calibration_error": None}

try:
    rubric = judge_lib.evaluate_rubric(task, traj, llm_env, model)
except Exception as e:  # noqa: BLE001 - reported, never lost
    rubric, record["rubric_error"] = None, f"{type(e).__name__}: {e}"
record["last_judge_failure"] = judge_lib.LAST_JUDGE_FAILURE
if rubric:
    (out / "rubric_score.json").write_text(json.dumps(rubric, indent=2))

if mode == "rubric":
    test_results = json.loads(Path("/judge/test_results.json").read_text())
    try:
        cal = judge_lib.evaluate_judge_calibration(task, traj, test_results, llm_env, model)
    except Exception as e:  # noqa: BLE001
        cal, record["calibration_error"] = None, f"{type(e).__name__}: {e}"
    if cal:
        (out / "calibration.json").write_text(json.dumps(cal, indent=2))
(out / "judge.json").write_text(json.dumps(record, indent=2))
'''


class JudgeContainerError(RuntimeError):
    """The judge sandbox could not be set up (never a judge verdict)."""


def _docker(*args, check=False, timeout=600, capture=True):
    r = subprocess.run(["docker", *args], capture_output=capture, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise JudgeContainerError(f"docker {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[-300:]}")
    return r


def judge_endpoint(provider, llm_env=None):
    """(url, host, port, scheme) of the endpoint the judge will call, as the
    host sees it.  Same resolution order as judge_lib.judge_transport."""
    env = os.environ
    override = (env.get("JUDGE_BASE_URL") or "").strip()
    if provider == "codex":
        base = override or (env.get("CODEX_BRIDGE_URL") or "").strip() or DEFAULT_CODEX_BRIDGE_URL
    else:
        llm_env = llm_env or {}
        base = (override or llm_env.get("LLM_BASE_URL") or llm_env.get("ANTHROPIC_BASE_URL")
                or env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com")
    u = urlparse(base)
    scheme = u.scheme or "http"
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if scheme == "https" else 80)
    return base, host, port, scheme


def _relay_target(host):
    """Loopback on the host is the container's own loopback; Docker publishes
    the host as host.docker.internal (host-gateway on Linux)."""
    if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
        return "host.docker.internal"
    return host


def _create_relay_network(run_id, target_host, target_port, platform):
    net = f"kakashi-judge-net-{run_id}"
    _docker("network", "create", "--internal", net, check=True)
    relay = f"kakashi-judge-relay-{run_id}"
    r = _docker("run", "-d", "--rm", "--platform", platform,
                "--network", net, "--network-alias", RELAY_ALIAS,
                "--add-host", "host.docker.internal:host-gateway",
                "--name", relay, RELAY_IMAGE,
                f"TCP-LISTEN:{target_port},fork,reuseaddr", f"TCP:{target_host}:{target_port}")
    if r.returncode != 0:
        _docker("network", "rm", net)
        raise JudgeContainerError(f"could not start the judge relay: {r.stderr.strip()[-200:]}")
    relay_cid = r.stdout.strip()
    r = _docker("network", "connect", "bridge", relay_cid)
    if r.returncode != 0:
        _docker("rm", "-f", relay_cid)
        _docker("network", "rm", net)
        raise JudgeContainerError(f"could not attach the relay to the default bridge: {r.stderr.strip()[-200:]}")
    return net, relay_cid


def _cleanup(cid, relay_cid, net):
    for c in (cid, relay_cid):
        if c:
            _docker("rm", "-f", c)
    if net:
        _docker("network", "rm", net)


def _stage_inputs(stage, task_dir, log_file, test_results, llm_env_for_judge):
    """Copy exactly what the judge may read into a host-side staging dir."""
    lib = stage / "lib"
    lib.mkdir()
    for name in JUDGE_MODULES:
        (lib / name).write_bytes((SCRIPTS_DIR / name).read_bytes())
    task = stage / "task"
    (task / "tests").mkdir(parents=True)
    for name in JUDGE_TASK_FILES:
        src = Path(task_dir) / name
        if src.is_file():
            (task / name).write_bytes(src.read_bytes())
    tests_src = Path(task_dir) / "tests"
    for p in tests_src.rglob("*"):
        if p.is_symlink():
            raise JudgeContainerError(f"symlink in bundle tests/ is not supported: {p}")
        rel = p.relative_to(tests_src)
        if p.is_dir():
            (task / "tests" / rel).mkdir(parents=True, exist_ok=True)
        else:
            (task / "tests" / rel).write_bytes(p.read_bytes())
    (stage / "agent.jsonl").write_text(
        Path(log_file).read_text(errors="replace") if Path(log_file).exists() else "",
        encoding="utf-8")
    (stage / "test_results.json").write_text(json.dumps(test_results or {}), encoding="utf-8")
    if llm_env_for_judge:
        (stage / "llm_env.json").write_text(json.dumps(llm_env_for_judge), encoding="utf-8")
    (stage / "run_judge.py").write_text(JUDGE_DRIVER, encoding="utf-8")


def judge_env(provider, relay_url, llm_env=None):
    """Environment handed to the judge container: the JUDGE_* knobs, the
    bridge secret, and the endpoint rewritten to the relay.  Nothing else
    from the host environment crosses."""
    env = {k: v for k, v in os.environ.items()
           if (k.startswith(JUDGE_ENV_PREFIXES) or k in JUDGE_ENV_KEYS) and v}
    env.pop("JUDGE_BASE_URL", None)
    env["JUDGE_PROVIDER"] = provider
    if provider == "codex":
        env["CODEX_BRIDGE_URL"] = relay_url
        env.setdefault("KAKASHI_CODEX_BRIDGE_SECRET", "codex-bridge")
    else:
        env["JUDGE_BASE_URL"] = relay_url
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            if (llm_env or {}).get(k):
                env[k] = llm_env[k]
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_judge_in_container(*, task_dir, log_file, test_results, llm_env, llm_model, out_dir,
                           mode="rubric", provider="codex", platform=DEFAULT_JUDGE_PLATFORM,
                           timeout=3600, on_output=None):
    """Judge one attempt's trajectory inside the sealed container.

    Returns (rubric_data, calibration_data, record) where record describes
    the sandbox (image, network, endpoint) and any judge error, for
    summary.json.  Raises JudgeContainerError only when the sandbox itself
    could not be built; a judge failure inside comes back as rubric_data=None
    with the reason in record, exactly like the host-side judge did.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_url, host, port, scheme = judge_endpoint(provider, llm_env)
    target_host = _relay_target(host)
    # The relay speaks TCP; an https endpoint keeps TLS end to end because the
    # container pins the real hostname to the relay's address in /etc/hosts.
    relay_url = f"{scheme}://{host}:{port}" if scheme == "https" else f"http://{RELAY_ALIAS}:{port}"
    llm_env_for_judge = None
    if provider != "codex":
        llm_env_for_judge = {k: v for k, v in (llm_env or {}).items()
                             if k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    run_id = uuid.uuid4().hex[:8]
    record = {
        "mode": "container",
        "image": JUDGE_IMAGE,
        "network": "internal + one-port relay",
        "endpoint": f"{target_host}:{port} ({provider})",
        "relay_url": relay_url,
        "run_id": run_id,
        "rubric_error": None,
        "calibration_error": None,
    }
    cid = relay_cid = net = None
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="kakashi-judge-") as tmp:
        stage = Path(tmp) / "judge"
        stage.mkdir()
        _stage_inputs(stage, task_dir, log_file, test_results, llm_env_for_judge)
        try:
            net, relay_cid = _create_relay_network(run_id, target_host, port, platform)
            env = judge_env(provider, relay_url, llm_env)
            if llm_model:
                env["KAKASHI_AGENT_MODEL"] = str(llm_model)
            cmd = ["run", "-d", "--platform", platform, "--network", net,
                   "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--memory", "1g", "--pids-limit", "256",
                   "--name", f"kakashi-judge-{run_id}", "-w", "/judge"]
            for k, v in env.items():
                cmd += ["-e", f"{k}={v}"]
            cmd += [JUDGE_IMAGE, "sleep", "infinity"]
            r = _docker(*cmd)
            if r.returncode != 0:
                raise JudgeContainerError(f"could not start the judge container: {r.stderr.strip()[-300:]}")
            cid = r.stdout.strip()
            _docker("cp", str(stage) + "/.", f"{cid}:/judge/", check=True)
            if scheme == "https":
                pin = (f"set -e; RIP=$(getent ahostsv4 {RELAY_ALIAS} | awk '{{print $1}}' | head -1); "
                       f"[ -n \"$RIP\" ]; grep -v ' {host}$' /etc/hosts > /tmp/hosts.new || true; "
                       f"cat /tmp/hosts.new > /etc/hosts; echo \"$RIP {host}\" >> /etc/hosts")
                pr = _docker("exec", cid, "sh", "-c", pin)
                if pr.returncode != 0:
                    raise JudgeContainerError(f"could not pin {host} to the relay: {pr.stderr.strip()[-200:]}")
            # No route but the relay: the judge cannot reach anything else and
            # nothing else can reach it.
            probe = _docker("exec", cid, "python3", "-c",
                            "import socket,sys\n"
                            "s=socket.socket(); s.settimeout(4)\n"
                            "try:\n s.connect(('1.1.1.1',80)); print('PROBE: internet reachable'); sys.exit(1)\n"
                            "except OSError: print('PROBE: internet blocked')")
            record["isolation_probe"] = (probe.stdout or "").strip()
            if probe.returncode != 0 and "reachable" in (probe.stdout or ""):
                raise JudgeContainerError("judge container can reach the internet; internal network not applied")
            r = subprocess.run(["docker", "exec", cid, "python3", "/judge/run_judge.py", mode],
                               capture_output=True, text=True, timeout=timeout, errors="replace")
            if r.stdout and on_output:
                on_output(r.stdout)
            if r.returncode != 0:
                record["rubric_error"] = f"judge driver exited {r.returncode}: {(r.stderr or '')[-600:]}"
            _docker("cp", f"{cid}:/judge/out/.", str(out_dir))
        finally:
            _cleanup(cid, relay_cid, net)
    record["duration_seconds"] = round(time.time() - started, 1)
    inner = _load(out_dir / "judge.json")
    if inner:
        record["rubric_error"] = record["rubric_error"] or inner.get("rubric_error")
        record["calibration_error"] = inner.get("calibration_error")
        record["last_judge_failure"] = inner.get("last_judge_failure")
    return _load(out_dir / "rubric_score.json"), _load(out_dir / "calibration.json"), record


def _load(path):
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


def describe_command(cid_or_name, mode):
    return " ".join(shlex.quote(c) for c in ["docker", "exec", cid_or_name, "python3", "/judge/run_judge.py", mode])
