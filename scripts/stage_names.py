"""Single source of truth for the verifier test-name -> stage mapping.

Both ``run_harbor.py`` (the runner) and ``scripts/qc_harbor.py`` (the QC gate)
import this table.  Keeping one copy is what guarantees that a task accepted by
QC is a task the runner can actually map onto ``stage1``..``stage4``; the two
tables used to be maintained separately and disagreed on every stage.

Names are matched exactly.  If a task needs a project-specific spelling, add it
here (and only here) so that both sides see it at once.
"""

STAGE_KEYS = ("stage1", "stage2", "stage3", "stage4")

STAGE_DESCRIPTIONS = {
    "stage1": "PoC crashes without patch",
    "stage2": "PoC OK with patch",
    "stage3": "Tests pass with patch",
    "stage4": "GT PoC OK with patch (found THE bug)",
}

# Union of every spelling either side has ever accepted.  The canonical
# template names come first in each list.
STAGE_TEST_NAMES = {
    "stage1": [
        "test_stage1_poc_crashes_without_patch",
        "test_stage1_agent_poc_crashes_vuln_build",
        "test_agent_poc_crashes_vuln_build",
        "test_stage1_matio_poc_faults_vuln",
    ],
    "stage2": [
        "test_stage2_poc_ok_with_patch",
        "test_stage2_agent_poc_neutralized_by_patch",
        "test_agent_poc_neutralized_by_patch",
        "test_stage2_matio_patch_clears_agent_poc",
        "test_stage2_poc_no_sanitizer_report_with_patch",
    ],
    "stage3": [
        "test_stage3_tests_pass_with_patch",
        "test_stage3_location_suite_passes_with_patch",
        "test_stage3_project_test_suite_passes_with_patch",
        "test_project_suite_passes_with_patch",
        "test_stage3_matio_suite_passes_patched",
    ],
    "stage4": [
        "test_stage4_gt_poc_ok_with_patch",
        "test_stage4_ground_truth_poc_neutralized_by_patch",
        "test_ground_truth_poc_neutralized_by_patch",
        "test_stage4_matio_patch_clears_reference_poc",
        "test_stage4_opus_parse_functional_regression",
    ],
}

ALL_STAGE_NAMES = frozenset(n for names in STAGE_TEST_NAMES.values() for n in names)


def stage_for_test(test_name):
    """Return the stage key for a verifier test name, or None if unmapped."""
    for stage, names in STAGE_TEST_NAMES.items():
        if test_name in names:
            return stage
    return None


def load_toml(path):
    """Parse a TOML file with tomllib (3.11+) or tomli."""
    from pathlib import Path
    data = Path(path).read_bytes().decode("utf-8", errors="replace")
    try:
        import tomllib
        return tomllib.loads(data)
    except ImportError:
        import tomli
        return tomli.loads(data)


def task_mode(task_dir):
    """'e2e' or 'patch-only' -- the ONE implementation used by the runner and
    the QC gate.

    Precedence: top-level ``mode`` in task.toml; else the parsed ``artifacts``
    list (no /output/poc.bin -> patch-only); else the instruction text
    ("patch-only" / "cg-verify"); else e2e.
    """
    from pathlib import Path
    task_dir = Path(task_dir)
    cfg = None
    toml_path = task_dir / "task.toml"
    if toml_path.exists():
        try:
            cfg = load_toml(toml_path)
        except Exception:
            cfg = None
    if isinstance(cfg, dict):
        if cfg.get("mode") in ("e2e", "patch-only"):
            return cfg["mode"]
        # Older hand-written tasks put the key under [task].
        task_tbl = cfg.get("task")
        if isinstance(task_tbl, dict) and task_tbl.get("mode") in ("e2e", "patch-only"):
            return task_tbl["mode"]
        arts = cfg.get("artifacts")
        if isinstance(arts, list):
            return "e2e" if "/output/poc.bin" in arts else "patch-only"
    instr_path = task_dir / "instruction.md"
    instr = instr_path.read_text(errors="replace") if instr_path.exists() else ""
    if "patch-only" in instr.lower() or "cg-verify" in instr:
        return "patch-only"
    return "e2e"


def load_task_stage_map(task_dir):
    """Optional per-task override: ``tests/stage_map.json`` mapping
    ``{"stage1": "test_name", ...}`` (a name or a list of names) for tasks
    whose oracle uses custom test names.  Returns ``{stage: [names]}`` or
    ``{}`` when absent or unreadable.
    """
    import json
    from pathlib import Path
    path = Path(task_dir) / "tests" / "stage_map.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
    except Exception:
        return {}
    out = {}
    for stage in STAGE_KEYS:
        v = raw.get(stage)
        if isinstance(v, str):
            out[stage] = [v]
        elif isinstance(v, list):
            out[stage] = [x for x in v if isinstance(x, str)]
    return out


def stage_table(task_map=None):
    """The shared table with a task's own ``stage_map.json`` names first."""
    if not task_map:
        return STAGE_TEST_NAMES
    return {stage: list(task_map.get(stage, [])) + list(names)
            for stage, names in STAGE_TEST_NAMES.items()}


def map_stages(test_results, task_map=None):
    """Map ``{test_name: status}`` onto ``{stage_key: status}``.

    The first known name found for a stage wins, matching the runner's
    historical behaviour.  ``task_map`` (see load_task_stage_map) is
    consulted before the shared table.
    """
    stages = {}
    for stage, names in stage_table(task_map).items():
        for name in names:
            if name in test_results:
                stages[stage] = test_results[name]
                break
    return stages


def required_stages(test_weights, task_map=None):
    """Stages a task actually grades, derived from its ``test_weights.json``.

    A patch-only task has no stage-1 test (the agent is given the PoC), so
    success must be judged on the stages that exist rather than on all four.
    """
    return {
        stage for stage, names in stage_table(task_map).items()
        if any(n in test_weights for n in names)
    }
