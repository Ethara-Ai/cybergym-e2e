#!/usr/bin/env bash
# Remove a task's target library from the agent runtime venv, then prove it is
# gone.  See ORACLE_LEAK_FIX.md.
#
# Why this exists: the agent image layers an OpenHands SDK venv on top of the
# clean task image.  That venv's dependency closure ships readable, released
# copies of third-party libraries -- including, for a PyJWT task, a PyJWT four
# minor versions ahead of the vulnerable baseline at /src.  A single `diff`
# against /src then hands the agent the injected CWE for free, collapsing the
# discovery phase.  Removing the target from the agent runtime closes that.
#
# Usage: scrub_oracle.sh <dist-name>      e.g. scrub_oracle.sh pyjwt
#
# SCOPE: this touches /opt/openhands-sdk-venv ONLY.  The task's own editable
# install under the system interpreter (/usr/local/lib/pythonX.Y/site-packages,
# pointing at /src) is the baseline the verifier needs -- never remove it.
set -euo pipefail

DIST="${1:?dist name required}"
VENV=/opt/openhands-sdk-venv
PY="$VENV/bin/python"
UV=/opt/uv/uv

[ -x "$PY" ] || { echo "[scrub] no agent runtime at $VENV; nothing to do"; exit 0; }

# Resolve top-level import names BEFORE uninstalling, so we do not need a
# hand-maintained dist->import table (pyjwt->jwt, pillow->PIL, pyyaml->yaml).
IMPORTS="$("$PY" - "$DIST" <<'PY'
import sys
import importlib.metadata as md

try:
    dist = md.distribution(sys.argv[1])
except md.PackageNotFoundError:
    sys.exit(0)

names = set()
top_level = dist.read_text("top_level.txt")
if top_level:
    names.update(line.strip() for line in top_level.splitlines() if line.strip())
else:
    for f in dist.files or []:
        head = str(f).split("/")[0]
        if head.endswith(".dist-info"):
            continue
        names.add(head[:-3] if head.endswith(".py") else head)

# CRITICAL: RECORD paths may escape site-packages (e.g. "../../bin/foo"), so a
# naive split yields "..".  Feeding that to `rm -rf` would target the parent
# directory.  A real top-level import name is always a Python identifier, so
# that filter drops "..", ".", stray data dirs and anything path-like.
print(" ".join(sorted(n for n in names if n and n.isidentifier())))
PY
)"

if [ -z "$IMPORTS" ]; then
    echo "[scrub] clean: '$DIST' is not present in the agent runtime"
    exit 0
fi

echo "[scrub] '$DIST' present in agent runtime as: $IMPORTS"
"$UV" pip uninstall --python "$PY" "$DIST" >/dev/null 2>&1 || true

# uninstall can leave stray package dirs / caches behind.  Confine every
# deletion to the venv's own site-packages -- never /src, never the system
# interpreter that carries the task's editable baseline install.
for SP in "$VENV"/lib/python*/site-packages; do
    [ -d "$SP" ] || continue
    for name in $IMPORTS; do
        # Belt and braces: the Python side already filters to identifiers, but
        # never let a traversal or separator reach rm -rf.
        case "$name" in
            ""|"."|".."|*/*|.*)
                echo "[scrub] refusing unsafe import name: '$name'" >&2
                exit 4
                ;;
        esac
        rm -rf "${SP:?}/${name:?}" "${SP:?}/${name:?}.py"
    done
    rm -rf "${SP:?}"/"${DIST}"-*.dist-info "${SP:?}"/"${DIST//-/_}"-*.dist-info
    find "$SP" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
done

# Wheels in the caches carry the same oracle, one unzip away.
rm -rf /tmp/uv-cache /root/.cache/uv /root/.cache/pip

# If the SDK genuinely needs it, fail loudly rather than shipping a broken
# agent.  Exercise the FULL chain the runner imports (openhands_sdk_runner.py
# builds exactly these three tools), not just `import openhands.sdk` -- a
# shallow check would pass while the agent still died at conversation start.
if ! "$PY" - <<'PY'
import traceback
try:
    # The chain openhands_sdk_runner.py actually builds.
    import openhands.sdk  # noqa: F401
    from openhands.sdk import LLM, Agent, Conversation  # noqa: F401
    from openhands.sdk.context import Skill  # noqa: F401
    from openhands.tools.file_editor import FileEditorTool  # noqa: F401
    from openhands.tools.task_tracker import TaskTrackerTool  # noqa: F401
    from openhands.tools.terminal import TerminalTool  # noqa: F401

    # Indirect chains that a naive tool-only check misses.  settings pulls
    # openhands.sdk.mcp.config -> fastmcp -> mcp, and mcp imports jwt in its
    # OAuth client-credentials extension; llm.auth.openai reaches settings
    # lazily.  Removing a dist that any of these need must fail the BUILD,
    # not surface as a ModuleNotFoundError partway through a paid run.
    import openhands.sdk.settings  # noqa: F401
    from openhands.sdk.mcp.config import MCPServer  # noqa: F401
    from openhands.sdk.llm.auth.openai import _get_current_codex_model_ids  # noqa: F401

    # The model call itself.
    import litellm  # noqa: F401
except Exception:
    traceback.print_exc()
    raise SystemExit(1)
PY
then
    echo "[scrub] FATAL: agent runtime requires '$DIST'; removing it breaks the" >&2
    echo "[scrub]        runner import chain.  Use version-matching (ORACLE_LEAK_FIX.md" >&2
    echo "[scrub]        Fix 2) instead of removal for this target." >&2
    exit 5
fi

# Prove it: no readable source copy of the target outside /src.  The task's own
# editable dist-info under the system interpreter is expected and allowed -- it
# carries no source and its RECORD holds no source hashes, so it is not an
# oracle.  What must not survive is an actual package directory.
LEAKS="$(find / -xdev \
    -path /src -prune -o \
    -path /proc -prune -o \
    -path "$VENV" -prune -o \
    -type d \( $(for n in $IMPORTS; do printf -- '-name %s -o ' "$n"; done) -false \) -print 2>/dev/null || true)"

REMAIN="$(for n in $IMPORTS; do
    find "$VENV" -maxdepth 4 -name "$n" -o -maxdepth 4 -name "${DIST}-*.dist-info" 2>/dev/null
done || true)"

if [ -n "$REMAIN" ]; then
    echo "[scrub] FATAL: '$DIST' still present in agent runtime after scrub:" >&2
    echo "$REMAIN" >&2
    exit 6
fi

if [ -n "$LEAKS" ]; then
    echo "[scrub] WARNING: source copies of $IMPORTS exist outside /src and the venv:"
    echo "$LEAKS"
    echo "[scrub] review these -- they may be a second oracle path."
fi

echo "[scrub] verified: no copy of '$DIST' in the agent runtime; runner chain intact"
