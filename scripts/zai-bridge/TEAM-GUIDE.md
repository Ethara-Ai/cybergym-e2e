# Team Guide — Z.ai (GLM) Auth Bridge for Claude Code

> Use your **Z.ai GLM Coding Plan subscription** (Lite / Pro / Max) inside **Claude Code** with a
> browser sign-in — no manual API-key copy/paste, no proxy, no config-file editing.
>
> `claude`  → your normal Anthropic/Claude subscription (untouched)
> `glm`     → same Claude Code UI, powered by your Z.ai GLM subscription quota

This document is for **teams adopting the bridge**. Share it as-is. Each developer follows the
"Developer Setup" section once (~2 minutes) and is done.

---

## 1. What this is (and why)

Z.ai runs an official **Anthropic-compatible API endpoint**:

```
https://api.z.ai/api/anthropic
```

Claude Code can talk to it natively — the only things needed are two environment variables
(`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`). The hard part is the token: normally you must log
in to the Z.ai dashboard, create an API key by hand, and paste it around.

**This bridge removes that step.** It implements the same OAuth browser-login flow that Z.ai's own
ZCode app uses:

1. `glm login` opens your browser → you sign in to Z.ai and click *Approve*.
2. The tool exchanges the OAuth code for a session, then **auto-creates (or reuses) an API key**
   named `glm-bridge` in your Z.ai account — you never see or touch the key.
3. The key is stored at `~/.zai_api_key` (permissions `600`, your user only).
4. `glm` launches Claude Code with the right env vars pointing at Z.ai.

Result: the exact same login UX as `claude /login` or Codex's ChatGPT sign-in, but for GLM.

### Why a separate `glm` command instead of editing `~/.claude/settings.json`?

Putting `ANTHROPIC_BASE_URL` into `settings.json` hijacks **every** Claude Code session on the
machine. The wrapper approach keeps both worlds side by side:

| Command | Bills | Models |
|---|---|---|
| `claude` | Your Anthropic subscription (OAuth, unchanged) | Claude Opus / Sonnet / Haiku |
| `glm` | Your Z.ai GLM Coding Plan quota | GLM 4.x / 5.x (mapped server-side) |

---

## 2. Prerequisites

Each developer needs:

- **A Z.ai GLM Coding Plan subscription** (Individual plan — Lite/Pro/Max). Sign up at
  <https://z.ai>. *Team-plan keys are a separate credential class and are not interchangeable
  with individual-plan keys.*
- **Claude Code** installed: `npm install -g @anthropic-ai/claude-code`
- **Python 3** (stock macOS/Linux python3 is fine — the login script is stdlib-only, zero pip
  installs)
- macOS or Linux, `bash` or `zsh`

---

## 3. Developer Setup (one time, ~2 minutes)

```bash
# 1. Get the repo (or just the zai-bridge/ folder)
git clone <this-repo>
cd <this-repo>/zai-bridge

# 2. Install → copies `glm` and `glm-login` to ~/.local/bin
./install.sh

# 3. Sign in (opens your browser)
glm login

# 4. Use it
glm                      # launches Claude Code on your Z.ai plan
```

That's it. The login is **one-time** — the minted key is durable, so you won't be asked again
unless you run `glm logout` or revoke the key in the Z.ai dashboard.

### All commands

| Command | What it does |
|---|---|
| `glm login` | Browser OAuth sign-in to Z.ai; auto-mints & stores the API key |
| `glm` | Start Claude Code routed through Z.ai (any extra args pass through, e.g. `glm -c`) |
| `glm logout` | Delete the local credential (`~/.zai_api_key`) |
| `claude` | Unchanged — your regular Claude subscription |

---

## 4. How the login flow works (for reviewers)

