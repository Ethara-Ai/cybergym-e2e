#!/bin/bash

# adapted from https://github.com/laude-institute/terminal-bench/blob/main/terminal_bench/agents/installed_agents/openhands/openhands-setup.sh.j2

# Update package manager
apt-get update

apt-get install -y curl git build-essential tmux

curl -LsSf https://astral.sh/uv/install.sh | sh

# Add uv to PATH for current session
source $HOME/.local/bin/env

# Install Python 3.13 using uv
uv python install 3.13

# Create a dedicated virtual environment for OpenHands
OPENHANDS_VENV="/opt/openhands-venv"
mkdir -p /opt
uv venv $OPENHANDS_VENV --python 3.13

# Activate the virtual environment and install OpenHands
source $OPENHANDS_VENV/bin/activate

# Set SKIP_VSCODE_BUILD to true to skip VSCode extension build for OpenHands
export SKIP_VSCODE_BUILD=true

# Use 1.0.0 which has Claude Opus 4.5 fix
# Staggered starts in batch_run.sh should avoid runtime contention issues
uv pip install --prerelease=allow openhands-ai==1.0.0

# Patch: drop the Jupyter plugin from CodeActAgent's sandbox_plugins. The
# Jupyter kernel gateway (ZeroMQ) hangs under amd64 QEMU emulation on arm64
# hosts (Apple Silicon), which kills LocalRuntime at startup. Bash execution
# (execute_bash) is sufficient for cybergym tasks; enable_jupyter=false (set in
# the agent env) removes the corresponding IPython tool so the agent won't emit
# unsupported ipython actions.
CODEACT="$OPENHANDS_VENV/lib/python3.13/site-packages/openhands/agenthub/codeact_agent/codeact_agent.py"
if [ -f "$CODEACT" ]; then
  sed -i '/JupyterRequirement(),/d' "$CODEACT"
fi

# Patch: strip `temperature` from Anthropic requests. Newer Claude models
# (including those served through a local Anthropic bridge) reject the
# `temperature` param with `invalid_request_error: "temperature" is deprecated
# for this model`. OpenHands always sends temperature=0.0, and litellm's
# drop_params only drops params it considers *unsupported* — it treats
# temperature as supported for Anthropic, so it never drops it and the request
# 400s. We install a startup monkeypatch into the venv (via a .pth import) that
# tells litellm's AnthropicConfig temperature is unsupported and pops it from
# the mapped params. Requires LLM_DROP_PARAMS=true (set in utils.get_llm_env).
SITE_PACKAGES=$(/opt/openhands-venv/bin/python -c 'import site,sys; sys.stdout.write(site.getsitepackages()[0])')
cat > "$SITE_PACKAGES/_cybergym_litellm_patch.py" << 'PYEOF'
"""Strip `temperature` from Anthropic completion requests (see install_openhands.sh)."""
import os
import sys


def _apply():
    try:
        import litellm  # noqa: F401
    except Exception:
        return False

    classes = []
    for path in (
        "litellm.llms.anthropic.chat.transformation",
        "litellm.llms.anthropic.completion.transformation",
    ):
        try:
            mod = __import__(path, fromlist=["AnthropicConfig"])
            cls = getattr(mod, "AnthropicConfig", None)
            if cls is not None:
                classes.append(cls)
        except Exception:
            pass
    try:
        cls = getattr(__import__("litellm", fromlist=["AnthropicConfig"]), "AnthropicConfig", None)
        if cls is not None and cls not in classes:
            classes.append(cls)
    except Exception:
        pass

    applied = False
    for cls in classes:
        # 1) Report temperature as unsupported so drop_params drops it.
        orig_supported = getattr(cls, "get_supported_openai_params", None)
        if orig_supported is not None and not getattr(orig_supported, "_cybergym", False):
            def _make_supported(orig):
                def patched(self, *a, **k):
                    params = orig(self, *a, **k)
                    try:
                        return [p for p in params if p != "temperature"]
                    except Exception:
                        return params
                patched._cybergym = True
                return patched
            try:
                cls.get_supported_openai_params = _make_supported(orig_supported)
                applied = True
            except Exception:
                pass

        # 2) Belt-and-suspenders: drop temperature during param mapping too, in
        #    case a litellm version passes it through outside the drop path.
        orig_map = getattr(cls, "map_openai_params", None)
        if orig_map is not None and not getattr(orig_map, "_cybergym", False):
            def _make_map(orig):
                def patched(self, *a, **k):
                    if "non_default_params" in k and isinstance(k["non_default_params"], dict):
                        k["non_default_params"].pop("temperature", None)
                    elif a and isinstance(a[0], dict):
                        a[0].pop("temperature", None)
                    result = orig(self, *a, **k)
                    if isinstance(result, dict):
                        result.pop("temperature", None)
                    return result
                patched._cybergym = True
                return patched
            try:
                cls.map_openai_params = _make_map(orig_map)
                applied = True
            except Exception:
                pass

    return applied


if os.environ.get("CYBERGYM_DROP_TEMPERATURE", "1") == "1":
    try:
        if _apply():
            print("[cybergym] litellm temperature-drop patch applied", file=sys.stderr)
    except Exception:
        pass
PYEOF

# `.pth` files with lines starting `import` are executed at interpreter startup,
# so the patch loads before OpenHands imports litellm. The zzz_ prefix orders it
# after other .pth files.
echo "import _cybergym_litellm_patch" > "$SITE_PACKAGES/zzz_cybergym_litellm_patch.pth"

echo "OpenHands patches applied (site-packages: $SITE_PACKAGES)"
