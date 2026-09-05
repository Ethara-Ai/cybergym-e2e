# Vendored zbridge

Source: `Devops-projects/devops-projects/zbridge/zbridge` (git `30d0425`,
plus the SSE usage fix described below).  Kept pristine so it can be
re-synced from upstream; kakashi-specific lifecycle code lives in
`scripts/glm_bridge.py`, not in here.

Why it is here: `--model-provider glm` used to point Claude Code straight at
`https://api.z.ai/api/anthropic` (z.ai's own Anthropic shim).  That endpoint
answers, but its streamed `message_start` carries `{"input_tokens": 0,
"output_tokens": 0}`, so every per-step token count in `trajectory.json`
landed as 0.  zbridge translates Anthropic <-> z.ai's OpenAI-compat Coding
Plan schema in a translation this repo owns and tests, and reports usage
through `translate.map_usage`.

Upstream `--glm-direct` keeps the old direct path for comparing the two.
