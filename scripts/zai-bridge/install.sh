#!/usr/bin/env bash
# Installs the Z.ai <-> Claude Code bridge: copies `glm` + `glm-login`
# into ~/.local/bin and makes them executable.
set -euo pipefail

SCRIPT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
BIN_DIR="$HOME/.local/bin"

command -v python3 >/dev/null 2>&1 || { echo "❌ python3 is required"; exit 1; }
command -v claude  >/dev/null 2>&1 || echo "⚠️  'claude' (Claude Code) not found on PATH — install it first: npm i -g @anthropic-ai/claude-code"

mkdir -p "$BIN_DIR"
install -m 755 "$SCRIPT_DIR/glm"       "$BIN_DIR/glm"
install -m 755 "$SCRIPT_DIR/glm-login" "$BIN_DIR/glm-login"

echo "✅ Installed: $BIN_DIR/glm  and  $BIN_DIR/glm-login"

if ! command -v glm >/dev/null 2>&1; then
  echo ""
  echo "⚠️  $BIN_DIR is not on your PATH. Add this to your shell rc file:"
  echo '    export PATH="$HOME/.local/bin:$PATH"'
fi

echo ""
echo "Next steps:"
echo "  1. glm login    # browser sign-in to your Z.ai account"
echo "  2. glm          # Claude Code running on your Z.ai Coding Plan"
