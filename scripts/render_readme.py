#!/usr/bin/env python3
"""render_readme.py: regenerate harness/README.md from the harness bytes.

trinity/FORGE.md:184 requires the harness README to be regenerated from the
harness code every run so it cannot drift from the implementation. This script
is the sole writer of that README. It introspects the harness's own bytes,
entry-point docstrings, the scripts directory, the lib directory, the projects
corpus, the env example, the gitignore, and any dependency pinning artifact,
and emits a deterministic Markdown file with a GENERATED banner. It reads no
network, no clock, and no random source, so two runs over the same tree
produce byte-identical output.

Usage from the harness root:

    python3 scripts/render_readme.py           # write README.md
    python3 scripts/render_readme.py --check   # exit non-zero on drift
"""

import ast
import hashlib
import sys
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[1]
README_PATH = HARNESS_ROOT / "README.md"
SELF_PATH = Path(__file__).resolve()

DASH_TABLE = str.maketrans({"\u2014": "-", "\u2013": "-", "\u2015": "-"})


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _normalize_prose(text: str) -> str:
    """Strip em-dashes and en-dashes; collapse hard line breaks to spaces."""
    return " ".join(text.translate(DASH_TABLE).split())


def _module_docstring_first_paragraph(path: Path) -> str:
    try:
        doc = ast.get_docstring(ast.parse(_read(path))) or ""
    except (SyntaxError, OSError, UnicodeDecodeError):
        return ""
    if not doc.strip():
        return ""
    first = doc.strip().split("\n\n", 1)[0]
    return _normalize_prose(first)


def _sh_header_comment(path: Path) -> str:
    """Return the first contiguous run of `# `-prefixed comments after the shebang."""
    try:
        raw = _read(path).splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    started = False
    collected: list[str] = []
    for line in raw:
        stripped = line.strip()
        if not started:
            if stripped.startswith("#!"):
                continue
            if not stripped:
                continue
            if stripped.startswith("#"):
                started = True
                collected.append(stripped.lstrip("#").strip())
                continue
            return ""
        else:
            if stripped.startswith("#"):
                collected.append(stripped.lstrip("#").strip())
            else:
                break
    if not collected:
        return ""
    return _normalize_prose(" ".join(c for c in collected if c))


def _list_top_level_py() -> list[Path]:
    return sorted(
        p for p in HARNESS_ROOT.glob("*.py")
        if p.is_file() and p.name != "__init__.py"
    )


def _list_scripts_entries() -> list[Path]:
    scripts = HARNESS_ROOT / "scripts"
    if not scripts.is_dir():
        return []
    entries = [p for p in scripts.iterdir() if p.name != "__pycache__"]
    return sorted(entries, key=lambda p: (p.is_dir(), p.name.lower()))


def _list_lib_entries() -> list[Path]:
    lib = HARNESS_ROOT / "lib"
    if not lib.is_dir():
        return []
    return sorted(p for p in lib.iterdir() if p.name != "__pycache__")


def _count_projects() -> int:
    projects = HARNESS_ROOT / "projects"
    if not projects.is_dir():
        return 0
    return sum(1 for p in projects.iterdir() if p.is_dir())


def _sample_projects(limit: int) -> list[str]:
    projects = HARNESS_ROOT / "projects"
    if not projects.is_dir():
        return []
    names = sorted(p.name for p in projects.iterdir() if p.is_dir())
    return names[:limit]


def _env_example_keys() -> list[str]:
    path = HARNESS_ROOT / ".env.example"
    if not path.is_file():
        return []
    keys: list[str] = []
    for line in _read(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        keys.append(s.split("=", 1)[0].strip())
    return keys


def _gitignore_patterns() -> list[str]:
    path = HARNESS_ROOT / ".gitignore"
    if not path.is_file():
        return []
    out: list[str] = []
    for line in _read(path).splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def _pinning_files() -> list[tuple[str, str]]:
    """Report presence and sha256 prefix of dependency pinning files."""
    candidates = [
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "harbor.lock",
        "poetry.lock",
        "Pipfile.lock",
        ".python-version",
    ]
    out: list[tuple[str, str]] = []
    for name in candidates:
        p = HARNESS_ROOT / name
        if p.is_file():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            out.append((name, digest))
    return out


def _config_manifests() -> list[tuple[str, str]]:
    """Report harness-level configuration manifests."""
    candidates = [
        "Dockerfile",
        ".dockerignore",
        "harness-config.json",
    ]
    out: list[tuple[str, str]] = []
    for name in candidates:
        p = HARNESS_ROOT / name
        if p.is_file():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            out.append((name, digest))
    return out


def _benchmark_dirs() -> list[tuple[str, str]]:
    """Report benchmarks/<name>/benchmark.toml presence."""
    root = HARNESS_ROOT / "benchmarks"
    if not root.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        cfg = child / "benchmark.toml"
        if cfg.is_file():
            digest = hashlib.sha256(cfg.read_bytes()).hexdigest()[:16]
            out.append((child.name, digest))
    return out


def _config_dir_entries() -> list[str]:
    """Report files under config/, sorted."""
    root = HARNESS_ROOT / "config"
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_file())


