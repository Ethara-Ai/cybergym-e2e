"""CLI entry: ``python -m glm_bridge [--port 8790] [--check]`` (run from scripts/)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

from .bridge import SECRET_ENV, _main_model, _small_model, _upstream_base
from .credentials import CredentialProvider, CredentialsError, key_prefix


def _watch_parent(ppid: int, interval: float = 3.0) -> None:
    """Exit when the launching runner is gone (a SIGKILLed runner never reaches
    its atexit hook; without this the bridge would hold the port forever)."""
    def _loop() -> None:
        while True:
            time.sleep(interval)
            try:
                os.kill(ppid, 0)
            except ProcessLookupError:
                print(f"[glm-bridge] parent {ppid} is gone; exiting", file=sys.stderr, flush=True)
                os._exit(0)
            except PermissionError:
                pass
    threading.Thread(target=_loop, name="parent-watch", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m glm_bridge")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8790)
    p.add_argument("--log-level", default="info")
    p.add_argument("--check", action="store_true",
                   help="Verify a GLM API key is available, then exit.")
    p.add_argument("--parent-pid", type=int, default=0,
                   help="Exit automatically when this process disappears.")
    p.add_argument("--bridge-secret", default=os.environ.get(SECRET_ENV, ""),
                   help=f"Shared secret clients must present as ANTHROPIC_API_KEY (env {SECRET_ENV}). "
                        "Required unless binding loopback.")
    args = p.parse_args(argv)

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    provider = CredentialProvider()
    try:
        key = provider.get_api_key()
    except CredentialsError as e:
        print(f"[glm-bridge] credentials error: {e}", file=sys.stderr)
        return 2
    print(f"[glm-bridge] credentials OK (key prefix: {key_prefix(key)}; upstream {_upstream_base()}; "
          f"model {_main_model()}, small {_small_model()})")
    if args.check:
        return 0

    secret = (args.bridge_secret or "").strip()
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    if not secret and not loopback:
        print(f"[glm-bridge] refusing to bind {args.host} without --bridge-secret / {SECRET_ENV}: "
              "that would expose an unauthenticated proxy that spends this plan.", file=sys.stderr)
        return 3
    if secret:
        os.environ[SECRET_ENV] = secret

    import uvicorn
    from .bridge import build_app

    if args.parent_pid:
        _watch_parent(args.parent_pid)
    print(f"[glm-bridge] listening on http://{args.host}:{args.port}")
    print("[glm-bridge] point clients at:")
    print(f"           export ANTHROPIC_BASE_URL=http://{args.host}:{args.port}")
    print("           export ANTHROPIC_API_KEY=<the --bridge-secret value>" if secret else
          "           export ANTHROPIC_API_KEY=glm-stub   # loopback, UNAUTHENTICATED")
    print(f"           export ANTHROPIC_MODEL={_main_model()}")
    uvicorn.run(build_app(provider), host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
