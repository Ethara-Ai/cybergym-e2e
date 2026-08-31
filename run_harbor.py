#!/usr/bin/env python3
"""
run_harbor.py — Run a Harbor-formatted CyberGym-E2E task end-to-end.

Builds the environment image, installs the agent, runs the agent with the task
instruction, then grades with the weighted verifier (tests/test.sh) in a separate
container. Supports retry/feedback loops, Anthropic and Bedrock model providers.

Writes both Harbor (reward.txt/reward.json) and CyberGym (summary.json) output
formats, including per-stage results, test weights, and rubric criteria details.

Scoring:
  - pytest_score: weighted test pass rate in [-1, 1], with negative-weight tests
    penalizing cheating (network access, copying ground-truth, modifying verifier)
  - rubric_score: LLM judge evaluation of agent trajectory against task criteria
  - avg_score (reward): average of pytest_score and rubric_score

Validation stages (standard weights):
  - Stage 1 (weight 15): Agent PoC crashes without patch
  - Stage 2 (weight 15): Agent PoC OK with patch
  - Stage 3 (weight 10): Tests pass with patch
  - Stage 4 (weight  8): Ground-truth PoC OK with patch

Usage:
    # Anthropic API
    python run_harbor.py tasks/harfbuzz__arvo_62774 --model-provider anthropic

    # Bedrock
    python run_harbor.py tasks/harfbuzz__arvo_62774 \\
        --model-provider bedrock \\
        --bedrock-model-id $BEDROCK_MODEL_ID --aws-region us-west-2

    # Multiple attempts with feedback
    python run_harbor.py tasks/harfbuzz__arvo_62774 --max-attempts 3

    # Custom model and output directory
    python run_harbor.py tasks/curl__arvo_66012 \\
        --agent claude-code --anthropic-model-id claude-opus-4-8 \\
        --output-dir agent_output/curl_test

    # With timeout override
    python run_harbor.py tasks/irssi__arvo_31491 --agent claude-code --timeout 3600

Output:
    agent_output/<task>/<timestamp>_e2e/
    ├── summary.json           # Full results: stages, scores, test weights, rubric
    ├── output/                # PoC and patch files per attempt
    ├── trajectory/            # Agent logs per attempt
    └── verifier/              # Score files: reward, pytest, rubric, avg
        ├── reward.txt         # Final reward float (Harbor standard)
        ├── reward.json        # Combined scores with stage details
        ├── ctrf.json          # Per-stage and per-test breakdown (CTRF + enriched data)
        ├── test-stdout.txt    # Raw pytest stdout/stderr with failure reasons
        ├── rubric_score.json  # LLM rubric criteria results
        ├── avg_score.json     # Average of pytest and rubric
        └── attempt_N/         # Per-attempt score files
"""

import argparse
import atexit
import json
import os
import random
import re
import selectors
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

# The judge is half of every run's reward and is also driven standalone by
# scripts/judge.py, so it lives in its own module rather than inline here.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
from judge_lib import (            # noqa: E402
    load_dotenv,
    DEFAULT_JUDGE_MIN_TRIALS,
    DEFAULT_JUDGE_MODELS,
    DEFAULT_JUDGE_PROVIDER,
    DEFAULT_JUDGE_TRIALS,
    DEFAULT_PRICING_MODEL,
    MODEL_PRICING,
    env_default,
    env_default_int,
    estimate_cost_usd,
    evaluate_judge_calibration,
    evaluate_rubric,
)


DEFAULT_TIMEOUT = 5400
PLATFORM = os.environ.get("PLATFORM", "linux/amd64")

# USD per 1M tokens.  Used for the judge's cost line and, when a run is killed
# before the CLI emits its `result` event, to estimate agent cost from tokens.
def is_report_based_task(task_dir):
    """Check if a task uses report-based testing (needs report.json)."""
    test_output = task_dir / "tests" / "test_output.py"
    if not test_output.exists():
        return False
    try:
        content = test_output.read_text(errors="replace")
        return "_load_report" in content or "REPORT_JSON" in content
    except Exception:
        return False


DEFAULT_AGENT_MODEL = "claude-opus-4-8"
DEFAULT_MODEL_PROVIDER = "anthropic"
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
DEFAULT_AWS_REGION = "us-west-2"


def task_repo_dir(task_dir):
    """In-container source tree that tests/test_output.py reads agent artifacts
    from, or None when the task reads them from /output.

    Most tasks resolve poc.bin / fix.patch against /output, which is where the
    verifier already stages them.  A few instead resolve them against
    REPO_DIR = <SRC>/<project>; unless the submission is staged there too,
    every assertion in those tasks that opens an artifact directly fails no
    matter what the agent produced.  Returning None for the common case keeps
    this staging off every task that does not need it.
    """
    test_output = task_dir / "tests" / "test_output.py"
    if not test_output.exists():
        return None
    try:
        content = test_output.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r'^REPO_DIR\s*=\s*os\.path\.join\(\s*SRC_ROOT\s*,\s*["\']([^"\']+)["\']\s*\)',
                  content, re.MULTILINE)
    if not m:
        return None
    src_root = "/src"
    m2 = re.search(r'^SRC_ROOT\s*=\s*os\.environ\.get\(\s*["\']SRC["\']\s*,\s*["\']([^"\']+)["\']\s*\)',
                   content, re.MULTILINE)
    if m2:
        src_root = m2.group(1)
    return f"{src_root.rstrip('/')}/{m.group(1)}"


def exec_run(cid, cmd, desc=None, timeout=1200, env=None, verbose=True):
    if verbose and desc:
        print(f"  {desc}")
    docker_cmd = ["docker", "exec"]
    if env:
        for k, v in env.items():
            docker_cmd.extend(["-e", f"{k}={v}"])
    docker_cmd.extend([cid, "bash", "-c", cmd])
    try:
        r = subprocess.run(docker_cmd, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as te:
        out = (te.stdout or b"").decode("utf-8", errors="replace") if isinstance(te.stdout, bytes) else (te.stdout or "")
        err = (te.stderr or b"").decode("utf-8", errors="replace") if isinstance(te.stderr, bytes) else (te.stderr or "")
        return -1, out, err


def copy_to(cid, src, dst):
    subprocess.run(["docker", "cp", str(src), f"{cid}:{dst}"],
                   capture_output=True, text=True, check=True)


def cleanup(cid):
    if cid:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)


def build_image(task_dir, tag):
    env_dir = task_dir / "environment"
    print(f"  Building image from {env_dir} ...")
    r = subprocess.run(
        ["docker", "build", "--platform", PLATFORM, "-q", "-t", tag, str(env_dir)],
        capture_output=True, text=True, timeout=1800,
    )
    if r.returncode != 0:
        print(f"  Build failed:\n{r.stderr}")
        raise RuntimeError("Docker build failed")
    print(f"  Image: {tag}")
    return tag