def _license_digest() -> str | None:
    p = HARNESS_ROOT / "LICENSE"
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16]


def _emitter_digest() -> str:
    return hashlib.sha256(SELF_PATH.read_bytes()).hexdigest()


def _describe_script_entry(path: Path) -> str:
    if path.is_dir():
        return f"`scripts/{path.name}/` (directory)"
    if path.suffix == ".py":
        summary = _module_docstring_first_paragraph(path) or "(no module docstring)"
        return f"`scripts/{path.name}`: {summary}"
    if path.suffix == ".sh":
        summary = _sh_header_comment(path) or "(no header comment)"
        return f"`scripts/{path.name}`: {summary}"
    return f"`scripts/{path.name}`"


def _describe_lib_entry(path: Path) -> str:
    if path.is_dir():
        return f"`lib/{path.name}/` (directory)"
    if path.suffix == ".py":
        summary = _module_docstring_first_paragraph(path) or "(no module docstring)"
        return f"`lib/{path.name}`: {summary}"
    return f"`lib/{path.name}`"


def render() -> str:
    lines: list[str] = []
    add = lines.append

    add("<!-- GENERATED SECTION. DO NOT HAND-EDIT. -->")
    add("<!-- This file is regenerated from the harness code by scripts/render_readme.py. -->")
    add("<!-- Any hand edit will be overwritten on the next run. -->")
    add("<!-- Regenerate with: python3 scripts/render_readme.py (from the harness root). -->")
    add(f"<!-- Emitter sha256: {_emitter_digest()} -->")
    add("")
    add("# harness")
    add("")
    add(
        "This is the Trinity `harness/` submodule, the benchmark extension an external runner uses to run inference and evaluation over resident task bundles under the parent project's `samples/` and `delivery/` lane roots. Per trinity/FORGE.md the harness is generated and reconciled by FORGE alone, is never executed by FORGE against a solver, delegates every scoring decision to each bundle's own `tests/` under a pinned Harbor release, and mounts neither `solution/` nor `trajectories/` for any path it exposes to the agent."
    )
    add("")
    add(
        "This README is generated. It reflects the bytes on disk at the moment `scripts/render_readme.py` was last run. If a section here disagrees with the code the code is correct and the README needs regenerating."
    )
    add("")

    add("## Docker host requirements")
    add("")
    add(
        "`--runner harbor` (the default) enforces the agent-phase egress allowlist with Harbor's nftables sidecar, so the docker daemon's kernel must carry `CONFIG_NFT_FIB_INET`. The harness probes for it and refuses to start rather than run an unisolated agent."
    )
    add("")
    add("| Host | Works with `--runner harbor` |")
    add("| --- | --- |")
    add("| Linux, stock docker | Yes. The canonical configuration `harness-config.json` is written against. |")
    add("| macOS + OrbStack | Yes. `docker context use orbstack`. |")
    add("| macOS + Colima | Yes. `colima start && docker context use colima`. |")
    add("| macOS + Docker Desktop | No. Its linuxkit kernel lacks the symbol; harbor refuses at startup. |")
    add("")
    add(
        "On a Mac the fix is another local daemon, not a Linux box: switch the docker context to OrbStack or Colima and the harbor runner works unchanged. `--runner legacy` is the deprecated raw-docker fallback that still isolates on Docker Desktop but only runs the `claude-code` agent (no `openhands-sdk`). `--no-lockdown` leaves the agent phase on the public network and is flagged in `summary.json`; it is for debugging and never for a run you intend to report."
    )
    add("")
    add(
        "Harbor's sidecar proxies every agent request through gost, whose stock 15s read timeout severs an in-flight LLM call once the prompt grows enough that time-to-first-byte exceeds it. `gost.yaml` ships inside the pinned Harbor wheel, so a fresh `uv sync` reinstates that default; the harness rewrites it at startup (`scripts/harbor_runner.py:ensure_gost_read_timeout`) and prints `Egress proxy: ...` when it does. Override the value with `KAKASHI_GOST_READ_TIMEOUT`."
    )
    add("")

    add("## Entry points")
    add("")
    tops = _list_top_level_py()
    if not tops:
        add("No top-level Python entry points detected.")
    else:
        for p in tops:
            summary = _module_docstring_first_paragraph(p) or "(no module docstring)"
            add(f"- `{p.name}`: {summary}")
    add("")

    add("## Scripts")
    add("")
    scripts = _list_scripts_entries()
    if not scripts:
        add("No `scripts/` directory found.")
    else:
        add("Contents of `scripts/`, sorted with files first and then subdirectories:")
        add("")
        for p in scripts:
            add(f"- {_describe_script_entry(p)}")
    add("")

    add("## Library modules")
    add("")
    libs = _list_lib_entries()
    if not libs:
        add("No `lib/` directory found.")
    else:
        for p in libs:
            add(f"- {_describe_lib_entry(p)}")
    add("")

    add("## Projects corpus")
    add("")
    count = _count_projects()
    if count == 0:
        add("No `projects/` directory found.")
    else:
        add(f"`projects/` holds {count} upstream-project subdirectories. First fifteen by name:")
        add("")
        for name in _sample_projects(15):
            add(f"- `projects/{name}/`")
        if count > 15:
            add(f"- ... and {count - 15} more")
    add("")

    add("## Environment variables")
    add("")
    keys = _env_example_keys()
    if not keys:
        add("No `.env.example` present or no keys defined.")
    else:
        add("Keys documented in `.env.example`:")
        add("")
        for k in keys:
            add(f"- `{k}`")
        add("")
        add(
            "Runtime credentials are read from `.env` (gitignored) and the real environment; a real `.env` never travels to `main`. See each entry point's docstring for the full behaviour of every variable it reads."
        )
    add("")

    add("## Runtime state")
    add("")
    ignored = _gitignore_patterns()
    if ignored:
        add(
            "The following patterns are gitignored per `.gitignore` and reflect runtime output the harness produces or consumes rather than tracked bytes:"
        )
        add("")
        for pat in ignored:
            add(f"- `{pat}`")
    else:
        add("No `.gitignore` patterns to report.")
    add("")

    add("## Dependency pinning")
    add("")
    pins = _pinning_files()
    if pins:
        add("Pinning artifacts present at the harness root:")
        add("")
        for name, digest in pins:
            add(f"- `{name}` (sha256 prefix `{digest}`)")
    else:
        add(
            "No dependency pinning artifact was found at the harness root. trinity/FORGE.md:184 requires every dependency of the benchmark extension to be pinned at an immutable revision, and the Harbor delivery block requires a `harbor.lock` binding one exact Harbor release. Neither `pyproject.toml`, `uv.lock`, `requirements.txt`, `poetry.lock`, `Pipfile.lock`, nor `harbor.lock` is present here. Treat this section as a coverage gap the next FORGE authoring run must close."
        )
    add("")

    add("## Configuration manifests")
    add("")
    manifests = _config_manifests()
    if manifests:
        add("Root-level configuration and image sources present at the harness root:")
        add("")
        for name, digest in manifests:
            add(f"- `{name}` (sha256 prefix `{digest}`)")
    else:
        add("No root-level configuration manifests detected.")
    add("")

    add("## Benchmarks")
    add("")
    benches = _benchmark_dirs()
    if benches:
        add("Self-describing benchmark directories under `benchmarks/`, per trinity/FORGE.md:184:")
        add("")
        for name, digest in benches:
            add(f"- `benchmarks/{name}/benchmark.toml` (sha256 prefix `{digest}`)")
    else:
        add("No `benchmarks/` directory found. trinity/FORGE.md:184 requires one self-describing directory per benchmark; this is a named coverage gap until scaffolded.")
    add("")

    add("## External configuration surface")
    add("")
    cfg_entries = _config_dir_entries()
    if cfg_entries:
        add("Files under `config/`, the externalization surface for the agent under test:")
        add("")
        for name in cfg_entries:
            add(f"- `config/{name}`")
    else:
        add("No `config/` directory found.")
    add("")

    add("## Contract boundaries")
    add("")
    add(
        "- FORGE alone reconciles bytes inside this submodule; any other instrument writing here is a scaffold defect that trinity/FORGE.md records as a named coverage gap."
    )
    add(
        "- Scoring is delegated from the rollout log to each bundle's own `tests/` under the pinned Harbor release. The extension carries no grading logic of its own that a bundle does not override."
    )
    add(
        "- Neither `solution/` nor `trajectories/` is mounted on any path this extension exposes to the agent under test, so the private-boundary leak gate holds."
    )
    add(
        "- FORGE never executes this extension against a solver. An author-side run is recorded as a `.seed/probe.yaml` entry, Bucket N and never difficulty evidence; every measured rollout comes from the out-of-band pilot runner."
    )
    add("- This file is regenerated every run; hand edits are overwritten.")
    add("")

    add("## License")
    add("")
    lic = _license_digest()
    if lic:
        add(f"See `LICENSE` (sha256 prefix `{lic}`). Bundle contents may carry their own upstream licenses recorded in each bundle's manifest.")
    else:
        add("No `LICENSE` file present at the harness root.")
    add("")

    add("## Regeneration")
    add("")
    add("Regenerate this README from the harness root:")
    add("")
    add("```")
    add("python3 scripts/render_readme.py")
    add("```")
    add("")
    add("Check for drift without writing:")
    add("")
    add("```")
    add("python3 scripts/render_readme.py --check")
    add("```")
    add("")
    add(
        "The emitter reads no network, no clock, and no random source; two runs over the same tree produce byte-identical output."
    )
    add("")

    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    check = "--check" in argv[1:]
    current = README_PATH.read_text(encoding="utf-8") if README_PATH.exists() else ""
    rendered = render()
    if check:
        if current == rendered:
            print("README.md is up to date.")
            return 0
        print(
            "README.md drift detected. Run: python3 scripts/render_readme.py",
            file=sys.stderr,
        )
        return 1
    README_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {README_PATH} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