No secrets are embedded anywhere; the flow is standard OAuth 2.0 authorization-code (as used by
Z.ai's own ZCode CLI — public client, no client secret, localhost callback):

```
┌──────────┐  1. open browser: chat.z.ai/api/oauth/authorize        ┌────────┐
│ glm login │ ────────────────────────────────────────────────────▶ │  You   │
│ (local)   │                    (state = 32 random hex, CSRF)      │ log in │
└─────┬────┘                                                        └───┬────┘
      │      2. redirect → http://127.0.0.1:&lt;port&gt;/callback?code=…&state=…│
      │◀────────────────────────────────────────────────────────────────┘
      │  3. verify state matches, then POST zcode.z.ai/api/v1/oauth/token
      │       → short-lived OAuth access token
      │  4. POST api.z.ai/api/auth/z/login  → business session JWT
      │  5. GET  …/getCustomerInfo → default org + project
      │     GET/POST …/api_keys    → find-or-create key named "glm-bridge"
      │     GET  …/api_keys/copy/… → full secret
      │  6. save "<id>.<secret>" → ~/.zai_api_key  (chmod 600) → live-verify
      ▼
   Done. `glm` now exports:
     ANTHROPIC_AUTH_TOKEN = <key from ~/.zai_api_key>
     ANTHROPIC_BASE_URL   = https://api.z.ai/api/anthropic
     API_TIMEOUT_MS       = 3000000
   …and exec's `claude`.
```

Details worth knowing:

- **Callback uses any free local port (`127.0.0.1:<port>`).** The script starts a temporary
  HTTP server on a random free loopback port and registers `http://127.0.0.1:<port>/callback`
  as the redirect URI. Z.ai accepts any `http://127.0.0.1` loopback redirect for the CLI client
  (it is not tied to a fixed port or a custom scheme). On a headless box, the script falls back
  to *paste mode*: open the printed URL anywhere, sign in, and paste the final
  `http://127.0.0.1:<port>/callback?...` URL back into the terminal.
- **`ANTHROPIC_AUTH_TOKEN`, not `ANTHROPIC_API_KEY`.** Claude Code shows a scary approval prompt
  for `ANTHROPIC_API_KEY` (and remembers a "no" forever). The wrapper also actively `unset`s any
  stale `ANTHROPIC_API_KEY` to avoid auth conflicts.
- The auth code from the browser expires in ~1–5 minutes; if you dawdle, just run `glm login`
  again.

---

## 5. Verifying it works

Inside a `glm` session, run:

```
/status
```

You should see `ANTHROPIC_BASE_URL = https://api.z.ai/api/anthropic` and the auth-token
credential. Or from the shell:

```bash
curl -s https://api.z.ai/api/anthropic/v1/messages \
  -H "x-api-key: $(cat ~/.zai_api_key)" \
  -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"claude-sonnet-4-20250514","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}'
```

The response's `"model"` field will show a **GLM** model — proof the mapping works. (`glm login`
already performs this exact check automatically at the end.)

---

## 6. Model mapping & tuning (optional)

Z.ai maps Claude model names → GLM models **server-side** (opus/sonnet → current best GLM, haiku →
a light model). Recommendation: **don't pin models**, so you auto-upgrade when Z.ai promotes new
GLM releases.

If you must pin, export before running `glm` (or add to the wrapper):

```bash
export ANTHROPIC_DEFAULT_SONNET_MODEL="glm-4.7"
export ANTHROPIC_DEFAULT_OPUS_MODEL="glm-4.7"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="glm-4.5-air"
```

1M-context variants exist with a `[1m]` suffix (e.g. `glm-4.7[1m]`); pair with
`CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000`.

---

## 7. Bonus: same subscription in OpenCode (automatic)

**No extra steps needed.** `glm login` detects an OpenCode installation and
connects its built-in `zai-coding-plan` provider automatically — it writes the
minted key into OpenCode's credential store
(`~/.local/share/opencode/auth.json`, or `$XDG_DATA_HOME/opencode/auth.json`),
preserving all your other providers.

After logging in:

1. Restart opencode (if it was running).
2. `/models` → pick any `zai-coding-plan/glm-*` model.

Same quota as `glm`, correct coding endpoint automatically. `glm logout`
disconnects the provider again (removes only the `zai-coding-plan` entry).

**Manual fallback** (if auto-connect ever fails):

```bash
opencode auth login       # or /connect inside opencode
# choose:  Z.AI Coding Plan   (NOT plain "Z.AI")
# paste:   cat ~/.zai_api_key
```

---

## 8. Troubleshooting