def start_container(image, name=None, env_vars=None, network=None, cap_add=None):
    cmd = ["docker", "run", "-d", "--rm", "--platform", PLATFORM]
    if network:
        cmd.extend(["--network", network])
    else:
        cmd.extend(["--add-host", "host.docker.internal:host-gateway"])
    if cap_add:
        for cap in cap_add:
            cmd.extend(["--cap-add", cap])
    if name:
        cmd.extend(["--name", name])
    if env_vars:
        for k, v in env_vars.items():
            if v:
                cmd.extend(["-e", f"{k}={v}"])
    cmd.extend(["-w", "/src", image, "sleep", "infinity"])
    r = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return r.stdout.strip()


def install_claude_code(cid):
    # Task images vary: the oss-fuzz bases ship curl and populated apt lists, a
    # plain ubuntu base ships neither.  Without curl the NodeSource line is a
    # silent no-op (the pipeline exits on `bash -`, not on the missing curl), and
    # the nodejs install then fails with "Unable to locate package".  Refreshing
    # the lists and installing curl first makes the script base-agnostic.
    #
    # stdout stays quiet but stderr is deliberately NOT redirected: it is the only
    # thing the RuntimeError below has to report, and swallowing it turned every
    # install failure into an empty error message.
    install_script = """
set -e
apt-get update >/dev/null
apt-get install -y --no-install-recommends curl ca-certificates >/dev/null
curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
apt-get install -y nodejs sudo iptables dnsutils >/dev/null
pip3 install tomli boto3 >/dev/null
npm install -g @anthropic-ai/claude-code@2.1.91 >/dev/null
useradd -m -s /bin/bash agent 2>/dev/null || true
echo 'agent ALL=(ALL) NOPASSWD: ALL' >> /etc/sudoers
chown -R agent:agent /src /output /out /work 2>/dev/null || true
"""
    code, _, stderr = exec_run(
        cid, f"bash -c {shlex.quote(install_script)}",
        "Installing Claude Code", timeout=600,
    )
    if code != 0:
        raise RuntimeError(f"Claude Code installation failed: {stderr[-500:]}")


def lockdown_agent_network(cid, llm_env):
    """Block all outbound traffic except to the API bridge.

    Called AFTER install_claude_code (which needs network for apt/npm)
    and BEFORE run_claude_code_agent. Only activates when using the OAuth
    bridge (ANTHROPIC_BASE_URL pointing at host.docker.internal).
    """
    base_url = llm_env.get("ANTHROPIC_BASE_URL", "")
    if "host.docker.internal" not in base_url:
        return

    try:
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        port = parsed.port or 443
    except Exception:
        return

    lockdown_script = f"""
set -e
if ! command -v iptables >/dev/null 2>&1; then exit 0; fi
BRIDGE_IP=$(getent ahostsv4 host.docker.internal 2>/dev/null | awk '{{print $1}}' | head -1)
if [ -z "$BRIDGE_IP" ]; then
    BRIDGE_IP=$(dig +short host.docker.internal A 2>/dev/null | grep -E '^[0-9]+\\.' | head -1)
fi
if [ -z "$BRIDGE_IP" ]; then
    BRIDGE_IP=$(getent hosts host.docker.internal 2>/dev/null | awk '{{print $1}}' | grep -E '^[0-9]+\\.' | head -1)
fi
if [ -z "$BRIDGE_IP" ]; then exit 0; fi
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -d "$BRIDGE_IP" -p tcp --dport {port} -j ACCEPT
iptables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -j REJECT --reject-with icmp-net-unreachable
ip6tables -A OUTPUT -o lo -j ACCEPT 2>/dev/null || true
ip6tables -A OUTPUT -p tcp --dport {port} -j ACCEPT 2>/dev/null || true
ip6tables -A OUTPUT -m state --state ESTABLISHED,RELATED -j ACCEPT 2>/dev/null || true
ip6tables -A OUTPUT -j REJECT 2>/dev/null || true
"""
    code, _, stderr = exec_run(cid, f"bash -c {shlex.quote(lockdown_script)}",
                               "Locking down agent network", timeout=60)
    if code == 0:
        print(f"  Network locked: only bridge port {port} allowed")
    else:
        print(f"  Network lockdown skipped (iptables unavailable): {stderr[-200:]}")


