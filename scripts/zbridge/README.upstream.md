# zbridge

Anthropic-shaped local proxy that lets a trajectory harness (or any Anthropic-SDK client) generate trajectories on z.ai's **GLM Coding Plan** (GLM-5.3) with only a base-URL change.

The only z.ai path in this repo — it replaced `zai-bridge/`, which pointed the *Claude
Code CLI* at z.ai and was removed. See [PLAN.md](./PLAN.md) for the full design,
translation contract, and rollout.

## Status

**Implemented and wired into the harness.** 145 tests pass under `tests/unit` +
`tests/integration`; `./harness.py doctor` runs a live GLM-5.3 round trip through it.

## Use it from the harness (the normal path)

`./harness.py run <task> --engine glm` starts this bridge automatically and points
terminus-2 at it — no manual startup, no env vars to export:

```bash
./harness.py login --glm                     # once: mints ~/.zai_api_key
./harness.py run <task-id> --engine glm      # GLM-5.3 through zbridge
./harness.py doctor                          # live round trip, PASS/FAIL
```

The harness picks a free port from 8766, hands the child its z.ai key through the
environment only, and writes the child's log to `harness/output/zbridge.log`. Runs land
under `harness/output/<task>/glm-5.3/run_<n>/`. `--glm-direct` bypasses this bridge and
calls z.ai's own Anthropic-compat endpoint instead, for comparing the two translations.

See [`../harness.py`](../harness.py) — `ZBridge`, `bridge_kind()`, `_engine_config()` —
and `harness/tests/test_harness_zbridge.py` for the wiring and its tests.

## Standalone quickstart

Start the bridge:

```bash
export ZB_ZAI_API_KEY=your-zai-key
export ZB_BRIDGE_SECRET=your-local-secret
python -m zbridge --host 127.0.0.1 --port 8766
```

### Point any Anthropic client at it

```bash
export ANTHROPIC_API_BASE=http://127.0.0.1:8766
export ANTHROPIC_API_KEY=$ZB_BRIDGE_SECRET
```

### Full-runnable curl (non-stream)

```bash
curl -sS http://127.0.0.1:8766/v1/messages \
  -H "content-type: application/json" \
  -H "x-zbridge-secret: $ZB_BRIDGE_SECRET" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": 128,
        "messages": [{"role":"user","content":"Say hi in one word."}]
      }' \
  | jq .
```

Expected: Anthropic-shape JSON with `content:[{"type":"text","text":"Hi"}], stop_reason:"end_turn", usage:{...}` backed by a real GLM-5.3 completion.

### Streaming (SSE)

```bash
curl -N -sS http://127.0.0.1:8766/v1/messages \
  -H "content-type: application/json" \
  -H "x-zbridge-secret: $ZB_BRIDGE_SECRET" \
  -H "accept: text/event-stream" \
  -d '{
        "model": "claude-3-5-sonnet-latest",
        "max_tokens": 128,
        "stream": true,
        "thinking": {"type":"enabled","budget_tokens":2048},
        "messages": [{"role":"user","content":"Explain in one sentence why the sky is blue."}]
      }'
```

Expected first event lines:

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant",...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking",...}}
```

### Health

```bash
curl -sS http://127.0.0.1:8766/healthz | jq .
# {"ok": true, "upstream": "api.z.ai", "model_default": "glm-5.3"}
```

## What lives where

| Path | Purpose |
|---|---|
| [PLAN.md](./PLAN.md) | Full work plan: goal, architecture, translation tables, error map, 25-task waves, 19 TDD scenarios |
| `zbridge/` | Source |
| `tests/` | pytest tree: `unit/`, `integration/` (respx), `smoke/` (real z.ai) |
| `docs/` | Frozen contracts (ARCHITECTURE, TRANSLATION, ERRORS, RUNBOOK) |
| `Dockerfile.zbridge` | slim py3.12, port 8766 |

## Design in one line

Expose Anthropic `/v1/messages` on `127.0.0.1:8766`; translate every request, response, and SSE event to and from z.ai's OpenAI-compat GLM Coding Plan endpoint at `https://api.z.ai/api/coding/paas/v4/chat/completions`. Buffer-and-retry streaming default-on so mid-stream drops are transparent.

## Data residency

**All traffic hits Chinese infrastructure (z.ai).** Do not point trajectories containing confidential or regulated data at this bridge. See `docs/RUNBOOK.md` for details.
