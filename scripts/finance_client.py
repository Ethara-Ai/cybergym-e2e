"""
finance_client.py — Post trajectory usage to the Ethara Finance API (Odoo).

This module is opt-in and fully isolated from the scoring pipeline.
It reads already-written output files (summary.json, trajectory JSON)
and POSTs usage data to the Finance API.  A failure here never affects
the harness exit code or scoring results.

Usage from run_harbor.py:
    from scripts.finance_client import post_run_usage
    post_run_usage(args, run_dir, task_name, timestamp, model_name)

All network calls have a 5-second timeout.  No external dependencies
beyond httpx (optional) or stdlib urllib.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _http_post(url, payload, timeout=5):
    """POST JSON to url using httpx (if available) or urllib.  Returns (ok, detail)."""
    headers = {"Content-Type": "application/json"}
    data = json.dumps(payload).encode()

    try:
        import httpx
        r = httpx.post(url, content=data, headers=headers, timeout=timeout)
        return r.status_code < 400, f"status={r.status_code}"
    except ImportError:
        pass

    import urllib.request
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400, f"status={resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)


def _read_trajectory_metrics(run_dir):
    """Read aggregated token metrics from trajectory JSON files in run_dir."""
    total_input = 0
    total_output = 0
    total_cache_input = 0

    traj_dir = run_dir / "trajectory"
    if not traj_dir.exists():
        return total_input, total_output, total_cache_input

    for f in sorted(traj_dir.glob("trajectory_attempt_*.json")):
        try:
            data = json.loads(f.read_text())
            metrics = data.get("final_metrics", {})
            total_input += metrics.get("total_prompt_tokens", 0)
            total_output += metrics.get("total_completion_tokens", 0)
            total_cache_input += metrics.get("total_cached_tokens", 0)
        except Exception:
            continue

    return total_input, total_output, total_cache_input


def _read_judge_usage(run_dir):
    """Read judge model usage from rubric_score.json files across attempts."""
    judge_input = 0
    judge_output = 0
    judge_cache_input = 0
    judge_model = ""

    verifier_dir = run_dir / "verifier"
    if not verifier_dir.exists():
        return judge_model, judge_input, judge_output, judge_cache_input

    for attempt_dir in sorted(verifier_dir.glob("attempt_*")):
        rubric_path = attempt_dir / "rubric_score.json"
        if not rubric_path.exists():
            continue
        try:
            data = json.loads(rubric_path.read_text())
            usage = data.get("judge_usage", {})
            judge_input += usage.get("input_tokens", 0)
            judge_output += usage.get("output_tokens", 0)
            judge_cache_input += usage.get("cache_read_input_tokens", 0)
            if not judge_model:
                judge_model = data.get("judge_model", "")
        except Exception:
            continue

    return judge_model, judge_input, judge_output, judge_cache_input


def post_run_usage(finance_url, run_dir, task_name, timestamp, model_name,
                   project_id="", project_type="CyberGym-E2E",
                   team_type="Projects", budget_type="Production",
                   rfp_sub_type="", production_mode="Singlephase",
                   subscription_id=""):
    """Post trajectory usage to the Finance API.

    This function is fully self-contained.  It reads from disk (summary.json,
    trajectory JSONs, rubric JSONs) and POSTs to the Finance API.
    Returns True on success, False on failure.  Never raises.
    """
    if not finance_url:
        return False

    try:
        run_dir = Path(run_dir)

        # Read trajectory token metrics
        traj_input, traj_output, traj_cache_input = _read_trajectory_metrics(run_dir)

        # Read judge usage
        judge_model, judge_input, judge_output, judge_cache_input = _read_judge_usage(run_dir)

        # Build ISO 8601 timestamp
        try:
            generated_at = datetime.now(timezone.utc).isoformat()
        except Exception:
            generated_at = timestamp

        # Determine phase flag
        is_phase_based = (budget_type == "Production" and production_mode == "Multiphase")

        # Build the payload per Finance API spec
        payload = {
            "project_id": project_id or task_name,
            "project_type": project_type,
            "task_id": task_name,
            "trajectory_id": f"{task_name}_{timestamp}",
            "team_type": team_type,
            "budget_type": budget_type,
            "rfp_sub_type": rfp_sub_type if budget_type == "RFP" else "",
            "production_mode": production_mode if budget_type == "Production" else "",
            "is_phase_based": is_phase_based,
            "generated_at": generated_at,
            "model_name": model_name,
            "trajectory_input_tokens": traj_input,
            "trajectory_output_tokens": traj_output,
            "trajectory_input_cache_tokens": traj_cache_input,
            "trajectory_output_cache_tokens": 0,
            "trajectory_cost_usd": 0.0,
            "subscription_id": subscription_id,
            "judge_lines": [],
        }

        # Add judge line if we have judge usage data
        if judge_input > 0 or judge_output > 0:
            payload["judge_lines"].append({
                "model_name": judge_model or "claude-sonnet-4-20250514",
                "judge_input_tokens": judge_input,
                "judge_output_tokens": judge_output,
                "judge_input_cache_tokens": judge_cache_input,
                "judge_output_cache_tokens": 0,
                "judge_cost_usd": 0.0,
            })

        # Build the full endpoint URL
        url = finance_url.rstrip("/")
        if not url.endswith("/trajectory_usage/create"):
            url = f"{url}/ethara_project/trajectory_usage/create"

        ok, detail = _http_post(url, payload, timeout=5)
        if ok:
            print(f"  [finance] Usage posted successfully ({detail})")
        else:
            print(f"  [finance] Warning: POST failed ({detail})")
        return ok

    except Exception as e:
        print(f"  [finance] Warning: usage tracking failed: {e}")
        return False
