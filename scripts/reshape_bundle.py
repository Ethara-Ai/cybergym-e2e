#!/usr/bin/env python3
"""Reshape a kakashi run directory into a per-model delivery bundle.

Disk-driven: reconstructs from the per-attempt files on disk, so it works on a
COMPLETE run *and* on a run that was killed partway (no summary.json needed).
Each kakashi attempt maps to a run (attempt_N -> runN); a --pass-at-k 8 run that
was stopped after 5 attempts yields run1..run5 and a pass@5 pass_summary.

    <out>/<task>/
      data/                         (copied from tasks/<task>: the task definition)
      trajectories/<model>/
        pass_summary.json           (rollup over the completed attempts)
        run1/ run2/ ...
          config.json  result.json
          agent/{trajectory.json, agent.jsonl}
          verifier/{score.json, reward.json, ctrf.json, usage.json}
          artifacts/{manifest.json, app/workspace/}

Usage:
    python3 scripts/reshape_bundle.py <run_dir> [--out delivery] [--task-dir tasks/<task>]
"""
import argparse
import json
import re
import shutil
from pathlib import Path


def _load(p, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return default


def _dump(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _copy(src, dst):
    src, dst = Path(src), Path(dst)
    if src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    return False


def _pass_at_k(n, c, k):
    from math import comb
    if n <= 0 or k > n:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def _discover_attempts(run_dir: Path):
    """(index, trajectory.json, agent.jsonl, verifier_dir) per attempt, from disk.
    Handles multi-attempt (attempt_N) and single-attempt runs."""
    vroot = run_dir / "verifier"
    troot = run_dir / "trajectory"
    multi = sorted(vroot.glob("attempt_*")) if vroot.is_dir() else []
    out = []
    if multi:
        for vdir in sorted(multi, key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
            i = int(re.sub(r"\D", "", vdir.name) or 0)
            out.append((i,
                        troot / f"trajectory_attempt_{i}.json",
                        troot / f"attempt_{i}.jsonl",
                        vdir))
    elif (troot / "trajectory.json").exists() or (vroot / "reward.json").exists():
        out.append((1, troot / "trajectory.json", troot / "agent.jsonl", vroot))
    return out


def _usage_from(traj_json: dict, rubric: dict) -> dict:
    """Per-run usage record in the reference bundle shape: combined top-level
    totals + a `sources` split (agent / judge), the judge broken down
    `per_member` (kakashi runs a single judge seat, exposed as `primary`), plus
    accounting `notes`. All values are derived from the run's own artifacts
    (`trajectory.json` final_metrics + `rubric_score.json` judge_usage)."""
    fm = (traj_json or {}).get("final_metrics", {}) or {}
    ju = (rubric or {}).get("judge_usage", {}) or {}

    def _i(v):
        return int(v or 0)

    agent = {
        "input_tokens": _i(fm.get("total_prompt_tokens")),
        "output_tokens": _i(fm.get("total_completion_tokens")),
        "cache_read_tokens": _i(fm.get("total_cached_tokens")),
        "cache_write_tokens": _i(fm.get("total_cache_creation_tokens")),
        "cost_usd": round(float(fm.get("total_cost_usd") or 0.0), 6),
    }
    judge_core = {
        "input_tokens": _i(ju.get("input_tokens")),
        "output_tokens": _i(ju.get("output_tokens")),
        "cache_read_tokens": _i(ju.get("cache_read_input_tokens")),
        "cache_write_tokens": _i(ju.get("cache_creation_input_tokens")),
        "request_count": _i((rubric or {}).get("trials_succeeded")),
        "cost_usd": round(float(ju.get("cost_usd") or 0.0), 6),
    }
    judge = dict(judge_core)
    judge["cost_known"] = bool(ju.get("cost_known"))
    # Single judge seat -> one 'primary' member, so the shape matches the
    # reference's multi-member council.
    judge["per_member"] = {"primary": {"model": (rubric or {}).get("judge_model"), **judge_core}}
    # Per-trial audit breakdown (each usable trial's tokens/cost; sums to the total).
    judge["per_trial"] = ju.get("per_trial", [])

    combined = {
        "input_tokens": agent["input_tokens"] + judge["input_tokens"],
        "output_tokens": agent["output_tokens"] + judge["output_tokens"],
        "cache_read_tokens": agent["cache_read_tokens"] + judge["cache_read_tokens"],
        "cache_write_tokens": agent["cache_write_tokens"] + judge["cache_write_tokens"],
        "cost_usd": round(agent["cost_usd"] + judge["cost_usd"], 6),
    }
    return {
        **combined,
        "sources": {"agent": agent, "judge": judge},
        "notes": [
            "agent.input_tokens is the full prompt size (includes cache_read_tokens + "
            "cache_write_tokens), so combined top-level counts agent cache inside input_tokens "
            "as well as in the cache_* fields.",
            "judge runs on the codex path (gpt-5.6-sol), which reports no prompt-cache tokens, "
            "so judge.cache_* are 0.",
            "judge.request_count is the number of usable rubric trials (adaptive: 3-11).",
        ],
    }


def reshape(run_dir: Path, out_root: Path, task_dir: Path | None) -> Path:
    run_dir = Path(run_dir).resolve()
    summary = _load(run_dir / "summary.json", {}) or {}

    # task/model from summary if present, else from the run dir path
    #   agent_output/<task>/<model>/<ts>/
    task = summary.get("task") or run_dir.parent.parent.name
    model = summary.get("model") or run_dir.parent.name
    complete = bool(summary)  # a full run wrote summary.json

    bundle = out_root / task
    model_dir = bundle / "trajectories" / model
    model_dir.mkdir(parents=True, exist_ok=True)

    # data/ : the task definition
    if task_dir and Path(task_dir).is_dir():
        data_dst = bundle / "data"
        if not data_dst.exists():
            shutil.copytree(task_dir, data_dst, dirs_exist_ok=True)
        for extra in ("TRUTH.md", "GROUND_TRUTH.md"):
            _copy(Path(task_dir) / extra, bundle / extra)
        # Fallback: if the task ships no TRUTH.md, derive one from its ground truth.
        if not (bundle / "TRUTH.md").exists():
            try:
                from gen_truth import generate as _gen_truth
                (bundle / "TRUTH.md").write_text(_gen_truth(Path(task_dir)), encoding="utf-8")
            except Exception:
                pass

    attempts = _discover_attempts(run_dir)
    run_rewards, solved = [], 0

    for i, traj_p, jsonl_p, vdir in attempts:
        reward_json = _load(vdir / "reward.json", {}) or {}
        rubric = _load(vdir / "rubric_score.json", {}) or {}
        traj_json = _load(traj_p, {}) or {}

        stages = reward_json.get("binary_stages", {}) or {}
        agent_success = bool(stages) and all(stages.values())
        solved += 1 if agent_success else 0
        run_rewards.append(reward_json.get("avg_score"))

        run_out = model_dir / f"run{i}"
        (run_out / "agent").mkdir(parents=True, exist_ok=True)
        (run_out / "verifier").mkdir(parents=True, exist_ok=True)
        (run_out / "artifacts" / "app" / "workspace").mkdir(parents=True, exist_ok=True)

        _dump(run_out / "config.json", {
            "task": task, "model": model,
            "model_provider": summary.get("model_provider"),
            "agent": summary.get("agent", "claude-code"),
            "mode": summary.get("mode"),
            "run_index": i,
        })
        _dump(run_out / "result.json", {
            "task": task, "model": model, "run": i,
            "status": reward_json.get("status") or ("success" if agent_success else "failed"),
            "reward": reward_json.get("reward"),
            "avg_score": reward_json.get("avg_score"),
            "pytest_score": reward_json.get("pytest_score"),
            "rubric_score": reward_json.get("rubric_score"),
            "agent_success": agent_success,
            "stages": stages,
            "judge_available": reward_json.get("judge_available"),
            "skip_reason": reward_json.get("skip_reason"),
        })

        _copy(traj_p, run_out / "agent" / "trajectory.json")
        _copy(jsonl_p, run_out / "agent" / "agent.jsonl")

        _copy(vdir / "reward.json", run_out / "verifier" / "reward.json")
        _copy(vdir / "ctrf.json", run_out / "verifier" / "ctrf.json")
        _copy(vdir / "test-stdout.txt", run_out / "verifier" / "test-stdout.txt")
        _dump(run_out / "verifier" / "usage.json", _usage_from(traj_json, rubric))
        _dump(run_out / "verifier" / "score.json", {
            "reward": reward_json.get("reward"),
            "avg_score": reward_json.get("avg_score"),
            "pytest_score": reward_json.get("pytest_score"),
            "rubric_score": reward_json.get("rubric_score"),
            "judge_available": reward_json.get("judge_available"),
            "rubric_detail": rubric,
        })

        # artifacts: kakashi keeps a single /output (last attempt's); copy best-effort
        collected, skipped = [], []
        for art in ("poc.bin", "fix.patch"):
            if _copy(run_dir / "output" / art, run_out / "artifacts" / "app" / "workspace" / art):
                collected.append(art)
            else:
                skipped.append(art)
        _dump(run_out / "artifacts" / "manifest.json", {
            "collected": collected, "skipped": skipped,
            "note": ("kakashi keeps a single /output overwritten each attempt, so these "
                     "artifacts are the run's final poc.bin/fix.patch, not per-attempt"),
        })

    # pass_summary over the completed attempts
    n = len(attempts)
    c = solved
    graded = [r for r in run_rewards if isinstance(r, (int, float))]
    pass_summary = {
        "task": task, "model": model,
        "runs_completed": n,
        "complete": complete,
        "note": None if complete else "PARTIAL run (stopped before finishing); pass@k is over completed attempts only",
        "c_solved": c,
        "rewards": run_rewards,
        "mean_reward": round(sum(graded) / len(graded), 6) if graded else None,
        "pass@k": {str(k): round(_pass_at_k(n, c, k), 6) for k in range(1, n + 1)},
        "pass_criterion": "agent_success (all required stages passed)",
    }
    _dump(model_dir / "pass_summary.json", pass_summary)
    return bundle


def main():
    ap = argparse.ArgumentParser(description="Reshape a kakashi run dir into a per-model delivery bundle (partial-run safe)")
    ap.add_argument("run_dir")
    ap.add_argument("--out", default="delivery")
    ap.add_argument("--task-dir", default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    task_dir = args.task_dir
    if task_dir is None:
        task = (_load(run_dir / "summary.json", {}) or {}).get("task") or run_dir.parent.parent.name
        cand = Path(__file__).resolve().parent.parent / "tasks" / task
        if cand.is_dir():
            task_dir = cand
    bundle = reshape(run_dir, Path(args.out), Path(task_dir) if task_dir else None)
    print(f"  delivery bundle written: {bundle}")


if __name__ == "__main__":
    main()
