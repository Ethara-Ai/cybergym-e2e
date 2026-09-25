"""Shared harness command-line base (FORGE.md:184)."""
import argparse
import subprocess
import sys
from pathlib import Path

from . import registry


HARNESS_ROOT = Path(__file__).resolve().parent.parent


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level `harness` argparse tree."""
    ap = argparse.ArgumentParser(prog="harness", description="Trinity harness runner")
    subs = ap.add_subparsers(dest="cmd", required=True)

    sp_run = subs.add_parser("run", help="Run e2e (inference + evaluation) via the current runner")
    sp_run.add_argument("bundle", help="Path to a resident bundle directory")
    sp_run.add_argument("--benchmark", default="cybergym_e2e",
                        help="Benchmark plugin (default: cybergym_e2e)")
    sp_run.add_argument("extra_args", nargs=argparse.REMAINDER,
                        help="Passed through to the underlying runner")

    subs.add_parser("list", help="List registered benchmark plugins")

    sp_info = subs.add_parser("info", help="Show a benchmark's info")
    sp_info.add_argument("--benchmark", default="cybergym_e2e")

    return ap


def main(argv: list[str] | None = None) -> int:
    """Dispatch a `harness` subcommand."""
    args = build_parser().parse_args(argv)

    if args.cmd == "list":
        for name in registry.names():
            print(name)
        return 0

    if args.cmd == "info":
        b = registry.get(args.benchmark)
        print(f"name: {b.name}")
        print(f"class: {b.__class__.__name__}")
        print(f"module: {b.__class__.__module__}")
        return 0

    if args.cmd == "run":
        cmd = [sys.executable, str(HARNESS_ROOT / "run_harbor.py"), args.bundle]
        if args.extra_args:
            cmd.extend(a for a in args.extra_args if a != "--")
        return subprocess.call(cmd)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
