#!/usr/bin/env python3
"""judge.py -- run the rubric judge on its own, apart from a task run.

run_harbor.py judges as the last step of a run, which means re-judging costs a
full agent execution.  This runs the same evaluate_rubric() over a trajectory
that already exists on disk, so a rubric can be revised, a judge model swapped,
or a disputed score re-checked for the price of the judge calls alone.

Usage:
    # a completed run directory (finds its own task and trajectory)
    python scripts/judge.py agent_output/OSV-2026-1015/20260821_120733_e2e

    # explicit task + trajectory
    python scripts/judge.py --task tasks/OSV-2026-1015 --trajectory path/to/agent.jsonl

    # try a different judge without touching .env, and write the result elsewhere
    JUDGE_MODEL=claude-sonnet-4-6 python scripts/judge.py <run_dir> -o /tmp/rubric.json

Judge model and trial count come from the environment (JUDGE_MODEL,
JUDGE_TRIALS, JUDGE_MIN_TRIALS), read from .env when not already set; each
falls back to its default when missing or blank.  Writes rubric_score.json and
never modifies the run's existing scores unless you point -o at them.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_run_harbor():
    """Import the judge library (scripts/judge_lib.py).

    Named for history: this used to reach into run_harbor.py for evaluate_rubric.
    The judge now lives in its own module, so the runner is not imported at all.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import judge_lib
    return judge_lib


def resolve_inputs(args, rh):
    """Work out (task_dir, trajectory_path) from whatever the caller supplied."""
    task_dir = Path(args.task) if args.task else None
    traj = Path(args.trajectory) if args.trajectory else None

    if args.run_dir:
        run_dir = Path(args.run_dir)
        if not run_dir.is_dir():
            sys.exit(f"ERROR: {run_dir} is not a directory")
        if traj is None:
            # Prefer the raw agent log; the converted trajectory.json is a
            # summary and reads less well as judge input.
            for candidate in ("trajectory/agent.jsonl", "trajectory/attempt_1.jsonl",
                              "trajectory/trajectory.json"):
                if (run_dir / candidate).is_file():
                    traj = run_dir / candidate
                    break
        if task_dir is None:
            summary = run_dir / "summary.json"
            if summary.is_file():
                try:
                    name = json.loads(summary.read_text()).get("task")
                    if name and (REPO_ROOT / "tasks" / name).is_dir():
                        task_dir = REPO_ROOT / "tasks" / name
                except (OSError, json.JSONDecodeError):
                    pass
            if task_dir is None:
                # agent_output/<task>/<timestamp>/ -- the parent names the task.
                guess = REPO_ROOT / "tasks" / run_dir.parent.name
                if guess.is_dir():
                    task_dir = guess

    if task_dir is None:
        sys.exit("ERROR: could not determine the task; pass --task tasks/<name>")
    if traj is None:
        sys.exit("ERROR: could not determine the trajectory; pass --trajectory <file>")
    if not (task_dir / "tests" / "rubric.json").is_file():
        sys.exit(f"ERROR: {task_dir}/tests/rubric.json not found — nothing to judge against")
    if not traj.is_file():
        sys.exit(f"ERROR: trajectory {traj} not found")
    return task_dir, traj


def main():
    ap = argparse.ArgumentParser(
        description="Run the rubric judge over an existing trajectory.")
    ap.add_argument("run_dir", nargs="?",
                    help="Completed run directory (task and trajectory are inferred from it).")
    ap.add_argument("--task", help="Task directory, e.g. tasks/OSV-2026-1015.")
    ap.add_argument("--trajectory", help="Trajectory file to judge (agent.jsonl).")
    ap.add_argument("-o", "--output",
                    help="Where to write rubric_score.json "
                         "(default: <run_dir>/verifier/rubric_score.json, else ./rubric_score.json).")
    ap.add_argument("--print-criteria", action="store_true",
                    help="Print the per-criterion verdicts as well as the score.")
    args = ap.parse_args()

    if not args.run_dir and not (args.task and args.trajectory):
        ap.error("give a run directory, or both --task and --trajectory")

    rh = _load_run_harbor()
    rh.load_dotenv()          # same .env the runner uses; real env still wins

    task_dir, traj_path = resolve_inputs(args, rh)
    provider = rh.env_default("JUDGE_PROVIDER", rh.DEFAULT_JUDGE_PROVIDER).lower()
    judge_model = rh.judge_model_for(provider)
    trials = rh.env_default_int("JUDGE_TRIALS", rh.DEFAULT_JUDGE_TRIALS)

    print(f"Task:       {task_dir}")
    print(f"Trajectory: {traj_path} ({traj_path.stat().st_size} bytes)")
    print(f"Judge:      {provider}:{judge_model}  ({trials} trials)")

    traj_text = traj_path.read_text(errors="replace")
    rubric_data = rh.evaluate_rubric(task_dir, traj_text, {}, judge_model)
    if not rubric_data:
        sys.exit("ERROR: rubric evaluation failed (too few trials succeeded)")

    if args.output:
        out = Path(args.output)
    elif args.run_dir:
        out = Path(args.run_dir) / "verifier" / "rubric_score.json"
    else:
        out = Path("rubric_score.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rubric_data, indent=2))

    print(f"\nrubric_score = {rubric_data['rubric_score']:.6f} "
          f"({rubric_data['earned']}/{rubric_data['total_positive']})")
    print(f"judged by:    {rubric_data.get('judge_provider')}:{rubric_data.get('judge_model')}")
    if rubric_data.get("judge_anomalies"):
        print(f"judge anomalies: {rubric_data['judge_anomalies']}")
    print(f"judge cost:   ${rubric_data.get('judge_usage', {}).get('cost_usd', 0.0):.4f}")
    print(f"written to:   {out}")

    if args.print_criteria:
        print()
        for num, c in rubric_data["criteria"].items():
            mark = "PASS" if c["met"] else "----"
            print(f"  {mark} {num:4s} {c['score']:+g}/{c['max_score']:+g}  {c['criterion'][:72]}")


if __name__ == "__main__":
    main()
