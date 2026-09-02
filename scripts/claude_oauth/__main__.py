"""CLI entry: ``python -m src.utils.claude_oauth [--port 8765] [--check]``."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import uvicorn

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

    # Honor WCB_CC_ACCOUNT_POOL if present (multi-account failover);
    # otherwise falls through to single default CredentialProvider.
    provider = _resolve_provider()

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
