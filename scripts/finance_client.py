"""
finance_client.py — Post trajectory usage to the Ethara Finance API (Odoo).

This module is opt-in and fully isolated from the scoring pipeline.
It reads already-written output files (summary.json, trajectory JSON)
and POSTs usage data to the Finance API.  A failure here never affects
the harness exit code or scoring results.

Usage from run_harbor.py:
    from finance_client import post_run_usage
    post_run_usage(finance_url, run_dir, task_name, timestamp, model_name, ...)

All network calls have a 5-second timeout.  No external dependencies
beyond httpx (optional) or stdlib urllib.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# --- Pricing -----------------------------------------------------------------
# USD per 1M tokens, (input, output).  Anthropic first-party list prices.
# Override or extend without touching this file via FINANCE_PRICING_JSON, e.g.
#   FINANCE_PRICING_JSON={"claude-opus-4-8":[5,25],"my-model":[1.5,7.5]}
# The pricing table lives in scripts/judge_lib.py (MODEL_PRICING) so the
# judge's cost line and the Finance API's judge_cost_usd can never disagree.
# FINANCE_PRICING_JSON still overrides per model with (input, output) pairs;
# cache rates for an override are derived with the standard multipliers.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge_lib as _judge_lib  # noqa: E402

# Cache multipliers applied to the INPUT rate for FINANCE_PRICING_JSON overrides.
_CACHE_READ_MULT = 0.1     # cache_read_input_tokens
_CACHE_WRITE_MULT = 1.25   # cache_creation_input_tokens, 5-minute TTL

_warned_models = set()


def _override_table():
    """FINANCE_PRICING_JSON entries as full 4-rate dicts."""
    table = {}
    raw = os.environ.get("FINANCE_PRICING_JSON", "").strip()
    if raw:
        try:
            for name, pair in json.loads(raw).items():
                rate_in, rate_out = float(pair[0]), float(pair[1])
                table[name] = {"input": rate_in, "output": rate_out,
                               "cache_write": rate_in * _CACHE_WRITE_MULT,
                               "cache_read": rate_in * _CACHE_READ_MULT}
        except Exception as e:
            print(f"  [finance] Warning: ignoring bad FINANCE_PRICING_JSON ({e})")
    return table


def _rates_for(model_name):
    """Per-1M-token rate dict for the model, or None if it is unpriced."""
    if not model_name:
        return None
    overrides = _override_table()
    if model_name in overrides:
        return overrides[model_name]
    low = model_name.lower()
    for key, rates in overrides.items():      # dated snapshot of an override
        if low.startswith(key.lower()):
            return rates
    return _judge_lib.pricing_for(model_name)


def _cost_usd(model_name, input_tokens, output_tokens, cache_read, cache_write):
    """Cost in USD.  Returns 0.0 when costing is off or the model is unpriced.

    cache_read / cache_write tokens are billed separately from input_tokens --
    the Anthropic usage block does not fold them into input_tokens.
    """
    if os.environ.get("FINANCE_COST_MODE", "list").strip().lower() == "zero":
        return 0.0
    rates = _rates_for(model_name)
    if rates is None:
        if model_name not in _warned_models:
            _warned_models.add(model_name)
            print(f"  [finance] Warning: no price for model {model_name!r}; "
                  f"cost reported as 0.0 (set FINANCE_PRICING_JSON to fix)")
        return 0.0
    total = (
        (input_tokens or 0) * rates["input"]
        + (output_tokens or 0) * rates["output"]
        + (cache_read or 0) * rates["cache_read"]
        + (cache_write or 0) * rates["cache_write"]
    ) / 1_000_000.0
    return round(total, 6)


def _auth_headers():
    """Build auth headers for the Finance API from the environment.

    FINANCE_API_TOKEN         the secret (no token -> no auth header sent)
    FINANCE_API_AUTH_HEADER   header name        (default: Authorization)
    FINANCE_API_AUTH_SCHEME   value prefix       (default: Bearer; set empty
                              for raw-token headers such as X-API-Key)
    """
    token = os.environ.get("FINANCE_API_TOKEN", "").strip()
    if not token:
        return {}
    name = os.environ.get("FINANCE_API_AUTH_HEADER", "Authorization").strip() or "Authorization"
    scheme = os.environ.get("FINANCE_API_AUTH_SCHEME", "Bearer").strip()
    return {name: f"{scheme} {token}" if scheme else token}


def _http_post(url, payload, timeout=5):
    """POST JSON to url using httpx (if available) or urllib.  Returns (ok, detail)."""
    headers = {"Content-Type": "application/json"}
    headers.update(_auth_headers())
    data = json.dumps(payload).encode()

    try:
        import httpx
    except ImportError:
        httpx = None

    if httpx is not None:
        try:
            r = httpx.post(url, content=data, headers=headers, timeout=timeout)
            return r.status_code < 400, f"status={r.status_code}"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

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
    """Read aggregated token metrics from trajectory JSON files in run_dir.

    ``total_cache_creation_tokens`` is absent from trajectories written before
    cache-write tracking was added, so it defaults to 0 for older runs.
    """
    total_input = 0
    total_output = 0
    total_cache_input = 0
    total_cache_write = 0

    traj_dir = run_dir / "trajectory"
    if not traj_dir.exists():
        return total_input, total_output, total_cache_input, total_cache_write

    for pattern in ["trajectory_attempt_*.json", "trajectory.json"]:
        for f in sorted(traj_dir.glob(pattern)):
            try:
                data = json.loads(f.read_text())
                metrics = data.get("final_metrics", {})
                total_input += metrics.get("total_prompt_tokens", 0)
                total_output += metrics.get("total_completion_tokens", 0)
                total_cache_input += metrics.get("total_cached_tokens", 0)
                total_cache_write += metrics.get("total_cache_creation_tokens", 0) or 0
            except Exception:
                continue

    return total_input, total_output, total_cache_input, total_cache_write


def _read_judge_usage(run_dir):
    """Read judge model usage from rubric_score.json files across attempts."""
    judge_input = 0
    judge_output = 0
    judge_cache_input = 0
    judge_cache_write = 0
    judge_model = ""

    verifier_dir = run_dir / "verifier"
    if not verifier_dir.exists():
        return judge_model, judge_input, judge_output, judge_cache_input, judge_cache_write

    rubric_paths = sorted(verifier_dir.glob("attempt_*/rubric_score.json"))
    top_level = verifier_dir / "rubric_score.json"
    if top_level.exists():
        rubric_paths.append(top_level)

    for rubric_path in rubric_paths:
        try:
            data = json.loads(rubric_path.read_text())
            usage = data.get("judge_usage", {})
            judge_input += usage.get("input_tokens", 0)
            judge_output += usage.get("output_tokens", 0)
            judge_cache_input += usage.get("cache_read_input_tokens", 0)
            judge_cache_write += usage.get("cache_creation_input_tokens", 0) or 0
            if not judge_model:
                judge_model = data.get("judge_model", "")
        except Exception:
            continue

    return judge_model, judge_input, judge_output, judge_cache_input, judge_cache_write


def post_run_usage(finance_url, run_dir, task_name, timestamp, model_name,
                   project_id="kakashi", project_type="technical",
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
        (traj_input, traj_output, traj_cache_input,
         traj_cache_write) = _read_trajectory_metrics(run_dir)

        # Read judge usage
        (judge_model, judge_input, judge_output,
         judge_cache_input, judge_cache_write) = _read_judge_usage(run_dir)

        # Build ISO 8601 timestamp
        try:
            generated_at = datetime.now(timezone.utc).isoformat()
        except Exception:
            generated_at = timestamp

        # Determine phase flag
        is_phase_based = (budget_type == "Production" and production_mode == "Multiphase")

        # Build the payload per Finance API spec
        payload = {
            "project_id": project_id or "kakashi",
            "project_type": project_type or "technical",
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
            "trajectory_output_cache_tokens": traj_cache_write,
            "trajectory_cost_usd": _cost_usd(
                model_name, traj_input, traj_output, traj_cache_input, traj_cache_write),
            "subscription_id": subscription_id,
            "judge_lines": [],
        }

        # Add judge line if we have judge usage data
        if judge_input > 0 or judge_output > 0 or judge_cache_write > 0:
            judge_model = judge_model or "claude-sonnet-4-20250514"
            payload["judge_lines"].append({
                "model_name": judge_model,
                "judge_input_tokens": judge_input,
                "judge_output_tokens": judge_output,
                "judge_input_cache_tokens": judge_cache_input,
                "judge_output_cache_tokens": judge_cache_write,
                "judge_cost_usd": _cost_usd(
                    judge_model, judge_input, judge_output,
                    judge_cache_input, judge_cache_write),
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