| Symptom | Cause → Fix |
|---|---|
| `Error 1113: Insufficient Balance` | No **active** Coding Plan on the account you logged in with, or wrong endpoint. Check plan at <https://z.ai>; the bridge already uses the correct `/api/anthropic` endpoint. |
| Browser shows "site can't be reached" after approving | The local callback server isn't reachable (firewall, remote/SSH session). Copy the full `http://127.0.0.1:<port>/callback?...` URL from the address bar and paste it into the waiting `glm login` prompt. |
| `Redirect URI not registered` | Old bug — the client registers loopback redirects (`http://127.0.0.1/callback`, any port), **not** a fixed port and **not** a `zcode://` scheme. Already fixed in `glm-login`; if it recurs, re-sync the OAuth flow from the ZCode reference (github.com/vibe-coding-labs/zcode-reverse-engineer). |
| HTTP 500 / `code 2007` during login | Auth code expired (1–5 min TTL). Re-run `glm login`. |
| Claude Code asks to approve an API key / auth errors | A stale `ANTHROPIC_API_KEY` export in your shell profile. Remove it; the wrapper unsets it per-session, but your other terminals may still have it. |
| 429s / model refuses mid-day | You hit the plan quota window. Quota refreshes on **5-hour rolling windows** and never bills overages — wait or upgrade the plan. |
| Works for one dev, not another | Each dev logs in with **their own** Z.ai account/subscription. The minted key lives per-machine in `~/.zai_api_key`. Keys are personal — never share or commit them. |
| Want to start fresh | `glm logout`, optionally revoke the `glm-bridge` key at <https://z.ai/manage-apikey/apikey-list>, then `glm login`. |

---

## 9. Security review notes

- **Stdlib only.** `glm-login` is a single Python 3 file: `http.server`, `urllib`, `secrets`,
  `webbrowser`. No pip packages, no curl-pipe-bash, nothing to supply-chain.
- **Talks only to `*.z.ai`** (`chat.z.ai`, `zcode.z.ai`, `api.z.ai`). No third-party relay or
  proxy ever sees your credentials.
- **CSRF-protected**: the OAuth `state` is 32 random hex chars and verified before the code is
  exchanged (also in paste-fallback mode).
- **Localhost-only callback**: the temporary HTTP server binds `127.0.0.1:<free port>` and lives
  only until the redirect arrives (max 5 min).
- **Credential storage**: `~/.zai_api_key`, `chmod 600`, outside any repo. Revocable any time
  from the Z.ai dashboard (key name: `glm-bridge`).
- The wrapper never writes to `~/.claude/settings.json`; your Anthropic login is untouched.

---

## 10. Known caveats

- Anthropic does not officially support pointing Claude Code at third-party backends. A Claude
  Code update could temporarily break compatibility; Z.ai historically patches within days. If
  `glm` breaks after an update, check <https://docs.z.ai/devpack/tool/claude> for the current
  guidance — the bridge only sets env vars, so fixes are usually trivial.
- The OAuth endpoints (`chat.z.ai` / `zcode.z.ai`) are the ones Z.ai's own ZCode client uses but
  are not formally documented as a public API. If Z.ai ever changes them, fall back to the manual
  path: create a key at <https://z.ai/manage-apikey/apikey-list> and save it yourself:
  `echo 'KEY' > ~/.zai_api_key && chmod 600 ~/.zai_api_key` — everything else keeps working.

---

## 11. Rollout checklist for team leads

- [ ] Confirm every dev has an **Individual GLM Coding Plan** (or budget for one).
- [ ] Share this repo / the `zai-bridge/` folder.
- [ ] Each dev: `./install.sh` → `glm login` → `glm`.
- [ ] Verify with `/status` (Section 5).
- [ ] Add `~/.zai_api_key` to your org's "never commit" awareness (it lives outside repos, but
      belt-and-suspenders).
- [ ] Optional: standardize model pinning (Section 6) or leave defaults (recommended).

**File inventory** (all in `zai-bridge/`):

| File | Purpose |
|---|---|
| `glm` | Bash wrapper — env setup + `exec claude`, `login`/`logout` subcommands |
| `glm-login` | Python 3 OAuth login + key auto-mint (stdlib only) |
| `install.sh` | Copies both to `~/.local/bin`, checks prerequisites |
| `README.md` | Short component README |
| `TEAM-GUIDE.md` | This document |
