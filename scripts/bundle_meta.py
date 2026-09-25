"""What a task bundle's oracle declares, read for the QC gate and the judge.

Task metadata only: nothing here reads verifier results.
"""
import ast
from pathlib import Path


def read_text(path):
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def _toplevel_constants(py_text):
    """Top-level NAME = <literal or NAME> assignments of a module, names resolved."""
    values = {}
    try:
        tree = ast.parse(py_text)
    except SyntaxError:
        return values
    pending = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            pending[node.targets[0].id] = node.value

    def resolve(node, depth=0):
        if depth > 5 or node is None:
            return None
        if isinstance(node, ast.Name):
            return resolve(pending.get(node.id), depth + 1) if node.id in pending else None
        if isinstance(node, ast.Tuple):
            items = [resolve(e, depth + 1) for e in node.elts]
            return None if any(i is None for i in items) else tuple(items)
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError):
            return None

    for name, node in pending.items():
        values[name] = resolve(node)
    return values


def declared_survivors(task_dir):
    """Files the oracle writes back after restoring the tree (SURVIVOR_RELATIVE_PATHS
    in tests/validate.py); [] when it declares none.  VULNERABLE_SOURCE_RELATIVE_PATH
    alone is not a survivor list: CVE-2026-32597 declares it and restores only run_poc.py."""
    surv = _toplevel_constants(read_text(Path(task_dir) / "tests" / "validate.py")).get("SURVIVOR_RELATIVE_PATHS")
    if isinstance(surv, str):
        return [surv]
    return [s for s in (surv or ()) if isinstance(s, str)]


GRADING_RULE_HEADING = "GRADING RULE for this task"


def grading_rule(task_dir):
    """Judge-facing statement of the oracle's survivor rule, or "" when the
    bundle declares no survivors.  Task metadata, never verifier results."""
    survivors = declared_survivors(Path(task_dir))
    if not survivors:
        return ""
    return (
        f"{GRADING_RULE_HEADING} (stated by the task's grader, not by the agent):\n"
        f"Before the submitted patch is graded, every file in the repository is restored to its "
        f"pristine state except: {', '.join(survivors)}.\n"
        "Any change the agent made anywhere else (a helper in another module, a new file, an "
        "edited test) does not exist in the graded tree.\n"
        "Judge whether the patch works AS GRADED, not as the agent tested it in its own "
        "container: a surviving file that imports or calls code the agent added elsewhere fails "
        "to import and fixes nothing.\n"
    )
