#!/usr/bin/env python3
"""Propagate a re-judged rubric_score.json into every derived verifier file.

`scripts/judge.py` writes ONLY `rubric_score.json`. The reward files and
`calibration.json` are produced by `run_harbor.py` and go stale after a rejudge.
This script re-derives them from the current `rubric_score.json`, using the same
formula and JSON shapes as `run_harbor.py` (avg = (pytest + rubric) / 2), and
regenerates `calibration.json` with the active judge provider.

Usage:
    # 1. rejudge (writes verifier/rubric_score.json)
    JUDGE_PROVIDER=codex python scripts/judge.py <run_dir>
    # 2. propagate into avg_score/reward/reward.txt/calibration/summary
    python scripts/rejudge_sync.py <run_dir>

It never re-runs the agent and never re-runs the verifier (pytest_score and the
stage results are read from the existing files, unchanged by a rejudge).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))
import judge_lib  # noqa: E402

STAGE_KEYS = ["stage1", "stage2", "stage3", "stage4"]
CAL_RETRIES = 4  # codex intermittently truncates; a few tries absorbs it


def _load(p: Path):
    return json.loads(p.read_text())


def resolve_task_dir(run_dir: Path, summary: dict | None) -> Path | None:
    """tasks/<name>: from summary.json's `task`, else the run-dir grandparent."""
    name = (summary or {}).get("task")
    if name:
        cand = REPO / "tasks" / name
        return cand if (cand / "tests").is_dir() else None
    # agent_output/<task>/<model>/<timestamp>_e2e (or the older
    # agent_output/<task>/<timestamp>_e2e): an ancestor names the task.
    for anc in (run_dir.parent, run_dir.parent.parent):
        cand = REPO / "tasks" / anc.name
        if (cand / "tests").is_dir():
            return cand
    return None


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    if not argv:
        print("usage: python scripts/rejudge_sync.py <run_dir>", file=sys.stderr)
        return 2
    run_dir = Path(argv[0]).resolve()
    V = run_dir / "verifier"
    rubric_path = V / "rubric_score.json"
    if not rubric_path.exists():
        print(f"ERROR: {rubric_path} not found — run scripts/judge.py first.", file=sys.stderr)
        return 1

    rubric = _load(rubric_path)
    rubric_score = round(float(rubric.get("rubric_score", 0.0)), 6)
    provider = rubric.get("judge_provider") or judge_lib.DEFAULT_JUDGE_PROVIDER
    model = rubric.get("judge_model")
    judge_available = "rubric_score" in rubric and not rubric.get("error")
    print(f"rubric_score.json: {provider}:{model} score={rubric_score} "
          f"(judge_available={judge_available})")

    # pytest_score and stages are unchanged by a rejudge; read them from the
    # existing files rather than recomputing.
    pytest_score = None
    stages = {}
    for src in ("reward.json", "avg_score.json"):
        if (V / src).exists():
            d = _load(V / src)
            if "pytest_score" in d:
                pytest_score = round(float(d["pytest_score"]), 6)
            sd = d.get("stages_detail") or {}
            if sd:
                stages = {k: v.get("status") for k, v in sd.items()}
            if pytest_score is not None:
                break
    if pytest_score is None and (V / "ctrf.json").exists():
        ctrf = _load(V / "ctrf.json")
        pytest_score = round(float(ctrf.get("reward", 0.0)), 6)
        stages = ctrf.get("stages", stages)
    if pytest_score is None:
        print("ERROR: could not determine pytest_score (no reward/avg/ctrf).", file=sys.stderr)
        return 1

    avg_score = round((pytest_score + rubric_score) / 2.0, 6)
    scoring = "pytest_and_rubric_mean"

    # --- avg_score.json / reward.json / reward.txt (run_harbor shapes) --------
    avg_data = {
        "avg_score": avg_score,
        "pytest_score": pytest_score,
        "rubric_score": rubric_score,
        "judge_available": judge_available,
        "scoring": scoring,
    }
    (V / "avg_score.json").write_text(json.dumps(avg_data, indent=2))

    reward_data = {
        "reward": avg_score,
        "pytest_score": pytest_score,
        "rubric_score": rubric_score,
        "avg_score": avg_score,
        "judge_available": judge_available,
        "scoring": scoring,
        "skip_reason": None,
        "stages_detail": {s: {"status": stages.get(s)} for s in STAGE_KEYS},
    }
    (V / "reward.json").write_text(json.dumps(reward_data, indent=2))
    (V / "reward.txt").write_text(f"{avg_score:.6f}")
    print(f"reward files: pytest={pytest_score} rubric={rubric_score} -> reward={avg_score}")

    # --- calibration.json (fresh judge call with the same provider) -----------
    task_dir = resolve_task_dir(run_dir, None)
    if task_dir is None and (run_dir / "summary.json").exists():
        task_dir = resolve_task_dir(run_dir, _load(run_dir / "summary.json"))
    traj = run_dir / "trajectory" / "agent.jsonl"
    if task_dir and traj.exists():
        os.environ["JUDGE_PROVIDER"] = provider  # keep calibration on the same provider
        test_results = _load(V / "ctrf.json").get("test_results", {}) if (V / "ctrf.json").exists() else {}
        cal = None
        for i in range(CAL_RETRIES):
            cal = judge_lib.evaluate_judge_calibration(task_dir, traj.read_text(errors="replace"),
                                                       test_results, {}, model)
            if cal:
                break
            print(f"  calibration attempt {i+1} failed, retrying...")
        cal_path = V / "calibration.json"
        if cal:
            cal_path.write_text(json.dumps(cal, indent=2))
            print(f"calibration.json: {cal.get('judge_model')} "
                  f"agreement={cal.get('calibration_agreement')} "
                  f"({cal.get('predictions_correct')}/{cal.get('predictions_total')})")
        else:
            # The judge could not produce a calibration. A leftover file from a
            # DIFFERENT judge would falsely label this run, so drop it — matching
            # run_harbor, which writes calibration.json only on success (absence
            # is the correct state for a run whose judge could not calibrate).
            stale = None
            if cal_path.exists():
                try:
                    stale = _load(cal_path).get("judge_model")
                except (OSError, json.JSONDecodeError):
                    stale = "?"
            if stale is not None and stale != model:
                cal_path.unlink()
                print(f"calibration.json: all attempts failed — removed stale "
                      f"{stale} file (a {provider} run that cannot calibrate has none)")
            else:
                print("calibration.json: all attempts failed — left unchanged "
                      "(existing file already matches this judge, or none present)")
    else:
        print("calibration.json: task_dir or trajectory not found — skipped")

    # --- summary.json scalar fields (single-attempt runs) ---------------------
    sp = run_dir / "summary.json"
    if sp.exists():
        s = _load(sp)
        s["reward"] = avg_score
        s["pytest_score"] = pytest_score
        s["rubric_score"] = rubric_score
        s["avg_score"] = avg_score
        if s.get("best_reward") is not None:
            s["best_reward"] = avg_score
        s["judge_available"] = judge_available
        rd = s.get("rubric_detail")
        if isinstance(rd, dict):
            rd["rubric_score"] = rubric_score
            if "criteria" in rubric:
                rd["criteria"] = rubric["criteria"]
            if "judge_usage" in rubric:
                rd["judge_usage"] = rubric["judge_usage"]
            if "trajectory" in rubric:
                rd["trajectory"] = rubric["trajectory"]
        s["judge_provider"] = provider
        s["judge_model"] = model
        sp.write_text(json.dumps(s, indent=2))
        print(f"summary.json: reward={avg_score} (multi-attempt runs: re-check per-attempt dirs)")
    else:
        print("summary.json: not found — skipped")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