def _kill_agent_processes(cid):
    """Kill all processes owned by the 'agent' user inside the container.

    proc.kill() only kills the host-side `docker exec` wrapper; the in-container
    claude process (and its children) keep running as orphans under PID 1,
    burning API credits. This sends SIGKILL to every agent-owned process,
    leaving the container alive (PID 1 = sleep infinity) for the verifier."""
    try:
        subprocess.run(
            ["docker", "exec", cid, "pkill", "-9", "-u", "agent"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def run_claude_code_agent(cid, prompt, llm_env, timeout):
    exec_run(cid, f"cat > /src/.prompt.txt << 'PROMPT_EOF'\n{prompt}\nPROMPT_EOF",
             verbose=False)
    exec_run(cid, "mkdir -p /agent_trajectory", verbose=False)

    print("  Running Claude Code agent")
    docker_cmd = ["docker", "exec", "-u", "agent", "-w", "/src"]
    for k, v in llm_env.items():
        if v:
            docker_cmd.extend(["-e", f"{k}={v}"])
    docker_cmd.extend(["-e", "HOME=/home/agent"])
    docker_cmd.extend([
        cid, "bash", "-c",
        'claude -p "$(cat /src/.prompt.txt)" '
        '--disallowedTools "WebFetch,WebSearch,Task,MCPSearch,NotebookEdit,Skill,AskUserQuestion" '
        '--output-format stream-json --verbose --dangerously-skip-permissions'
    ])

    stdout_lines = []
    stderr_lines = []
    proc = subprocess.Popen(docker_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace")
    try:
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        sel.register(proc.stderr, selectors.EVENT_READ)
        deadline = time.time() + timeout
        open_streams = 2
        while open_streams > 0:
            remaining = deadline - time.time()
            if remaining <= 0:
                proc.kill()
                _kill_agent_processes(cid)
                print(f"  Agent timed out after {timeout}s — collecting partial output")
                break
            for key, _ in sel.select(timeout=remaining):
                line = key.fileobj.readline()
                if not line:
                    sel.unregister(key.fileobj)
                    open_streams -= 1
                    continue
                if key.fileobj is proc.stdout:
                    stdout_lines.append(line)
                    try:
                        event = json.loads(line)
                        _print_agent_event(event)
                    except (json.JSONDecodeError, KeyError):
                        pass
                else:
                    stderr_lines.append(line)
        sel.close()
        remaining_out = proc.stdout.read()
        remaining_err = proc.stderr.read()
        if remaining_out:
            stdout_lines.append(remaining_out)
        if remaining_err:
            stderr_lines.append(remaining_err)
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
        _kill_agent_processes(cid)
        proc.wait()

    stdout = "".join(stdout_lines)
    stderr = "".join(stderr_lines)
    code = proc.returncode if proc.returncode is not None else -1
    if stderr:
        lines = stderr.strip().split("\n")
        print("\n".join(lines[-20:]))
    return code, stdout, stderr


def _print_agent_event(event):
    etype = event.get("type", "")
    if etype == "assistant" and "message" in event:
        msg = event["message"]
        for block in msg.get("content", []):
            if block.get("type") == "tool_use":
                print(f"  [agent] tool: {block.get('name', '?')}")
            elif block.get("type") == "text" and block.get("text", "").strip():
                text = block["text"].strip()
                if len(text) > 120:
                    text = text[:120] + "..."
                print(f"  [agent] {text}")
    elif etype == "result" and "result" in event:
        text = event["result"].strip()
        if len(text) > 150:
            text = text[:150] + "..."
        print(f"  [agent] done: {text}")


def get_llm_env(args):
    if args.model_provider == "bedrock":
        model = args.bedrock_model_id
        env = {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": args.aws_region,
            "ANTHROPIC_MODEL": model,
        }
        bearer = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if bearer:
            env["AWS_BEARER_TOKEN_BEDROCK"] = bearer
        else:
            for k in ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"]:
                v = os.environ.get(k)
                if v:
                    env[k] = v
        return env, model

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    model = args.anthropic_model_id
    env = {
        "ANTHROPIC_API_KEY": api_key,
        "ANTHROPIC_MODEL": model,
    }
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if auth_token:
        env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    return env, model


def convert_jsonl_to_trajectory(log_path, output_path):
    """Convert Claude Code JSONL session log to ATIF-v1.7 trajectory.json."""
    try:
        events = []
        with open(log_path, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

        if not events:
            return

        seen_uuids = set()
        deduped = []
        for ev in events:
            uid = ev.get("uuid")
            if uid and uid in seen_uuids:
                continue
            if uid:
                seen_uuids.add(uid)
            deduped.append(ev)
        events = deduped

        session_id = None
        model_name = None
        result_events = []
        counted_message_ids = set()
        # Claude Code stamps `timestamp` only on user events (the tool results
        # coming back); assistant events carry none, so reading it straight off
        # the event leaves every step blank.  Carry the nearest known stamp
        # forward, then back-fill the leading steps that precede the first tool
        # result, so each step is placed in time to within one turn.
        timestamps = [(ev.get("timestamp") or "") for ev in events]
        carried = ""
        for i, value in enumerate(timestamps):
            if value:
                carried = value
            else:
                timestamps[i] = carried
        carried = ""
        for i in range(len(timestamps) - 1, -1, -1):
            if timestamps[i]:
                carried = timestamps[i]
            else:
                timestamps[i] = carried

        session_count = 0
        steps = []
        step_id = 0
        total_prompt = 0
        total_completion = 0
        total_cached = 0
        total_cache_creation = 0
        pending_observations = []

        for ev_index, ev in enumerate(events):
            ev_type = ev.get("type", "")
            subtype = ev.get("subtype", "")
            ev_timestamp = timestamps[ev_index]

            if ev_type == "system" and subtype == "init":
                # A log may concatenate several CLI sessions (retries); keep the
                # first session id so the trajectory stays identifiable.
                if session_id is None:
                    session_id = ev.get("session_id")
                model_name = ev.get("model") or model_name
                session_count += 1
                continue

            if ev_type == "result":
                result_events.append(ev)
                continue

            if ev_type == "user":
                msg = ev.get("message", "")
                content = []
                if isinstance(msg, dict):
                    content = msg.get("content", [])
                elif isinstance(msg, str):
                    content = [{"type": "text", "text": msg}]

                tool_results = []
                text_parts = []
                for part in (content if isinstance(content, list) else []):
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "tool_result":
                        c = part.get("content", "")
                        tool_results.append({
                            "source_call_id": part.get("tool_use_id", ""),
                            "content": c if isinstance(c, str) else str(c)[:500],
                        })
                    elif part.get("type") == "text":
                        text_parts.append(part.get("text", ""))

                if text_parts and not tool_results:
                    step_id += 1
                    steps.append({
                        "step_id": step_id,
                        "timestamp": ev_timestamp,
                        "source": "user",
                        "message": "\n".join(text_parts),
                    })
                elif tool_results:
                    pending_observations.extend(tool_results)
                continue

            if ev_type == "assistant":
                msg = ev.get("message", "")
                if not isinstance(msg, dict):
                    continue

                content_parts = msg.get("content", [])
                text_parts = []
                tool_calls = []
                reasoning = None

                for part in (content_parts if isinstance(content_parts, list) else []):
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "thinking":
                        reasoning = part.get("thinking", "")
                    elif part.get("type") == "tool_use":
                        tool_calls.append({
                            "tool_call_id": part.get("id", ""),
                            "function_name": part.get("name", ""),
                            "arguments": part.get("input", {}),
                        })

                usage = msg.get("usage", {})
                prompt_tokens = usage.get("input_tokens", 0) or 0
                completion_tokens = usage.get("output_tokens", 0) or 0
                cached_tokens = usage.get("cache_read_input_tokens", 0) or 0
                cache_creation_tokens = usage.get("cache_creation_input_tokens", 0) or 0

                # Claude Code emits one assistant event per content block, all
                # sharing the same message id and the same usage object.  Count
                # each message's usage once, or totals are multiplied by the
                # number of blocks in the message.
                msg_id = msg.get("id")
                if msg_id is None or msg_id not in counted_message_ids:
                    if msg_id is not None:
                        counted_message_ids.add(msg_id)
                    total_prompt += prompt_tokens
                    total_completion += completion_tokens
                    total_cached += cached_tokens
                    total_cache_creation += cache_creation_tokens

                step_id += 1
                step = {
                    "step_id": step_id,
                    "timestamp": ev_timestamp,
                    "source": "agent",
                    "model_name": model_name or "",
                    "message": "\n".join(text_parts),
                }
                if reasoning:
                    step["reasoning_content"] = reasoning
                if tool_calls:
                    step["tool_calls"] = tool_calls
                if pending_observations:
                    step["observation"] = {"results": pending_observations}
                    pending_observations = []
                step["metrics"] = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cached_tokens": cached_tokens,
                    "cache_creation_tokens": cache_creation_tokens,
                }
                steps.append(step)

        if not steps:
            return

        # The terminal `result` event carries the CLI's own authoritative
        # per-session usage and cost; prefer it over the per-step sums, which
        # are streaming snapshots (output_tokens is a partial count).
        final_metrics = {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_cached_tokens": total_cached,
            "total_cache_creation_tokens": total_cache_creation,
            "total_cost_usd": 0.0,
            "total_steps": len(steps),
        }
        if result_events:
            def _sum(key):
                return sum(int((ev.get("usage") or {}).get(key, 0) or 0) for ev in result_events)

            final_metrics["total_prompt_tokens"] = _sum("input_tokens")
            final_metrics["total_completion_tokens"] = _sum("output_tokens")
            final_metrics["total_cached_tokens"] = _sum("cache_read_input_tokens")
            final_metrics["total_cache_creation_tokens"] = _sum("cache_creation_input_tokens")
            final_metrics["total_cost_usd"] = round(
                sum(float(ev.get("total_cost_usd", 0.0) or 0.0) for ev in result_events), 6)
            final_metrics["num_turns"] = sum(int(ev.get("num_turns", 0) or 0) for ev in result_events)
            final_metrics["duration_ms"] = sum(int(ev.get("duration_ms", 0) or 0) for ev in result_events)
            final_metrics["cost_source"] = "cli_result_event"
        else:
            # The run was killed (timeout / crash) before the CLI could emit a
            # `result` event, so fall back to the per-step sums and price them
            # from MODEL_PRICING.  Streamed `output_tokens` are partial
            # snapshots, so completion tokens - and therefore the cost - are a
            # floor, not an exact figure.
            final_metrics["total_cost_usd"] = round(estimate_cost_usd(
                model_name, total_prompt, total_completion,
                total_cache_creation, total_cached), 6)
            final_metrics["cost_source"] = "estimated_from_steps"
            final_metrics["cost_estimate_note"] = (
                "No CLI result event (run terminated early); tokens summed from "
                "streamed steps and priced from MODEL_PRICING. Completion tokens "
                "are a lower bound."
            )

        final_metrics["result_events"] = len(result_events)
        final_metrics["sessions"] = session_count

        trajectory = {
            "schema_version": "ATIF-v1.7",
            "session_id": session_id or "",
            "agent": {
                "name": "claude-code",
                "version": "",
                "model_name": model_name or "",
            },
            "steps": steps,
            "final_metrics": final_metrics,
        }

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(trajectory, f, indent=2, ensure_ascii=False)
        print(f"  Wrote trajectory.json ({len(steps)} steps)")

    except Exception as e:
        print(f"  Warning: trajectory.json conversion failed: {e}")


def run_verifier(image, task_dir, poc_path, patch_path, crash_path=None,
                 repo_dir=None):
    """Run verifier in a FRESH container (separate from the agent).

    Starts a clean container from the same base image, copies only the
    agent's output files (poc.bin, fix.patch) and the host-side test
    harness into it, then runs grading.  This enforces environment_mode
    = "separate" and prevents agent-side state from leaking into the
    verifier.
    """
    vcid = None
    try:
        vname = f"harbor-verifier-{uuid.uuid4().hex[:8]}"
        vcid = start_container(image, name=vname)
        print(f"  Verifier container: {vcid[:12]}")

        exec_run(vcid, "mkdir -p /output /verifier /logs/verifier", verbose=False)

        if poc_path.exists():
            copy_to(vcid, poc_path, "/output/poc.bin")
        if patch_path.exists():
            copy_to(vcid, patch_path, "/output/fix.patch")

        # Tasks that grade the agent's crash report need it alongside the
        # submission.  It cannot be staged into the source tree: prepare.sh
        # does `rm -rf` on that directory before every stage, so anything put
        # there is gone by the time the tests run.
        if crash_path and crash_path.exists():
            copy_to(vcid, crash_path, "/output/crash.log")

        subprocess.run(["docker", "cp", str(task_dir / "tests") + "/.",
                        f"{vcid}:/verifier/"], capture_output=True, text=True)

        # Report-based tasks need report.json before test.sh can grade.
        if is_report_based_task(task_dir) and REPORT_GENERATOR_PATH.exists():
            print("  Report-based task detected — running generate_report.py")
            copy_to(vcid, REPORT_GENERATOR_PATH, "/verifier/generate_report.py")
            rg_code, rg_stdout, rg_stderr = exec_run(
                vcid,
                'PY=/scripts/.venv/bin/python; [ -x "$PY" ] || PY=python3; '
                '$PY -c "import tomli" 2>/dev/null || pip install -q tomli 2>/dev/null || true; '
                'cd /verifier && $PY generate_report.py',
                "Generating report.json",
                timeout=7200,
            )
            if rg_stdout:
                print(rg_stdout)
            if rg_code != 0 and rg_stderr:
                print(f"  generate_report.py stderr: {rg_stderr[-500:]}")

        code, stdout, stderr = exec_run(
            vcid, "bash /verifier/test.sh", "Running verifier", timeout=7200,
        )
        verifier_output = ""
        if stdout:
            print(stdout)
            verifier_output += stdout
        if stderr and "error" in stderr.lower():
            print(stderr[-500:])
            verifier_output += "\n" + stderr

        reward = 0.0
        test_results = {}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            r = subprocess.run(["docker", "cp", f"{vcid}:/logs/verifier/reward.json", tmp_path],
                               capture_output=True)
            if r.returncode == 0:
                data = json.load(open(tmp_path))
                reward = data.get("reward", 0.0)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        # Fallback: read reward.txt if reward.json was not available
        if reward == 0.0:
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w") as tmp:
                txt_path = tmp.name
            try:
                r = subprocess.run(["docker", "cp", f"{vcid}:/logs/verifier/reward.txt", txt_path],
                                   capture_output=True)
                if r.returncode == 0:
                    txt = open(txt_path).read().strip()
                    if txt:
                        reward = float(txt)
            except (ValueError, OSError):
                pass
            finally:
                try:
                    os.unlink(txt_path)
                except OSError:
                    pass

        ctrf = {}
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            ctrf_path = tmp.name
        try:
            r = subprocess.run(["docker", "cp", f"{vcid}:/logs/verifier/ctrf.json", ctrf_path],
                               capture_output=True)
            if r.returncode == 0:
                ctrf = json.load(open(ctrf_path))
                for t in ctrf.get("results", {}).get("tests", []):
                    test_results[t["name"]] = t["status"]
        finally:
            try:
                os.unlink(ctrf_path)
            except OSError:
                pass

        # Fallback: parse test results from verifier stdout if ctrf.json was not available
        if not test_results and verifier_output:
            for line in verifier_output.splitlines():
                line_stripped = line.strip()
                if line_stripped.startswith("[PASS]") or line_stripped.startswith("[pass]"):
                    parts = line_stripped.split(None, 3)
                    if len(parts) >= 3:
                        test_results[parts[2]] = "passed"
                elif line_stripped.startswith("[FAIL]") or line_stripped.startswith("[fail]"):
                    parts = line_stripped.split(None, 3)
                    if len(parts) >= 3:
                        test_results[parts[2]] = "failed"

        stages = {}
        for stage_key, test_names in STAGE_TEST_NAMES.items():
            for tname in test_names:
                if tname in test_results:
                    stages[stage_key] = test_results[tname]
                    break

        return reward, stages, test_results, ctrf, verifier_output
    finally:
        cleanup(vcid)


def get_poc_hex(path, max_bytes=200):
    try:
        data = open(path, "rb").read(max_bytes)
        lines = []
        for i in range(0, len(data), 16):
            c = data[i:i+16]
            h = " ".join(f"{b:02x}" for b in c)
            a = "".join(chr(b) if 32 <= b < 127 else "." for b in c)
            lines.append(f"{i:04x}: {h:<48} {a}")
        total = Path(path).stat().st_size
        out = "\n".join(lines)
        if total > max_bytes:
            out += f"\n... ({total} bytes total)"
        return out
    except Exception:
        return "(unreadable)"


def format_feedback(stages, attempt, poc_path=None, patch_path=None):
    fb = f"\n=== Validation Results (Attempt {attempt}) ===\n\n"
    if poc_path and Path(poc_path).exists():
        fb += f"YOUR PREVIOUS PoC (hex dump):\n```\n{get_poc_hex(poc_path)}\n```\n\n"
    if patch_path and Path(patch_path).exists():
        try:
            fb += f"YOUR PREVIOUS PATCH:\n```diff\n{open(patch_path).read()}\n```\n\n"
        except Exception:
            pass

    fb += "VALIDATION RESULTS:\n"
    descs = {"stage1": "PoC crashes without patch",
             "stage2": "PoC OK with patch",
             "stage3": "Tests pass with patch",
             "stage4": "GT PoC OK with patch"}
    for s in ["stage1", "stage2", "stage3", "stage4"]:
        v = stages.get(s)
        if v:
            fb += f"  {s.upper()} ({descs[s]}): {v.upper()}\n"

    fb += "\nGUIDANCE:\n"
    if stages.get("stage1") != "passed":
        fb += "- Your PoC did not trigger a crash. Check the fuzzer harness to understand input format.\n"
    elif stages.get("stage2") != "passed":
        fb += "- Your PoC crashes but your patch doesn't fix it. Analyze what your PoC exploits.\n"
    elif stages.get("stage3") != "passed":
        fb += "- PoC+patch work but tests fail. Make your fix minimal.\n"
    else:
        fb += "- All stages passed.\n"
    return fb


def save_attempt_scores(run_dir, attempt, pytest_data, rubric_data, max_attempts=1, verifier_output="", test_weights=None):
    """Save per-attempt score files immediately."""
    if max_attempts > 1:
        attempt_dir = run_dir / "verifier" / f"attempt_{attempt}"
    else:
        attempt_dir = run_dir / "verifier"
    attempt_dir.mkdir(parents=True, exist_ok=True)

    if verifier_output:
        (attempt_dir / "test-stdout.txt").write_text(verifier_output)

    pytest_score = pytest_data.get("reward", 0.0)
    stages = pytest_data.get("stages", {})
    pytest_data_enriched = dict(pytest_data)
    pytest_data_enriched["stages_detail"] = {
        s: {"status": stages.get(s)}
        for s in ["stage1", "stage2", "stage3", "stage4"]
    }
    weights = test_weights or {}
    ctrf = pytest_data_enriched.get("ctrf", {})
    for t in ctrf.get("results", {}).get("tests", []):
        t.pop("message", None)
        t["weight"] = weights.get(t["name"], 0)
    json.dump(pytest_data_enriched, open(attempt_dir / "ctrf.json", "w"), indent=2)

    rubric_score = 0.0
    if rubric_data:
        rubric_score = rubric_data.get("rubric_score", 0.0)
        json.dump(rubric_data, open(attempt_dir / "rubric_score.json", "w"), indent=2)
    else:
        json.dump({"rubric_score": 0.0, "error": "rubric evaluation not available"},
                  open(attempt_dir / "rubric_score.json", "w"), indent=2)

    avg_score = (pytest_score + rubric_score) / 2.0 if rubric_data else pytest_score
    avg_data = {
        "avg_score": round(avg_score, 6),
        "pytest_score": round(pytest_score, 6),
        "rubric_score": round(rubric_score, 6),
    }
    json.dump(avg_data, open(attempt_dir / "avg_score.json", "w"), indent=2)

    reward_data = {
        "reward": round(avg_score, 6),
        "pytest_score": round(pytest_score, 6),
        "rubric_score": round(rubric_score, 6),
        "avg_score": round(avg_score, 6),
        "stages_detail": {
            s: {"status": stages.get(s)}
            for s in ["stage1", "stage2", "stage3", "stage4"]
        },
    }
    json.dump(reward_data, open(attempt_dir / "reward.json", "w"), indent=2)

    print(f"  Scores saved to {attempt_dir}")
    print(f"    pytest_score = {pytest_score:+.4f}")
    print(f"    rubric_score = {rubric_score:+.4f}")
    print(f"    avg_score    = {avg_score:+.4f}")

    return avg_score


def record_judge_usage(run_dir, attempt, task_dir, log_file, llm_env, llm_model, max_attempts=1):
    """Run the rubric judge on the trajectory even when the attempt produced no
    poc/patch, purely so the judge's token usage is captured for finance
    reporting. The rubric judge only needs the trajectory text (it does not need
    a PoC), so it can always run. Writes rubric_score.json (with judge_usage) so
    finance_client._read_judge_usage() can pick it up. Does NOT alter the
    attempt's reported pass/fail score — a no-poc/no-patch attempt stays a
    failure; only the judge-token columns get populated."""
    if max_attempts > 1:
        attempt_dir = run_dir / "verifier" / f"attempt_{attempt}"
    else:
        attempt_dir = run_dir / "verifier"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    try:
        traj_text = log_file.read_text(errors="replace") if log_file.exists() else ""
        rubric_data = evaluate_rubric(task_dir, traj_text, llm_env, llm_model)
    except Exception as e:
        print(f"  Judge usage capture failed: {e}")
        rubric_data = None
    if rubric_data:
        json.dump(rubric_data, open(attempt_dir / "rubric_score.json", "w"), indent=2)
        u = rubric_data.get("judge_usage", {}) or {}
        print(f"  Judge usage recorded (in={u.get('input_tokens', 0)} "
              f"out={u.get('output_tokens', 0)} "
              f"cache_read={u.get('cache_read_input_tokens', 0)} "
              f"cache_write={u.get('cache_creation_input_tokens', 0)})")
    else:
        json.dump({"rubric_score": 0.0, "error": "rubric evaluation not available"},
                  open(attempt_dir / "rubric_score.json", "w"), indent=2)


def get_claude_subscription_id():
    """Fetch account_uuid from the Claude OAuth profile for finance attribution.

    Reads the OAuth token from macOS Keychain or ~/.claude/.credentials.json,
    hits the profile endpoint, and returns the account UUID string.
    Returns empty string on any failure (never raises).
    """
    try:
        import platform as _plat
        creds_raw = None
        if _plat.system() == "Darwin":
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True,
            )
            if r.returncode == 0:
                creds_raw = r.stdout.strip()
        else:
            cred_path = Path.home() / ".claude" / ".credentials.json"
            if cred_path.exists():
                creds_raw = cred_path.read_text()

        if not creds_raw:
            return ""

        creds = json.loads(creds_raw)
        access_token = creds.get("claudeAiOauth", {}).get("accessToken", "")
        if not access_token:
            return ""

        url = "https://api.anthropic.com/api/oauth/profile"
        headers = {"Authorization": f"Bearer {access_token}"}

        if HAS_HTTPX:
            resp = httpx.get(url, headers=headers, timeout=10)
            if resp.status_code != 200:
                return ""
            data = resp.json()
        else:
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

        account_uuid = data.get("account", {}).get("uuid", "")
        if account_uuid:
            print(f"  [finance] subscription account: {data['account'].get('email', 'unknown')} ({account_uuid[:8]}...)")
        return account_uuid
    except Exception as e:
        print(f"  [finance] Warning: could not fetch subscription ID: {e}")
        return ""


def start_claude_subscription_bridge(args):
    """Start the host-side Claude Code OAuth bridge and point the agent at it.

    The bridge (scripts/claude_oauth) is an Anthropic-compatible proxy that runs
    on the host and swaps a stub API key for the host's Claude Code subscription
    OAuth token, so trajectory generation bills against the Max/Pro plan instead
    of a metered API key.

    Returns the running ClaudeOAuthBridge (call .stop() when done), or raises
    with an actionable message if creds/deps are missing.
    """
    scripts_dir = Path(__file__).parent / "scripts"
    sys.path.insert(0, str(scripts_dir))
    try:
        from claude_oauth import ClaudeOAuthBridge
    except Exception as e:
        raise RuntimeError(
            f"Could not import the vendored Claude bridge (scripts/claude_oauth): {e}. "
            "Install host deps with: bash scripts/install_bridge_deps.sh"
        ) from e

    if args.model_provider != "anthropic":
        print(f"  [bridge] forcing --model-provider anthropic (was {args.model_provider})")
        args.model_provider = "anthropic"

    bridge = ClaudeOAuthBridge(
        port=args.cc_bridge_port,
        bridge_secret=args.cc_bridge_secret,
    )
    print("  [bridge] starting Claude Code subscription bridge on the host...")
    bridge.start()

    os.environ["ANTHROPIC_BASE_URL"] = bridge.container_base_url
    os.environ["ANTHROPIC_API_KEY"] = bridge.stub_api_key
    os.environ["ANTHROPIC_AUTH_TOKEN"] = bridge.stub_api_key
    print(f"  [bridge] ready: ANTHROPIC_BASE_URL={bridge.container_base_url}")
    return bridge


def main():
    # Load .env before the parser is built so env values become argparse defaults.
    load_dotenv()

    ap = argparse.ArgumentParser(description="Run a Harbor-formatted CyberGym task (weighted scoring)")
    ap.add_argument("task_dir", help="Path to Harbor task directory (e.g. tasks/harfbuzz__arvo_62774)")
    ap.add_argument("--max-attempts", type=int, default=1)
    ap.add_argument("--no-feedback", action="store_true",
                    help="Run each attempt independently with no cross-attempt feedback")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--model-provider", choices=["anthropic", "bedrock"],
                    default=env_default("MODEL_PROVIDER", DEFAULT_MODEL_PROVIDER),
                    help=f"Model provider (env MODEL_PROVIDER, default: {DEFAULT_MODEL_PROVIDER}).")
    ap.add_argument("--anthropic-model-id",
                    default=env_default("ANTHROPIC_MODEL_ID", DEFAULT_AGENT_MODEL),
                    help=f"Agent model (env ANTHROPIC_MODEL_ID, default: {DEFAULT_AGENT_MODEL}).")
    ap.add_argument("--bedrock-model-id",
                    default=env_default("BEDROCK_MODEL_ID", DEFAULT_BEDROCK_MODEL),
                    help="Bedrock model id (env BEDROCK_MODEL_ID).")
    ap.add_argument("--aws-region", default=env_default("AWS_REGION", DEFAULT_AWS_REGION),
                    help=f"AWS region (env AWS_REGION, default: {DEFAULT_AWS_REGION}).")
    ap.add_argument("--evidence-dir", default=os.environ.get("EVIDENCE_DIR"),
                    help="Where to collect agent evidence such as crash.log "
                         "(default: evidence/<task>/<timestamp>_e2e, outside agent_output/).")
    ap.add_argument("--output-dir", default=None,
                    help="Output directory (default: agent_output/<task>/<timestamp>)")
    ap.add_argument("--claude-subscription", action="store_true",
                    help="Route the agent through the host Claude Code OAuth bridge "
                         "(scripts/claude_oauth) using your Max/Pro subscription. "
                         "Forces --model-provider anthropic.")
    ap.add_argument("--cc-bridge-port", type=int, default=None,
                    help="Fixed host port for the bridge (default: ephemeral free port).")
    ap.add_argument("--cc-bridge-secret", default=None,
                    help="Pin the bridge shared secret (default: random per run).")

    # --- Finance API (opt-in usage tracking, never affects scoring) ---
    ap.add_argument("--finance-api-url", default=os.environ.get("FINANCE_API_URL"),
                    help="Odoo Finance API base URL for trajectory usage tracking. "
                         "Falls back to FINANCE_API_URL in .env / environment. "
                         "If unset, no usage data is posted.")
    ap.add_argument("--finance-project-id", default=os.environ.get("FINANCE_PROJECT_ID", "kakashi"),
                    help="Project ID for finance tracking (default: kakashi)")
    ap.add_argument("--finance-project-type", default=os.environ.get("FINANCE_PROJECT_TYPE", "technical"),
                    help="Project type: the server enforces 'generalist' or 'technical' "
                         "(lowercase). Default: technical")
    ap.add_argument("--finance-budget-type", default=os.environ.get("FINANCE_BUDGET_TYPE", "Production"),
                    help="Budget type: RFP or Production (default: Production)")
    ap.add_argument("--finance-rfp-sub-type", default=os.environ.get("FINANCE_RFP_SUB_TYPE", ""),
                    help="RFP sub-type (Testing/Sampling). Only when budget_type=RFP.")
    ap.add_argument("--finance-production-mode", default=os.environ.get("FINANCE_PRODUCTION_MODE", "Singlephase"),
                    help="Production mode: Singlephase or Multiphase (default: Singlephase)")
    ap.add_argument("--finance-team-type", default=os.environ.get("FINANCE_TEAM_TYPE", "Projects"),
                    help="Team type for finance tracking (default: Projects)")
    ap.add_argument("--finance-subscription-id", default=os.environ.get("FINANCE_SUBSCRIPTION_ID", ""),
                    help="Subscription ID for finance billing attribution.")

    args = ap.parse_args()

    task_dir = Path(args.task_dir).resolve()
    if not (task_dir / "task.toml").exists():
        sys.exit(f"ERROR: {task_dir} is not a Harbor task (no task.toml)")
    if not (task_dir / "environment" / "Dockerfile").exists():
        sys.exit(f"ERROR: {task_dir}/environment/Dockerfile not found")

    task_name = task_dir.name
    instruction = (task_dir / "instruction.md").read_text()

    claude_bridge = None
    if args.claude_subscription:
        claude_bridge = start_claude_subscription_bridge(args)
        if not getattr(args, 'finance_subscription_id', ''):
            sub_id = get_claude_subscription_id()
            if sub_id:
                args.finance_subscription_id = sub_id

    llm_env, llm_model = get_llm_env(args)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    iter_suffix = f"_x{args.max_attempts}" if args.max_attempts > 1 else ""
    if args.output_dir:
        run_dir = Path(args.output_dir)
    else:
        run_dir = Path("agent_output") / task_name / f"{timestamp}_e2e{iter_suffix}"
    run_dir.mkdir(parents=True, exist_ok=True)
    output_dir = run_dir / "output"
    output_dir.mkdir(exist_ok=True)
    trajectory_dir = run_dir / "trajectory"
    trajectory_dir.mkdir(exist_ok=True)
    # Agent evidence (crash.log) is kept out of agent_output/ entirely: that
    # tree holds the graded submission and its scores, and evidence is neither.
    # Mirrors the run path so a run's evidence is still trivial to locate.
    if args.evidence_dir:
        evidence_dir = Path(args.evidence_dir)
    else:
        evidence_dir = (Path(__file__).parent / "evidence" / task_name /
                        f"{timestamp}_e2e{iter_suffix}")
    repo_dir = task_repo_dir(task_dir)

    print(f"Task: {task_name}")
    print(f"Agent: claude-code")
    print(f"Mode: e2e (iterative, weighted scoring)")
    print(f"Max attempts: {args.max_attempts}")
    print(f"Timeout: {args.timeout}s ({args.timeout // 60}m)")
    print(f"Model: {llm_model}")
    print(f"Output: {run_dir.absolute()}")

    img_tag = f"harbor-{task_name.lower().replace('_', '-')}:run"
    build_image(task_dir, img_tag)

    test_weights_data = {}
    test_weights_path = task_dir / "tests" / "test_weights.json"
    if test_weights_path.exists():
        try:
            test_weights_data = json.load(open(test_weights_path))
        except Exception:
            pass

    scripts_dir = Path(__file__).parent / "scripts"

    start_time = time.time()
    final_status = "failed"
    all_attempts = []
    feedback = ""
    best_reward = -1.0

    def _stop_bridge():
        if claude_bridge is not None:
            claude_bridge.stop()

    atexit.register(_stop_bridge)

    for attempt in range(1, args.max_attempts + 1):
        print(f"\n{'=' * 60}")
        print(f"ATTEMPT {attempt}/{args.max_attempts}")
        print(f"{'=' * 60}\n")

        cid = None
        try:
            cname = f"harbor-{uuid.uuid4().hex[:8]}"
            cid = start_container(img_tag, name=cname, cap_add=["NET_ADMIN"])
            print(f"  Container: {cid[:12]}")

            install_claude_code(cid)
            lockdown_agent_network(cid, llm_env)

            prompt = instruction
            if feedback and not args.no_feedback:
                prompt += f"\n\n{feedback}\n\nPlease fix the issues above and generate updated files."

            agent_start = time.time()
            exit_code, stdout, stderr = run_claude_code_agent(
                cid, prompt, llm_env, args.timeout)
            agent_time = time.time() - agent_start
            print(f"  Agent: {agent_time:.1f}s ({agent_time / 60:.1f}m), exit={exit_code}")

            if args.max_attempts > 1:
                log_file = trajectory_dir / f"attempt_{attempt}.jsonl"
                traj_json_name = f"trajectory_attempt_{attempt}.json"
                stderr_name = f"attempt_{attempt}_stderr.log"
            else:
                log_file = trajectory_dir / "agent.jsonl"
                traj_json_name = "trajectory.json"
                stderr_name = "stderr.log"
            with open(log_file, "w") as f:
                if stdout:
                    f.write(stdout)
            if stderr:
                with open(trajectory_dir / stderr_name, "w") as f:
                    f.write(stderr)

            subprocess.run(["docker", "cp", f"{cid}:/agent_trajectory/.",
                            str(trajectory_dir)], capture_output=True)

            if log_file.exists():
                traj_json = trajectory_dir / traj_json_name
                convert_jsonl_to_trajectory(log_file, traj_json)

            if args.max_attempts > 1:
                poc_file = output_dir / f"poc_attempt_{attempt}.bin"
                patch_file = output_dir / f"fix_attempt_{attempt}.patch"
            else:
                poc_file = output_dir / "poc.bin"
                patch_file = output_dir / "fix.patch"

            subprocess.run(["docker", "cp", f"{cid}:/output/poc.bin", str(poc_file)],
                           capture_output=True)
            subprocess.run(["docker", "cp", f"{cid}:/output/fix.patch", str(patch_file)],
                           capture_output=True)

            # crash.log is the agent's evidence for what it found; it is
            # collected outside agent_output/ (see evidence_dir).  The
            # instruction asks the agent for it in both the source tree and
            # /output, so try both; a task that never asks for one collects
            # nothing and carries on.
            evidence_dir.mkdir(parents=True, exist_ok=True)
            crash_file = evidence_dir / (f"crash_attempt_{attempt}.log"
                                         if args.max_attempts > 1 else "crash.log")
            for src in ([f"{cid}:/output/crash.log"] +
                        ([f"{cid}:{repo_dir}/crash.log"] if repo_dir else [])):
                subprocess.run(["docker", "cp", src, str(crash_file)],
                               capture_output=True)
                if crash_file.exists():
                    break
            if crash_file.exists():
                print(f"  Collected crash.log ({crash_file.stat().st_size} bytes) -> "
                      f"{crash_file}")

            if not poc_file.exists():
                print("  No PoC generated!")
                all_attempts.append({
                    "attempt": attempt, "agent_exec_seconds": round(agent_time, 2),
                    "reward": 0.0, "success": False,
                    "stage1": "skipped:no_poc", "stage2": "skipped:no_poc",
                    "stage3": "skipped:no_poc", "stage4": "skipped:no_poc",
                    "skip_reason": "No poc.bin was generated by the agent",
                    "pytest_score": 0.0, "rubric_score": 0.0, "avg_score": 0.0,
                })
                print("  Running rubric judge for token capture (no PoC)...")
                record_judge_usage(run_dir, attempt, task_dir, log_file, llm_env, llm_model, args.max_attempts)
                if attempt < args.max_attempts:
                    feedback = "\n=== Previous Attempt Failed ===\nNo poc.bin was generated."
                continue

            if not patch_file.exists():
                print("  No patch generated!")
                all_attempts.append({
                    "attempt": attempt, "agent_exec_seconds": round(agent_time, 2),
                    "reward": 0.0, "success": False,
                    "stage1": "skipped:no_patch", "stage2": "skipped:no_patch",
                    "stage3": "skipped:no_patch", "stage4": "skipped:no_patch",
                    "skip_reason": "No fix.patch was generated by the agent",
                    "pytest_score": 0.0, "rubric_score": 0.0, "avg_score": 0.0,
                })
                print("  Running rubric judge for token capture (no patch)...")
                record_judge_usage(run_dir, attempt, task_dir, log_file, llm_env, llm_model, args.max_attempts)
                if attempt < args.max_attempts:
                    feedback = "\n=== Previous Attempt Failed ===\nNo fix.patch was generated."
                continue

            if args.max_attempts > 1:
                shutil.copy(poc_file, output_dir / "poc.bin")
                shutil.copy(patch_file, output_dir / "fix.patch")

            # Destroy agent container BEFORE grading to prevent state leakage.
            cleanup(cid)
            cid = None

            print(f"\n  Grading (attempt {attempt}) in fresh container...")
            reward, stages, test_results, ctrf, verifier_output = run_verifier(
                img_tag, task_dir, poc_file, patch_file,
                crash_path=crash_file if crash_file.exists() else None,
                repo_dir=repo_dir)

            agent_success = (stages.get("stage1") == "passed" and
                             stages.get("stage2") == "passed" and
                             stages.get("stage3") == "passed" and
                             stages.get("stage4") == "passed")
            gt_success = stages.get("stage4") == "passed"

            pytest_data = {
                "reward": reward,
                "stages": stages,
                "test_results": test_results,
                "ctrf": ctrf,
            }

            print(f"\n  Evaluating rubric (attempt {attempt})...")
            traj_text = ""
            if log_file.exists():
                traj_text = log_file.read_text(errors="replace")
            rubric_data = evaluate_rubric(task_dir, traj_text, llm_env, llm_model)

            print(f"\n  Judge calibration check (attempt {attempt})...")
            calibration_data = evaluate_judge_calibration(task_dir, traj_text, test_results, llm_env, llm_model)
            if calibration_data:
                attempt_dir = run_dir / f"attempt_{attempt}" if args.max_attempts > 1 else run_dir
                cal_dir = attempt_dir / "verifier"
                cal_dir.mkdir(parents=True, exist_ok=True)
                json.dump(calibration_data, open(cal_dir / "calibration.json", "w"), indent=2)

            avg_score = save_attempt_scores(run_dir, attempt, pytest_data, rubric_data, args.max_attempts, verifier_output, test_weights_data)

            attempt_result = {
                "attempt": attempt,
                "agent_exec_seconds": round(agent_time, 2),
                "stage1": stages.get("stage1"),
                "stage2": stages.get("stage2"),
                "stage3": stages.get("stage3"),
                "stage4": stages.get("stage4"),
                "agent_success": agent_success,
                "gt_success": gt_success,
                "success": agent_success,
                "pytest_score": reward,
                "rubric_score": rubric_data.get("rubric_score", 0.0) if rubric_data else 0.0,
                "avg_score": avg_score,
                "reward": avg_score,
                "test_results": test_results,
            }
            all_attempts.append(attempt_result)

            if avg_score > best_reward:
                best_reward = avg_score

            if agent_success:
                print(f"\n*** SUCCESS on attempt {attempt}! (avg_score={avg_score:+.4f}) ***")
                final_status = "success"
                break
            else:
                feedback = format_feedback(stages, attempt, str(poc_file), str(patch_file))
                print(feedback)
                if attempt >= args.max_attempts:
                    break

        except Exception as e:
            print(f"  Error: {e}")
            import traceback
            traceback.print_exc()
            all_attempts.append({
                "attempt": attempt, "agent_exec_seconds": 0,
                "reward": 0.0, "success": False,
                "stage1": "error", "stage2": "error",
                "stage3": "error", "stage4": "error",
                "skip_reason": f"Exception: {e}",
                "pytest_score": 0.0, "rubric_score": 0.0, "avg_score": 0.0,
            })
            if attempt >= args.max_attempts:
                break
        finally:
            cleanup(cid)

    duration = time.time() - start_time

    reward_dir = run_dir / "verifier"
    reward_dir.mkdir(exist_ok=True)
    best = next((a for a in reversed(all_attempts) if a.get("success")), all_attempts[-1] if all_attempts else {})

    final_pytest = best.get("pytest_score", best.get("reward", 0.0))
    final_rubric = best.get("rubric_score", 0.0)
    final_avg = best.get("avg_score", final_pytest)

    # reward.txt (Harbor standard)
    (reward_dir / "reward.txt").write_text(str(round(final_avg, 6)))

    # Load rubric criteria from best attempt if available
    best_rubric_detail = None
    if args.max_attempts > 1:
        best_rubric_attempt_path = reward_dir / f"attempt_{best.get('attempt', 1)}" / "rubric_score.json"
    else:
        best_rubric_attempt_path = reward_dir / "rubric_score.json"
    if best_rubric_attempt_path.exists():
        try:
            best_rubric_detail = json.load(open(best_rubric_attempt_path))
        except Exception:
            pass


    summary = {
        "task": task_name,
        "agent": "claude-code",
        "prompt_style": "iterative",
        "mode": "e2e",
        "max_attempts": args.max_attempts,
        "timeout": args.timeout,
        "status": final_status,
        "reward": round(final_avg, 6),
        "pytest_score": round(final_pytest, 6),
        "rubric_score": round(final_rubric, 6),
        "avg_score": round(final_avg, 6),
        "best_reward": best_reward,
        "stages": {
            s: {"status": best.get(s)}
            for s in ["stage1", "stage2", "stage3", "stage4"]
        },
        "agent_success": best.get("agent_success", False),
        "found_ground_truth_bug": best.get("gt_success", False),
        "skip_reason": best.get("skip_reason"),
        "attempts": all_attempts,
        "test_weights": test_weights_data,
        "test_results": best.get("test_results", {}),
        "rubric_detail": best_rubric_detail,
        "duration_seconds": duration,
        "duration_minutes": round(duration / 60, 2),
        "output_dir": str(run_dir.absolute()),
        "model": llm_model,
        "harbor_task": str(task_dir),
    }
    json.dump(summary, open(run_dir / "summary.json", "w"), indent=2)

    # --- Finance API: post usage (opt-in, fully isolated) ---
    if getattr(args, 'finance_api_url', None):
        try:
            scripts_dir_fin = Path(__file__).parent / "scripts"
            sys.path.insert(0, str(scripts_dir_fin))
            from finance_client import post_run_usage
            post_run_usage(
                finance_url=args.finance_api_url,
                run_dir=run_dir,
                task_name=task_name,
                timestamp=timestamp,
                model_name=llm_model,
                project_id=getattr(args, 'finance_project_id', None) or "kakashi",
                project_type=getattr(args, 'finance_project_type', None) or "technical",
                team_type=getattr(args, 'finance_team_type', "Projects"),
                budget_type=getattr(args, 'finance_budget_type', "Production"),
                rfp_sub_type=getattr(args, 'finance_rfp_sub_type', ""),
                production_mode=getattr(args, 'finance_production_mode', "Singlephase"),
                subscription_id=getattr(args, 'finance_subscription_id', ""),
            )
        except Exception as e:
            print(f"  [finance] Warning: usage tracking failed: {e}")

    print(f"\n{'=' * 60}")
    print(f"Task: {task_name}")
    print(f"Status: {final_status.upper()}")
    print(f"Pytest Score:  {final_pytest:+.4f}")
    print(f"Rubric Score:  {final_rubric:+.4f}")
    print(f"Avg Score:     {final_avg:+.4f}")
    print(f"Duration: {summary['duration_minutes']:.2f} minutes")
    for att in all_attempts:
        stages_str = []
        for s in ["stage1", "stage2", "stage3", "stage4"]:
            v = att.get(s)
            if v:
                stages_str.append(f"S{s[-1]}:{v}")
        result = "SUCCESS (all stages passed)" if att.get("success") else "FAILED"
        ps = att.get("pytest_score", att.get("reward", 0.0))
        rs = att.get("rubric_score", 0.0)
        av = att.get("avg_score", ps)
        print(f"  Attempt {att['attempt']}: {' | '.join(stages_str)} -> {result} (pytest={ps:+.4f} rubric={rs:+.4f} avg={av:+.4f})")
    print(f"Output: {run_dir.absolute()}")
    print(f"{'=' * 60}")

    sys.exit(0 if final_status == "success" else 1)


if __name__ == "__main__":
    main()
