# zai-bridge — Claude Code on your Z.ai GLM Coding Plan

Use your **Z.ai GLM Coding Plan subscription** (Lite/Pro/Max) inside **Claude Code**
with a browser sign-in — no manual API key copy/pasting, just like `claude /login`.

> 📖 Rolling this out to a team? Read **[TEAM-GUIDE.md](TEAM-GUIDE.md)** — full
> onboarding, flow diagram, verification, troubleshooting, and a lead checklist.

```
glm login --paste-key   ✅ EASIEST: paste an API key from the Z.ai dashboard (one time)
glm login               🌐 browser sign-in (OAuth) — optional alternative
glm                     🥷 Claude Code, powered by GLM on your Z.ai plan
glm logout              👋 remove the stored credential
claude                  ✅ your normal Anthropic subscription — completely untouched
```

## Install

```bash
./install.sh              # copies glm + glm-login into ~/.local/bin
glm login --paste-key     # opens the Z.ai key page → create a key → paste it
glm                       # go!
```

### Getting your API key (the reliable way everyone uses)

1. Sign in at **https://z.ai/manage-apikey/apikey-list**
2. Click **“+ Create a new API key”** and copy it (looks like `id.secret`)
3. Run `glm login --paste-key` and paste it at the prompt

That's it — this is exactly how the Z.ai GLM Coding Plan is meant to be used with
Claude Code. The browser OAuth flow (`glm login`) still works as an alternative.

Requirements: `python3` (stdlib only, no pip packages), `claude` (Claude Code), macOS or Linux.

## How it works

1. `glm login` opens Z.ai's official OAuth page (`chat.z.ai`) — the same
   authorization-code flow Z.ai's own ZCode CLI uses (client id
   `client_P8X5CMWmlaRO9gyO-KSqtg`, callback `http://127.0.0.1:<port>/callback`
   on any free local port).
2. A tiny localhost server catches the redirect; the `state` parameter is
   verified (CSRF protection). If the redirect can't reach the local server,
   you can paste the callback URL instead.
3. The login code is exchanged for tokens (`zcode.z.ai/api/v1/oauth/token`
   → `api.z.ai/api/auth/z/login`).
4. The script finds your default org/project and **auto-creates (or reuses) a
   Coding Plan API key named `glm-bridge`** — you never see or handle it.
5. The credential is stored in `~/.zai_api_key` (chmod `600`) and verified with
   one tiny live request against `https://api.z.ai/api/anthropic/v1/messages`.
6. `glm` then launches `claude` with:
   - `ANTHROPIC_AUTH_TOKEN` = stored credential (never `ANTHROPIC_API_KEY` —
     that variant triggers a trust prompt in Claude Code)
   - `ANTHROPIC_BASE_URL`  = `https://api.z.ai/api/anthropic`
   - `API_TIMEOUT_MS`      = `3000000`

Because env vars are scoped to the `glm` process only, your regular `claude`
command keeps using your Anthropic subscription OAuth as-is. Run both side by side.

## Notes & troubleshooting

- **Login is one-time.** The minted credential is durable (same class as keys
  created in the Z.ai dashboard). Re-run `glm login` only after `glm logout`
  or if you revoke the `glm-bridge` key at https://z.ai/manage-apikey/apikey-list
- **Quota** comes from your Coding Plan (5-hour rolling windows). It never
  charges beyond the subscription.
- **Error 1113 "Insufficient Balance"** at the Anthropic endpoint means the
  account has no active Coding Plan package — subscribe at z.ai first.
- **Team plans**: keys/quota are separate from individual plans.
- **OpenCode users**: `glm login` connects the OpenCode TUI automatically
  (it registers the built-in `zai-coding-plan` provider in OpenCode's
  `auth.json`). Restart opencode and pick a `zai-coding-plan/glm-*` model
  via `/models`. `glm logout` disconnects it again. Manual fallback:
  `opencode auth login` → "Z.AI Coding Plan" → paste `cat ~/.zai_api_key`.
- Model mapping (opus/sonnet/haiku → GLM tiers) happens server-side at Z.ai, so
  you automatically get upgraded defaults. To pin models explicitly, export
  `ANTHROPIC_DEFAULT_SONNET_MODEL=glm-4.7` (etc.) before running `glm`.

## Security

- Stdlib-only Python; every request goes directly to `*.z.ai` — no third-party
  servers involved.
- OAuth `state` is verified; callback binds to `127.0.0.1` only.
- Credential file is user-read-only (`600`) and lives outside any repo.
