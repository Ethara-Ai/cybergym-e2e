"""CLI entry: ``python -m src.utils.claude_oauth [--port 8765] [--check]``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import uvicorn

from . import rotation_state
from .bridge import _resolve_provider, build_app
from .credentials import CredentialsError


def _watch_parent(ppid: int, interval: float = 3.0) -> None:
    """Terminate this bridge when the launching runner is gone.

    A runner killed with SIGKILL (or one that died inside a long docker build)
    never reaches its atexit hook, and a bridge started with --rm-less Popen
    then lives forever holding a port.  Polling the parent PID works on macOS
    and Linux alike (no PR_SET_PDEATHSIG needed).
    """
    import threading

    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                os.kill(ppid, 0)
            except ProcessLookupError:
                print(f"[bridge] parent {ppid} is gone; exiting", file=sys.stderr, flush=True)
                os._exit(0)
            except PermissionError:
                pass  # parent alive but not ours to signal

    threading.Thread(target=_loop, name="parent-watch", daemon=True).start()


def _dump_rate_limit_headers(provider) -> int:
    """Send one tiny request and report the rate-limit headers upstream returns.

    The cooldown durations this bridge publishes are only as good as the header
    names the classifier looks for, and those names are not documented -- they
    were derived from observed traffic. This makes verifying them a one-command
    job rather than an assumption. Costs ~1 output token.
    """
    import httpx

    from .bridge import (
        DEFAULT_ANTHROPIC_VERSION,
        SYSTEM_PREFIX,
        _build_forward_headers,
        _upstream_base,
    )
    from .errors import (
        _BUCKET_RESET_HEADERS,
        _UNIFIED_RESET_HEADERS,
        UNIFIED_CLAIM_HEADER,
        UNIFIED_STATUS_HEADER,
    )

    try:
        token = provider.get_access_token()
    except CredentialsError as e:
        print(f"[bridge] credentials error: {e}", file=sys.stderr)
        return 2

    headers = _build_forward_headers(
        {"content-type": "application/json", "anthropic-version": DEFAULT_ANTHROPIC_VERSION},
        token,
    )
    payload = {
        "model": os.environ.get("WCB_CC_PROBE_MODEL", "claude-sonnet-4-6"),
        "max_tokens": 1,
        "system": SYSTEM_PREFIX,
        "messages": [{"role": "user", "content": "hi"}],
    }

    url = f"{_upstream_base()}/v1/messages"
    print(f"[bridge] probing {url} (model={payload['model']}, max_tokens=1)")
    try:
        r = httpx.post(url, json=payload, headers=headers, timeout=60)
    except httpx.HTTPError as e:
        print(f"[bridge] request failed: {e}", file=sys.stderr)
        return 2

    print(f"[bridge] HTTP {r.status_code}\n")

    observed = {
        k.lower(): v for k, v in r.headers.items()
        if k.lower().startswith("anthropic-ratelimit") or k.lower() == "retry-after"
    }
    if not observed:
        print("  (no rate-limit headers returned on this response)")
    else:
        print("  Headers returned by Anthropic:")
        for k in sorted(observed):
            print(f"    {k} = {observed[k]}")

    expected = {
        "subscription window reset": list(_UNIFIED_RESET_HEADERS),
        "account-level status": [UNIFIED_STATUS_HEADER, UNIFIED_CLAIM_HEADER],
        "per-bucket reset": list(_BUCKET_RESET_HEADERS),
    }
    print("\n  Names the classifier reads:")
    missing_critical = []
    for group, names in expected.items():
        for name in names:
            mark = "FOUND  " if name in observed else "absent "
            print(f"    [{mark}] {name}   ({group})")
            if group != "per-bucket reset" and name not in observed:
                missing_critical.append(name)

    known = {n for names in expected.values() for n in names}
    # Informational on a live response; deliberately not read by the classifier.
    #   *-utilization / *-limit / *-remaining  -> how full the window is
    #   *-overage-*                            -> org billing config; reads
    #                                             "rejected" on healthy accounts
    benign = ("-limit", "-remaining", "-utilization", "-percentage")
    unknown = [
        k for k in observed
        if k != "retry-after" and k not in known
        and not k.endswith(benign) and "-overage-" not in k
        and not k.endswith("-status")  # per-window statuses are handled
    ]
    if unknown:
        print("\n  !! Unrecognised reset/status headers -- the classifier ignores these:")
        for k in sorted(unknown):
            print(f"       {k} = {observed[k]}")
        print("     If one of these is the real reset, a cap will fall back to "
              "the 300s default. Teach errors.py about it.")

    print()
    if missing_critical:
        print("  VERDICT: some subscription headers were absent. They are normally "
              "present on every response, so check for a rename.")
        return 1
    print("  VERDICT: every header the classifier depends on is present. "
          "A real cap will cool until its true reset.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m src.utils.claude_oauth")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--log-level", default="info")
    p.add_argument(
        "--check",
        action="store_true",
        help="Verify credentials load successfully (without refreshing), then exit.",
    )
    p.add_argument(
        "--pool-status",
        action="store_true",
        help="Print the shared rotation state (which accounts are cold, until when), then exit.",
    )
    p.add_argument(
        "--clear-cooldowns",
        action="store_true",
        help="Re-enable every account after a rate-limit storm, then exit. "
             "Clears cooldowns AND invalid flags machine-wide.",
    )
    p.add_argument(
        "--dump-rate-limit-headers",
        action="store_true",
        help="Send one minimal request upstream and print the rate-limit headers "
             "Anthropic actually returns, cross-checked against the names the "
             "classifier reads. Use after a Claude CLI update to catch renames.",
    )
    p.add_argument(
        "--parent-pid", type=int, default=0,
        help="Exit automatically when this process disappears (prevents orphaned bridges).",
    )
    p.add_argument(
        "--bridge-secret",
        default=os.environ.get("WCB_CC_BRIDGE_SECRET", ""),
        help="Shared secret clients must present as ANTHROPIC_API_KEY "
             "(env WCB_CC_BRIDGE_SECRET). Required unless binding loopback.",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Operator commands run before provider resolution on purpose: they must
    # work when credentials are broken, which is usually why you are running
    # them.
    if args.clear_cooldowns:
        cleared = rotation_state.clear_cooldowns()
        print(f"[bridge] cleared rotation state for {cleared} account(s) "
              f"in {rotation_state.state_path()}")
        return 0

    if args.pool_status:
        rows = rotation_state.snapshot()
        if not rows:
            print(f"[bridge] no accounts in {rotation_state.state_path()} "
                  "(set WCB_CC_ACCOUNT_POOL and run the bridge once)")
            return 0
        now = time.time()
        print(f"[bridge] rotation state: {rotation_state.state_path()}")
        for row in rows:
            if row["invalid"]:
                status = "INVALID (needs re-login)"
            elif row["available"]:
                status = "available"
            else:
                status = f"cooling for {max(0.0, row['cooldown_until'] - now):.0f}s"
            fails = row["failure_count"]
            streak = f"  ({fails} consecutive failure{'s' if fails != 1 else ''})" if fails else ""
            print(f"  {row['label']:<40} {status}{streak}")
            if row["last_reason"]:
                print(f"  {'':<40}   last: {row['last_reason']}")
        return 0

    # Honor WCB_CC_ACCOUNT_POOL if present (multi-account failover);
    # otherwise falls through to single default CredentialProvider.
    provider = _resolve_provider()

    if args.dump_rate_limit_headers:
        return _dump_rate_limit_headers(provider)

    if args.check:
        # Preflight must not consume the (single-use) refresh token: a
        # throwaway --check process that refreshed would rotate the token and
        # leave the real bridge with a dead one.  Load and inspect only.
        try:
            peek = getattr(provider, "peek", None)
            if peek is None:
                # Account pools have no refresh-free inspection; a real token
                # fetch is the only honest check (it may refresh a slot).
                token = provider.get_access_token()
                print(f"[bridge] credentials OK (token prefix: {token[:15]}..., pool)")
                return 0
            creds = peek()
        except CredentialsError as e:
            print(f"[bridge] credentials error: {e}", file=sys.stderr)
            return 2
        remaining = int(creds.expires_at_ms / 1000 - time.time())
        state = "expired; will refresh at first use" if creds.is_expired() else f"valid for {remaining}s"
        print(f"[bridge] credentials OK (token prefix: {creds.access_token[:15]}..., {state})")
        return 0

    secret = (args.bridge_secret or "").strip()
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not secret and not loopback:
        print(f"[bridge] refusing to bind {args.host} without --bridge-secret / "
              "WCB_CC_BRIDGE_SECRET: that would expose an unauthenticated proxy that "
              "spends this subscription.", file=sys.stderr)
        return 3
    if secret:
        os.environ["WCB_CC_BRIDGE_SECRET"] = secret

    try:
        token = provider.get_access_token()
    except CredentialsError as e:
        print(f"[bridge] credentials error: {e}", file=sys.stderr)
        return 2
    print(f"[bridge] credentials OK (token prefix: {token[:15]}...)")

    if args.parent_pid:
        _watch_parent(args.parent_pid)
    print(f"[bridge] listening on http://{args.host}:{args.port}")
    print("[bridge] point clients at:")
    print(f"           export ANTHROPIC_BASE_URL=http://{args.host}:{args.port}")
    if secret:
        print("           export ANTHROPIC_API_KEY=<the --bridge-secret value>")
    else:
        print("           export ANTHROPIC_API_KEY=kaiju-cc-stub   # loopback, UNAUTHENTICATED")
    uvicorn.run(
        build_app(provider),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
