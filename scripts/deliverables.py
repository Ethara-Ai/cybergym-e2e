#!/usr/bin/env python3
"""Client deliverables tree, one project per task:

    deliverables/<task>/
    ├── TRUTH.md                      ground truth and how the task is scored
    ├── data/                         verbatim copy of the input task bundle
    └── trajectories_<uuid>/<model>/  run<N>/ per harness run + pass_summary.json

run<N>/ holds result.json, run<N>_summary.json, fix.patch, poc.bin (e2e tasks),
agent/, verifier/, artifacts/ (see export_run).  `python scripts/deliverables.py <agent_output run dir>` exports
or re-exports one run, for example after scripts/rejudge_sync.py.
"""
import hashlib
import json
import math
import shutil
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from judge_lib import cost_estimation_enabled, pricing_known

try:
    import tomllib
except ImportError:                       # Python < 3.11
    import tomli as tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_names import (                  # noqa: E402
    STAGE_DESCRIPTIONS,
    STAGE_KEYS,
    load_task_stage_map,
    map_stages,
    required_stages,
    task_mode,
)
from trajectory import UNSCORED_STATUSES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_EXCLUDE = {".DS_Store", "__pycache__", ".pytest_cache"}
FINGERPRINT_FILE = ".bundle_fingerprint.json"


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dump(obj, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def _load(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _bundle_files(task_dir):
    task_dir = Path(task_dir)
    # Skip symlinks so a stray link cannot pull foreign bytes into the fingerprint.
    return sorted(p for p in task_dir.rglob("*")
                  if not p.is_symlink() and p.is_file()
                  and not (set(p.relative_to(task_dir).parts) & DATA_EXCLUDE))


def bundle_fingerprint(task_dir):
    """sha256 over every file's (relative path, sha256), so a re-packed bundle is detectable."""
    h = hashlib.sha256()
    for p in _bundle_files(task_dir):
        h.update(f"{p.relative_to(task_dir).as_posix()}\0{_sha256(p)}\n".encode())
    return h.hexdigest()


def _table(rows, header):
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", " ")
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def write_truth_md(task_dir, dest):
    """TRUTH.md: the instruction, the reference fix, the hidden reproducers,
    every weighted test with its stage, the rubric, and the reward formula."""
    task_dir = Path(task_dir)
    cfg = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8")) \
        if (task_dir / "task.toml").exists() else {}
    weights = _load(task_dir / "tests" / "test_weights.json", {}) or {}
    rubric = _load(task_dir / "tests" / "rubric.json", []) or []
    stage_map = load_task_stage_map(task_dir)
    required = sorted(required_stages(weights, stage_map))
    task, meta = cfg.get("task", {}), cfg.get("metadata", {})
    lines = [f"# {task.get('name', task_dir.name)} — ground truth and scoring", "",
             f"- **Description:** {task.get('description', '')}",
             f"- **Mode:** {task_mode(task_dir)}",
             f"- **Difficulty / category:** {meta.get('difficulty', '?')} / {meta.get('category', '?')}"
             f" / {meta.get('subcategory', '?')}",
             f"- **Tags:** {', '.join(meta.get('tags', []))}",
             f"- **Artifacts the grader reads:** {', '.join(cfg.get('artifacts', []))}",
             f"- **Stages that must pass for success:** {', '.join(required) or 'n/a'}", ""]
    instr = task_dir / "instruction.md"
    if instr.exists():
        lines += ["## Task given to the agent", "", "```markdown",
                  instr.read_text(encoding="utf-8", errors="replace").rstrip(), "```", ""]
    fix = task_dir / "solution" / "fix.patch"
    if fix.exists():
        lines += ["## Reference fix (solution/fix.patch)", "", "```diff",
                  fix.read_text(encoding="utf-8", errors="replace").rstrip(), "```", ""]
    pocs = [p for p in sorted(list((task_dir / "tests" / "data").glob("*"))
                              + list((task_dir / "solution").glob("poc*"))) if p.is_file()]
    if pocs:
        lines += ["## Reproducers (hidden from the agent unless given/)", "",
                  _table([(p.relative_to(task_dir).as_posix(), p.stat().st_size, _sha256(p)[:16])
                          for p in pocs], ["file", "bytes", "sha256 (prefix)"]), ""]
    if weights:
        rows = []
        for name, w in weights.items():
            stage = next(iter(map_stages({name: "passed"}, stage_map)), "")
            role = f"{stage} — {STAGE_DESCRIPTIONS[stage]}" if stage else ("cheating gate" if w < 0 else "")
            rows.append((name, w, role))
        positive = sum(w for w in weights.values() if w > 0)
        lines += ["## Verifier tests and weights", "", _table(rows, ["test", "weight", "stage / role"]), "",
                  f"Positive weight total: **{positive}**. Negative weights penalise shortcuts "
                  "and are never required for success.", ""]
    if rubric:
        lines += ["## Rubric (LLM judge)", "",
                  _table([(r.get("number"), r.get("criterion"), r.get("importance", ""), r.get("score"),
                           "yes" if r.get("is_positive", True) else "no") for r in rubric],
                         ["#", "criterion", "importance", "score", "positive"]), ""]
    lines += ["## Reward", "",
              "- `pytest_score = Σ(weights of passed tests) / Σ(positive weights)`, clamped to [-1, 1] (tests/test.sh).",
              "- `rubric_score = earned / total_positive` of the lower-median judge trial (scripts/judge_lib.py).",
              "- `reward = avg_score = (pytest_score + rubric_score) / 2`; with `--no-judge` the reward is "
              "`pytest_score` alone and is labelled `scoring: pytest_only`.",
              f"- `success` = every required stage passed ({', '.join(required) or 'n/a'}).", ""]
    text = "\n".join(lines)
    Path(dest).write_text(text, encoding="utf-8")
    return text


def ensure_project(root, task_dir, trajectories_dir=None):
    """deliverables/<task>/ with TRUTH.md and data/ (written once), plus the
    trajectories_<uuid>/ batch dir: `trajectories_dir` when given, else the
    newest existing one, else a new one.  Returns (project_dir, trajectories_dir)."""
    task_dir = Path(task_dir).resolve()
    project = Path(root) / task_dir.name
    project.mkdir(parents=True, exist_ok=True)
    data = project / "data"
    if not data.exists():
        shutil.copytree(task_dir, data, ignore=shutil.ignore_patterns(*DATA_EXCLUDE))
    else:
        (data / FINGERPRINT_FILE).unlink(missing_ok=True)       # written by earlier versions
        if bundle_fingerprint(data) != bundle_fingerprint(task_dir):
            print(f"  !! deliverables: {data} differs from the current task bundle {task_dir} "
                  "(it was copied from an earlier version); the run is exported anyway")
    if not (project / "TRUTH.md").exists():
        write_truth_md(task_dir, project / "TRUTH.md")
    if trajectories_dir:
        traj = Path(trajectories_dir)
    else:
        existing = sorted(project.glob("trajectories_*"), key=lambda p: p.stat().st_mtime)
        traj = existing[-1] if existing else project / f"trajectories_{uuid.uuid4()}"
    traj.mkdir(parents=True, exist_ok=True)
    return project, traj


def allocate_run_dir(trajectories_dir, model_slug):
    """run<N>/ with the first free N.  mkdir is atomic, so two runners never share one."""
    model_dir = Path(trajectories_dir) / model_slug
    model_dir.mkdir(parents=True, exist_ok=True)
    for n in range(1, 10001):
        try:
            (model_dir / f"run{n}").mkdir()
            return model_dir / f"run{n}", n
        except FileExistsError:
            continue
    # Bounded fallback: contention above 10k means fresh sequential names are
    # pathological; switch to a monotonic ns+hex suffix so mkdir cannot spin.
    tag = f"{time.time_ns():x}{uuid.uuid4().hex[:6]}"
    d = model_dir / f"run_{tag}"
    d.mkdir()
    return d, -1


def attempt_names(attempt=None):
    """File names inside an agent_output run dir for one attempt (None = single-attempt run)."""
    if attempt is None:
        return {"jsonl": "agent.jsonl", "traj": "trajectory.json", "stderr": "stderr.log",
                "session": "claude_session", "verifier": "verifier", "artifacts": "artifacts",
                "patch": "fix.patch", "poc": "poc.bin"}
    return {"jsonl": f"attempt_{attempt}.jsonl", "traj": f"trajectory_attempt_{attempt}.json",
            "stderr": f"attempt_{attempt}_stderr.log",
            "session": f"claude_session_attempt_{attempt}",
            "verifier": f"verifier/attempt_{attempt}", "artifacts": f"artifacts/attempt_{attempt}",
            "patch": f"fix_attempt_{attempt}.patch", "poc": f"poc_attempt_{attempt}.bin"}


def cache_accounting(summary):
    """How cache_creation_input_tokens was produced: the provider's own report,
    zbridge's block model, or a bridge that states there are no cache writes."""
    attribution = ((summary or {}).get("agent_bridge") or {}).get("cache_write_attribution")
    if attribution == "block":
        return "zbridge_block_model"
    if attribution == "none":
        return "bridge_reports_none"
    return "provider_reported"


def _first(extra, *names):
    """First of `names` present in a trajectory's final_metrics.extra.

    scripts/trajectory.py (Claude Code) and scripts/harbor_agents/
    openhands_sdk_runner.py name the same counters differently; a reader that
    knows only one spelling silently drops the other agent's numbers.
    """
    for n in names:
        v = (extra or {}).get(n)
        if v is not None:
            return v
    return None


def _cache_write_tokens(extra, summary=None):
    reported = _first(extra, "total_cache_creation_input_tokens", "total_cache_write_tokens")
    if reported is not None:
        return reported
    # "none" states that the provider performs no cache writes; that is a count
    # of zero, not an absence of data, and it makes uncached derivable.
    if ((summary or {}).get("agent_bridge") or {}).get("cache_write_attribution") == "none":
        return 0
    return None


def _uncached_input_tokens(fm, extra, summary=None):
    """Reported when the writer gives it, else prompt minus the two cache legs."""
    reported = (extra or {}).get("total_uncached_input_tokens")
    if reported is not None:
        return reported
    prompt = (fm or {}).get("total_prompt_tokens")
    cache_read = (fm or {}).get("total_cached_tokens")
    cache_write = _cache_write_tokens(extra, summary)
    if None in (prompt, cache_read, cache_write):
        return None
    derived = prompt - cache_read - cache_write
    return derived if derived >= 0 else None


def _agent_cost_provenance(fm, extra, model):
    """(cost_source, cost_known) for the agent leg.

    scripts/trajectory.py reports both; the openhands-sdk runner reports neither
    and its figure comes from litellm applying MODEL_PRICING, so it is a
    list-price estimate.  The predicate mirrors judge_lib's own.
    """
    source = (extra or {}).get("cost_source")
    known = (extra or {}).get("cost_known")
    if source is not None or known is not None:
        return source, known
    if (fm or {}).get("total_cost_usd") is None:
        return None, None
    return "list_price_estimate", bool(pricing_known(model)) and cost_estimation_enabled()


def build_usage(trajectory, rubric, calibration, summary):
    """verifier/usage.json: agent and judge tokens and cost for one run."""
    fm = (trajectory or {}).get("final_metrics") or {}
    ex = fm.get("extra") or {}
    ju = (rubric or {}).get("judge_usage") or {}
    cu = (calibration or {}).get("judge_usage") or {}
    summary = summary or {}
    _agent_model = summary.get("model") or (trajectory or {}).get("agent", {}).get("model_name")
    agent = {
        "model": _agent_model,
        "provider": summary.get("model_provider"),
        "uncached_input_tokens": _uncached_input_tokens(fm, ex, summary),
        "cache_read_input_tokens": fm.get("total_cached_tokens"),
        "cache_creation_input_tokens": _cache_write_tokens(ex, summary),
        "cache_accounting": cache_accounting(summary),
        "prompt_tokens": fm.get("total_prompt_tokens"),
        "completion_tokens": fm.get("total_completion_tokens"),
        "thinking_tokens": _first(ex, "total_thinking_tokens", "total_reasoning_tokens"),
        "cost_usd": fm.get("total_cost_usd"),
        "cost_source": _agent_cost_provenance(fm, ex, _agent_model)[0],
        "cost_known": _agent_cost_provenance(fm, ex, _agent_model)[1],
        "num_turns": ex.get("num_turns"),
        "duration_ms": ex.get("duration_ms"),
        "steps": fm.get("total_steps"),
        # The CLI's per-model block minus the web-search counter, which the
        # sandbox makes meaningless (no web tools are available to the agent).
        "model_usage": {m: {k: v for k, v in (u or {}).items() if k != "webSearchRequests"}
                        for m, u in (ex.get("model_usage") or {}).items()} or None,
    }
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "cost_usd", "cost_known")
    judge = None
    if rubric:
        judge = {"provider": rubric.get("judge_provider"), "model": rubric.get("judge_model"),
                 "trials": rubric.get("trials_succeeded"), **{k: ju.get(k) for k in keys}}
    calib = {k: cu.get(k) for k in keys} if calibration else None
    parts = [agent["cost_usd"], (judge or {}).get("cost_usd"), (calib or {}).get("cost_usd")]
    return {
        "schema": "cybergym-e2e-usage-v1",
        "agent": agent,
        "judge": judge,
        "calibration": calib,
        "total_cost_usd": round(sum(p for p in parts if isinstance(p, (int, float))), 6),
        "cost_known": bool(agent.get("cost_known")) and (judge is None or bool(judge.get("cost_known", True))),
    }


def build_score(summary, reward, avg, ctrf_enriched, rubric, calibration):
    """verifier/score.json: everything that went into the reward, in one file."""
    summary, reward, avg = summary or {}, reward or {}, avg or {}
    ctrf_enriched = ctrf_enriched or {}
    pure = ctrf_enriched.get("ctrf") if isinstance(ctrf_enriched.get("ctrf"), dict) else ctrf_enriched
    tests = ((pure or {}).get("results") or {}).get("tests") or []
    weights = summary.get("test_weights") or {}
    scores = avg or reward
    return {
        "schema": "cybergym-e2e-score-v1",
        "reward": reward.get("reward", scores.get("avg_score")),
        "pytest_score": scores.get("pytest_score"),
        "rubric_score": scores.get("rubric_score"),
        "scoring": scores.get("scoring"),
        "judge_available": scores.get("judge_available"),
        "status": summary.get("status"),
        "success": summary.get("agent_success"),
        "mode": summary.get("mode"),
        "required_stages": summary.get("required_stages"),
        "stages": summary.get("stages"),
        "binary_stages": scores.get("binary_stages"),
        "skip_reason": summary.get("skip_reason"),
        "tests": [{"name": t.get("name"), "status": t.get("status"),
                   "weight": t.get("weight", weights.get(t.get("name"))),
                   "duration": t.get("duration")} for t in tests],
        "test_weights": weights,
        "rubric": rubric,
        "calibration": calibration,
    }


def export_run(src_run_dir, dest_run_dir, attempt=None):
    """Copy one agent_output run into a deliverables run<N>/ and derive the
    client-facing files.  `attempt` is the reported attempt of a multi-attempt
    run (None for a single-attempt run).  Returns the export manifest."""
    src, dest = Path(src_run_dir).resolve(), Path(dest_run_dir)
    names = attempt_names(attempt)
    summary = _load(src / "summary.json", {}) or {}
    manifest = {"exported_at": _now(), "source_run_dir": str(src), "reported_attempt": attempt,
                "files": [], "skipped": []}

    def take(rel_src, rel_dest):
        s, d = src / rel_src, dest / rel_dest
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        elif s.is_file():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        else:
            manifest["skipped"].append({"file": rel_dest,
                                        "reason": f"{rel_src} was not produced by the run"})
            return None
        manifest["files"].append(rel_dest)
        return d

    # Earlier exports wrote a synthesized cast, a raw/ tree and config.json; all are gone.
    shutil.rmtree(dest / "agent" / "raw", ignore_errors=True)
    (dest / "agent" / "recording.cast").unlink(missing_ok=True)
    (dest / "config.json").unlink(missing_ok=True)
    traj_path = take(f"trajectory/{names['traj']}", "agent/trajectory.json")
    trajectory = (_load(traj_path, {}) if traj_path else {}) or {}
    take(f"trajectory/{names['jsonl']}", "agent/agent.jsonl")

    # The agent's submission at the run root.  Patch-only tasks never produce a
    # PoC, so its absence there is not recorded as a skipped file.
    take(f"output/{names['patch']}", "fix.patch")
    if summary.get("mode") != "patch-only":
        take(f"output/{names['poc']}", "poc.bin")

    v = names["verifier"]
    take(f"{v}/reward.json", "verifier/reward.json")
    take(f"{v}/test-stdout.txt", "verifier/test-stdout.txt")
    take(f"{v}/rubric_score.json", "verifier/rubric.json")      # the judge's verdict, or the runner's no-judge sentinel
    ctrf = _load(src / v / "ctrf.json", {}) or {}
    pure = ctrf.get("ctrf") if isinstance(ctrf.get("ctrf"), dict) else ctrf
    if (pure or {}).get("results"):
        _dump(pure, dest / "verifier" / "ctrf.json")
        manifest["files"].append("verifier/ctrf.json")
    else:
        manifest["skipped"].append({"file": "verifier/ctrf.json",
                                    "reason": "no CTRF results (attempt not graded)"})
    reward, avg = _load(src / v / "reward.json"), _load(src / v / "avg_score.json")
    rubric, calibration = _load(src / v / "rubric_score.json"), _load(src / v / "calibration.json")
    _dump(build_score(summary, reward, avg, ctrf, rubric, calibration), dest / "verifier" / "score.json")
    _dump(build_usage(trajectory, rubric, calibration, summary), dest / "verifier" / "usage.json")
    manifest["files"] += ["verifier/score.json", "verifier/usage.json"]

    take(names["artifacts"], "artifacts")
    if attempt is not None:
        for sub in ("trajectory", "verifier", "artifacts", "output"):
            s = src / sub
            if s.is_dir():
                shutil.copytree(s, dest / "attempts" / sub, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns("claude_session*"))
                manifest["files"].append(f"attempts/{sub}")

    result = dict(summary)
    result.update({"record_type": "cybergym-e2e run summary", "run": dest.name,
                   "model_dir": dest.parent.name, "trajectories_dir": dest.parent.parent.name,
                   "reported_attempt": attempt, "source_run_dir": str(src)})
    _dump(result, dest / "result.json")
    _dump(build_run_summary(dest), dest / f"{dest.name}_summary.json")
    manifest["files"].append(f"{dest.name}_summary.json")
    art_manifest = _load(dest / "artifacts" / "manifest.json", {}) or {}
    art_manifest["export"] = manifest
    _dump(art_manifest, dest / "artifacts" / "manifest.json")
    return manifest


def _run_index(path):
    return int(path.name[3:]) if path.name[3:].isdigit() else 0


def _same_run(recorded, run_dir):
    """True when a result.json's source_run_dir names `run_dir`.  Older exports
    stored the path as run_harbor.py saw it (relative to the repo root)."""
    if not recorded:
        return False
    rec = Path(recorded)
    if not rec.is_absolute():
        rec = REPO_ROOT / rec
    return rec.resolve() == Path(run_dir).resolve()


def _run_record(run):
    """The per-run entry used by pass_summary.json (None when the run has no result.json)."""
    res = _load(run / "result.json")
    if not res:
        return None
    usage = _load(run / "verifier" / "usage.json", {}) or {}
    agent = usage.get("agent") or {}
    reward_val = res.get("reward")
    return {
        "run": run.name,
        "task": res.get("task"),
        "status": res.get("status"),
        "success": bool(res.get("agent_success")),
        # A run only counts toward pass@k when it actually yielded a number:
        # a judge outage publishes reward null, and a null must not enter the
        # denominator as a failure.
        "scored": (res.get("status") not in UNSCORED_STATUSES
                   and isinstance(reward_val, (int, float))
                   and math.isfinite(reward_val)),
        "reward": reward_val,
        "pytest_score": res.get("pytest_score"),
        "rubric_score": res.get("rubric_score"),
        "judge_available": res.get("judge_available"),
        "stages": {s: ((res.get("stages") or {}).get(s) or {}).get("status") for s in STAGE_KEYS},
        "duration_seconds": res.get("duration_seconds"),
        "prompt_tokens": agent.get("prompt_tokens"),
        "completion_tokens": agent.get("completion_tokens"),
        "cost_usd": usage.get("total_cost_usd"),
        "cost_known": usage.get("cost_known"),
        "skip_reason": res.get("skip_reason"),
        "summary_file": f"{run.name}/{run.name}_summary.json",
    }


def _started_at_from_dir_name(output_dir):
    """Runs recorded before summary.json carried started_at: the run directory
    is named <YYYYmmdd_HHMMSS>_e2e[...]."""
    name = Path(output_dir or "").name
    stamp = name.split("_e2e")[0]
    return stamp if len(stamp) == 15 and stamp[8] == "_" and stamp.replace("_", "").isdigit() else None


def build_run_summary(run_dir):
    """run<N>_summary.json: this run's pass_summary entry plus the run-only
    detail (tests, tokens, cost, judge, agent, files)."""
    run_dir = Path(run_dir)
    rec = _run_record(run_dir) or {"run": run_dir.name, "status": None, "success": False, "scored": False}
    res = _load(run_dir / "result.json", {}) or {}
    usage = _load(run_dir / "verifier" / "usage.json", {}) or {}
    score = _load(run_dir / "verifier" / "score.json", {}) or {}
    agent_u, judge_u = usage.get("agent") or {}, usage.get("judge") or {}
    tests = score.get("tests") or []
    failed = [t["name"] for t in tests if t.get("status") != "passed"]
    files = {label: rel for label, rel in (
        ("patch", "fix.patch"), ("poc", "poc.bin"),
        ("trajectory", "agent/trajectory.json"), ("agent_log", "agent/agent.jsonl"),
        ("result", "result.json"),
        ("score", "verifier/score.json"), ("usage", "verifier/usage.json"),
        ("reward", "verifier/reward.json"), ("rubric", "verifier/rubric.json"), ("ctrf", "verifier/ctrf.json"),
        ("test_stdout", "verifier/test-stdout.txt"), ("artifacts_manifest", "artifacts/manifest.json"),
    ) if (run_dir / rel).is_file()}
    success_rate = (1.0 if rec["success"] else 0.0) if rec["scored"] else None
    return {
        "schema": "cybergym-e2e-run-summary-v1",
        "generated_at": _now(),
        "task": res.get("task"),
        "model": res.get("model"),
        "model_provider": res.get("model_provider"),
        "model_dir": run_dir.parent.name,
        "trajectories_dir": run_dir.parent.parent.name,
        "run": rec["run"],
        "status": rec["status"],
        "success": rec["success"],
        "scored": rec["scored"],
        "success_rate": success_rate,
        "pass_at_1": success_rate,
        "reward": rec.get("reward"),
        "pytest_score": rec.get("pytest_score"),
        "rubric_score": rec.get("rubric_score"),
        "scoring": res.get("scoring"),
        "stages": rec.get("stages"),
        "required_stages": res.get("required_stages"),
        "binary_stages": score.get("binary_stages"),
        "tests": {"total": len(tests), "passed": len(tests) - len(failed), "failed": len(failed),
                  "failed_names": failed},
        "attempts": len(res.get("attempts") or []),
        "reported_attempt": res.get("reported_attempt"),
        "skip_reason": rec.get("skip_reason"),
        "judge": {"provider": judge_u.get("provider"), "model": judge_u.get("model"),
                  "available": res.get("judge_available"), "trials": judge_u.get("trials")},
        "agent": {"name": res.get("agent") or rec.get("agent") or "unknown",
                  "version": res.get("agent_version"),
                  "num_turns": agent_u.get("num_turns"), "steps": agent_u.get("steps"),
                  "duration_ms": agent_u.get("duration_ms")},
        "tokens": {"prompt_tokens": agent_u.get("prompt_tokens"),
                   "uncached_input_tokens": agent_u.get("uncached_input_tokens"),
                   "cache_read_input_tokens": agent_u.get("cache_read_input_tokens"),
                   "cache_creation_input_tokens": agent_u.get("cache_creation_input_tokens"),
                   "completion_tokens": agent_u.get("completion_tokens")},
        "cost": {"agent_usd": agent_u.get("cost_usd"), "judge_usd": judge_u.get("cost_usd"),
                 "total_usd": usage.get("total_cost_usd"), "cost_known": usage.get("cost_known"),
                 "cost_source": agent_u.get("cost_source")},
        "duration_seconds": rec.get("duration_seconds"),
        "started_at": res.get("started_at") or _started_at_from_dir_name(res.get("output_dir")),
        "finished_at": res.get("finished_at"),
        "files": files,
    }


def write_pass_summary(model_dir):
    """pass_summary.json: rollup over every run<N>/ under one model dir.
    Rewritten after each export, so it always covers every run present."""
    model_dir = Path(model_dir)
    runs, task = [], None
    for run in sorted((p for p in model_dir.glob("run*") if p.is_dir()), key=_run_index):
        rec = _run_record(run)
        if not rec:
            continue
        task = task or rec["task"]
        runs.append(rec)
    scored = [r for r in runs if r["scored"]]
    n, c = len(scored), sum(1 for r in scored if r["success"])
    rewards = [r["reward"] for r in scored
               if isinstance(r["reward"], (int, float)) and math.isfinite(r["reward"])]
    summary = {
        "schema": "cybergym-e2e-pass-summary-v1",
        "generated_at": _now(),
        "task": task,
        "model_dir": model_dir.name,
        "trajectories_dir": model_dir.parent.name,
        "runs_total": len(runs),
        "runs_scored": n,
        # A run that is not scored is either a real failure of the harness
        # (UNSCORED_STATUSES) or a run the judge never evaluated, which is an
        # absence of measurement rather than an error.  Counting the latter as
        # "errored" reads as though the run broke.
        "runs_errored": sum(1 for r in runs if r["status"] in UNSCORED_STATUSES),
        "runs_unjudged": sum(1 for r in runs
                             if r["status"] not in UNSCORED_STATUSES and not r["scored"]),
        "successes": c,
        "success_rate": round(c / n, 6) if n else None,
        "reward_mean": round(statistics.fmean(rewards), 6) if rewards else None,
        "reward_best": max(rewards) if rewards else None,
        "reward_stdev": round(statistics.stdev(rewards), 6) if len(rewards) > 1 else None,
        "total_cost_usd": round(sum(r["cost_usd"] or 0 for r in runs), 6),
        "runs": runs,
    }
    _dump(summary, model_dir / "pass_summary.json")
    return summary


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Export (or re-export) one agent_output run into deliverables/")
    ap.add_argument("run_dir", help="agent_output/<task>/<model>/<timestamp>_e2e")
    ap.add_argument("--task-dir", help="input bundle (default: summary.json's harbor_task)")
    ap.add_argument("--deliverables-dir", default=str(REPO_ROOT / "deliverables"))
    ap.add_argument("--trajectories-dir", help="existing trajectories_<uuid> dir to add the run to")
    ap.add_argument("--model-slug", help="model directory name (default: the run dir's parent name)")
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).resolve()
    summary = _load(run_dir / "summary.json", {}) or {}
    task_dir = Path(args.task_dir or summary.get("harbor_task") or "")
    if not (task_dir / "task.toml").exists():
        sys.exit(f"ERROR: task bundle not found ({task_dir}); pass --task-dir")
    slug = args.model_slug or run_dir.parent.name
    _, traj = ensure_project(args.deliverables_dir, task_dir, args.trajectories_dir)
    previous = next((r for r in sorted((traj / slug).glob("run*"), key=_run_index)
                     if _same_run((_load(r / "result.json", {}) or {}).get("source_run_dir"), run_dir)), None)
    dest = previous or allocate_run_dir(traj, slug)[0]
    reported = summary.get("best_attempt") if (summary.get("max_attempts") or 1) > 1 else None
    export_run(run_dir, dest, attempt=reported)
    write_pass_summary(dest.parent)
    print(f"exported {run_dir} -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
