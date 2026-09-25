#!/usr/bin/env bash
# Host-side deps for the three relay bridges: the Claude Code subscription
# bridge (scripts/claude_oauth), the codex judge bridge (scripts/codex_oauth)
# and the Z.ai Coding Plan bridge (scripts/zbridge, started by glm_bridge.py).
#
# They all run on the HOST (not in the task container) using the same Python
# that runs run_harbor.py, so install these into that interpreter. httpx is
# usually already present; fastapi + uvicorn power the Anthropic-compatible
# proxy servers.  For a full harness install prefer `uv sync` at the harness
# root against pyproject.toml + uv.lock; this script is the pip fallback for
# environments where uv is not available.
set -eux

PYTHON="${PYTHON:-python3}"
"$PYTHON" -m pip install --upgrade "fastapi>=0.110" "uvicorn>=0.29" "httpx>=0.27"

echo "Bridge deps installed. Verify Claude Code subscription creds with:"
echo "  $PYTHON -m claude_oauth --check   # run from the scripts/ dir"
