# Plan: parallel pass@k trajectories in `run_harbor.py`

**Author:** kakashi/harness
**Scope:** `harness/run_harbor.py`, `harness/scripts/glm_bridge.py`, `harness/scripts/claude_oauth/`, `harness/scripts/codex_oauth/`, `harness/scripts/deliverables.py`
**Out of scope:** legacy runner (`--runner legacy`, deprecated per README), `batch_run.sh` (deprecated), FORGE reconciliation, harbor.lock repin, judge container concurrency.

---

## 1. Problem

`run_pass_at_k()` at [`run_harbor.py:2423`](../../run_harbor.py) runs K samples **sequentially**:

```python
# run_harbor.py:2442-2450 (current)
for i in range(1, args.pass_at_k + 1):
    ...
    child = subprocess.Popen(cmd)
    exits.append(child.wait())     # <-- blocks; K runs happen back-to-back
```

At `--pass-at-k 8` with a 90-minute per-trial wall-clock cap
([`harness-config.json:23`](../../harness-config.json) → `wallClockTimeoutSeconds: 5400`)
this is up to **12 hours of wall-clock for one task**. The docker host has spare
cores; the harness does not use them.

The user's brief reads:

> "there's no built-in parallel mode, so you launch two processes, but three
> resources are shared."

The three shared resources, verified in the code:

| # | Resource | Where | Symptom under naive parallel launch |
|---|----------|-------|-------------------------------------|
| 1 | Bridge ports: claude-oauth `8765`, codex-oauth `8788`, GLM zbridge `8820` (only 4 fallback slots) | [`claude_oauth/__main__.py:149`](../../scripts/claude_oauth/__main__.py), [`codex_oauth/__main__.py:13`](../../scripts/codex_oauth/__main__.py), [`glm_bridge.py:34`](../../scripts/glm_bridge.py) | Second worker: `EADDRINUSE`; GLM: `_PortTaken` after 4 tries and the run aborts |
| 2 | Timestamped run-dir: `agent_output/<task>/<model>/YYYYMMDD_HHMMSS_e2e/` (1-second granularity) | [`run_harbor.py:3203,3215`](../../run_harbor.py) | `unique_dir()` at [`run_harbor.py:403`](../../run_harbor.py) recovers with `_2/_3` suffixes when it wins the race, but the pass@k parent bypasses this by passing `--output-dir <base>/run{i}` explicitly at [`run_harbor.py:2447`](../../run_harbor.py) |
| 3 | Deliverables batch: `ensure_project()` picks the "newest existing `trajectories_*` dir or creates a new one" | [`deliverables.py:175-176`](../../scripts/deliverables.py) | Two parent processes race and each carve their own batch → the 8 runs split across two `trajectories_<uuid>/` trees, `write_pass_summary()` at [`deliverables.py:562`](../../scripts/deliverables.py) reports pass@4 twice instead of pass@8 |

Resources that were checked and are **safe** to run in parallel (do not touch):

- **Docker container names**: `harbor-{uuid.uuid4().hex[:8]}` — [`run_harbor.py:2481`](../../run_harbor.py). Collision probability negligible.
- **Harbor `jobs_dir`**: `run_dir / "harbor" / "jobs"` — per-run, no sharing ([`run_harbor.py:2814`](../../run_harbor.py)).
- **`deliverables.allocate_run_dir()`**: uses atomic `mkdir` on `run<N>` ([`deliverables.py:186-190`](../../scripts/deliverables.py)) — race-safe by construction.
- **`.env` / z.ai key file**: read-only.
- **Docker daemon, uv cache tarball**: docker/uv handle their own concurrency.

## 2. Non-goals

- Parallelizing across **tasks** (that is `batch_run.sh`'s job and deprecated; a separate plan if wanted).
- Making the legacy runner concurrency-safe.
- Reducing per-run resource use (2 vCPU × 8 GiB per sandbox from `harness-config.json:29-30` is fixed by contract; the plan respects it).
- Changing scoring, judge, or trajectory shape.

## 3. Design

### 3.1 Concurrency knob

Add `--parallel N` (int, default `1`) alongside `--pass-at-k K` in the top-level parser
([`run_harbor.py:3024`](../../run_harbor.py)). Semantics:

- `N = 1`: current sequential behavior byte-identical.
- `N > 1`: at most `N` of the `K` child runs execute at once.
- `N > K`: clamped to `K`.
- Also read `KAKASHI_PASS_AT_K_PARALLEL` env for CI ergonomics (matches the
  existing `KAKASHI_GOST_READ_TIMEOUT` pattern at [`harbor_runner.py:239`](../../scripts/harbor_runner.py)).

Print an "adequacy" line at start similar to the harbor pin banner, e.g.:

```
pass@8 with --parallel 4 → 2 waves of 4 concurrent runs, wall clock upper bound ≈ 3h
```

### 3.2 Fix resource 1 — bridge ports (the hard one)

**Root cause:** the bridges default to fixed ports; only zbridge tries 4
consecutive fallbacks, and it prints a hard error after that.

**Fix:** allocate a free port per child in the parent, hand it in via the
existing CLI flags:

- `--cc-bridge-port` already exists at [`run_harbor.py:3039`](../../run_harbor.py) → reuse.
- `--glm-bridge-port` already exists at [`run_harbor.py:3009`](../../run_harbor.py) → reuse.
- Add `--codex-bridge-port` symmetrically (thin passthrough; codex_oauth already
  accepts `--port`).

Parent-side port allocator (one function in `run_harbor.py`, ~15 lines):

```python
def _pick_free_port():
    """Bind :0, read the port, close — same pattern the stdlib test suite uses.
    Race window is small; children still handle EADDRINUSE on start."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
```

For each of the K child commands built in `run_pass_at_k()`, append the port
flag(s) that the run's provider actually needs:

- `--model-provider anthropic` + `--claude-subscription`: `--cc-bridge-port <p>`
- `--model-provider glm` (bridged, i.e. not `--glm-direct`): `--glm-bridge-port <p>`
- codex subscription (if triggered by the agent config): `--codex-bridge-port <p>`

Direct-API modes (`--glm-direct`, plain bedrock, plain anthropic without
subscription) allocate nothing. This is critical — those modes never start a
bridge and adding a port flag would be misleading.

**Also raise `glm_bridge.BIND_ATTEMPTS`** from 4 → 32 as belt-and-suspenders
([`glm_bridge.py:35`](../../scripts/glm_bridge.py)); this covers a lost race between the parent's
`bind(:0)` and the child's spawn.

### 3.3 Fix resource 2 — output-dir race

Current: parent writes `--output-dir agent_output/<task>/<model>/<ts>_e2e/run{i}`.
Two parallel children never collide *on run{i}* because `i` differs, but the
`<ts>` string is computed *inside each child* at [`run_harbor.py:3203`](../../run_harbor.py) and two
sibling children hit the same second.

Two-part fix:

1. **Compute the timestamp once in the parent** for the whole pass@k batch, and
   pass a single `--output-dir <ts>_e2e/run{i}` layout that shares the parent
   timestamp. Reuses the existing `--output-dir` semantics; no new flag.
2. **Keep `unique_dir()` in the child** as the safety net for cross-parent races
   (someone starts two `run_harbor.py` invocations by hand). It already works.

### 3.4 Fix resource 3 — deliverables batch

Current: `run_pass_at_k()` already **pre-computes `traj_dir` once** and passes
`--trajectories-dir` down ([`run_harbor.py:2436-2440`](../../run_harbor.py)). That path is race-safe
for children of a single parent because `deliverables.allocate_run_dir()` uses
atomic `mkdir`.

The remaining exposure is two **parent** `run_harbor.py --pass-at-k 8` processes
racing on `ensure_project()`. That's outside the scope of "one parent, K
parallel children," but two small hardenings pay for themselves:

- In `ensure_project()` at [`deliverables.py:172-177`](../../scripts/deliverables.py), when the batch is
  auto-picked, log the chosen `trajectories_*` path so a split is visible.
- Document that concurrent `--pass-at-k` on the **same task** must share a
  `--trajectories-dir` explicitly, and add a `README` note under "Entry points."

No code change to `allocate_run_dir()` itself — it is already correct.

### 3.5 Rewrite `run_pass_at_k` to a bounded worker pool

Replace the sequential loop at [`run_harbor.py:2441-2457`](../../run_harbor.py) with a bounded
concurrent runner. Constraints preserved from the current implementation:

- Each sample is still a **fresh subprocess** (the docstring at
  [`run_harbor.py:2424`](../../run_harbor.py) is load-bearing — atexit hooks, `os.environ` edits,
  and bridge state must not leak between samples).
- Ctrl-C / `SIGTERM` still terminates all children (the current `try/except
  BaseException` path at [`run_harbor.py:2451-2456`](../../run_harbor.py)).
- Exit code still `0` iff at least one child returned `0` ([`run_harbor.py:2466`](../../run_harbor.py)).

Implementation shape:

```python
import concurrent.futures

def run_pass_at_k(args, task_dir):
    parallel = _clamp_parallel(args.parallel, args.pass_at_k)
    ts = time.strftime("%Y%m%d_%H%M%S")               # (3.3) parent stamp
    ...
    def _launch_one(i):
        cmd = list(base)
        if args.output_dir:
            cmd += ["--output-dir",
                    str(Path(args.output_dir) / f"{ts}_e2e" / f"run{i}")]
        # (3.2) per-child port allocation
        for flag in _child_bridge_flags(args):
            cmd += [flag, str(_pick_free_port())]
        proc = subprocess.Popen(cmd)
        try:
            return proc.wait()
        except BaseException:
            proc.terminate()
            try: proc.wait(60)
            except subprocess.TimeoutExpired: proc.kill()
            raise

    with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = [pool.submit(_launch_one, i) for i in range(1, args.pass_at_k + 1)]
        exits = []
        try:
            for f in concurrent.futures.as_completed(futures):
                exits.append(f.result())
        except BaseException:
            for f in futures: f.cancel()
            raise
    ...
```

Threads (not `ProcessPoolExecutor`) because the workers only wait on
subprocesses; each `_launch_one` is IO-bound on the child.

### 3.6 Ordering of runtime state

Keep `atexit.register(glm_bridge.stop)` at [`run_harbor.py:3200`](../../run_harbor.py) — but in the
**parent** of a parallel pass@k the parent itself does not start a bridge; each
child manages its own. Verify by reading `start_glm_bridge()` and confirming it
is skipped when `args.pass_at_k > 1` early-returns to `run_pass_at_k()` at
[`run_harbor.py:3183`](../../run_harbor.py). (Cross-check during implementation: if the parent
somehow still starts a bridge, wrap the early return so it does not.)

---

## 4. File-by-file change list

| File | Change | Approx LoC |
|------|--------|------------|
| `run_harbor.py` | Add `--parallel` arg + env; rewrite `run_pass_at_k()`; add `_pick_free_port()`, `_child_bridge_flags()`, `_clamp_parallel()`; parent-side timestamp | ~90 net |
| `scripts/glm_bridge.py` | Bump `BIND_ATTEMPTS` 4 → 32 | 1 |
| `scripts/codex_oauth/__main__.py` (and its bridge module) | Confirm `--port` is already threaded through; add a `--codex-bridge-port` on the run_harbor parser and pass it into the codex bridge launch site | ~10 |
| `scripts/deliverables.py` | One `print()` in `ensure_project()` when an existing `trajectories_*` is auto-picked | 2 |
| `README.md` | Regenerated by `scripts/render_readme.py` — do **not** hand-edit ([`README.md:1-5`](../../README.md)); mention `--parallel` in the docstring of `run_harbor.py` and rerun the emitter | 0 direct, regenerated |

No `harbor.lock`, `harness-config.json`, `pyproject.toml`, or `uv.lock` touched.

---

## 5. Acceptance criteria (verifiable)

1. **Sequential parity:** `run_harbor.py <task> --pass-at-k 4` with `--parallel 1`
   produces the same `run1..run4` layout under
   `deliverables/<task>/trajectories_*/` and the same `pass_summary.json` shape
   as the current implementation on the same task. Diff-clean modulo timestamps.
2. **Parallel wall-clock:** `--pass-at-k 4 --parallel 4` finishes in < 1.5×
   the wall-clock of a single run on the pilot task
   (`input/CVE-2023-31122` per `run_harbor.py:90`), where the current sequential
   version takes ≈ 4× a single run.
3. **No port collisions:** four concurrent GLM runs all start their bridges and
   none hit `_PortTaken`. Verify by grepping the emitted `Egress proxy: ...` /
   bridge startup lines in the four child logs; four distinct ports appear.
4. **Deliverables unified:** all 4 (or 8) runs land under a single
   `trajectories_<uuid>/<model>/` and `write_pass_summary()` reports
   `runs_scored == pass_at_k`.
5. **Ctrl-C:** SIGINT to the parent tears down all running children within 60 s
   (already the per-child `wait(60)` bound); `docker ps` shows no leaked
   `harbor-*` containers.
6. **`--parallel > K` clamped:** `--pass-at-k 2 --parallel 8` runs 2 children,
   not 8; the startup banner prints the clamped value.

## 6. Test plan

- **Unit:** add a fake-subprocess test that stubs `subprocess.Popen` and drives
  `run_pass_at_k()` with parallel = {1, 2, 4}, asserting the correct number of
  children and that per-child `--output-dir` / bridge-port args are distinct.
  Place under `harness/tests/` (already exists per the `ls`).
- **Smoke (self-test):** extend the existing `--self-test` path at
  [`run_harbor.py:2990`](../../run_harbor.py) with a `test_run_pass_at_k_parallel_stub()` that
  runs `--pass-at-k 3 --parallel 3` against a stub `run_harbor.py` script
  written to a temp dir (mirrors the synthetic-tree pattern already used at
  [`run_harbor.py:495`](../../run_harbor.py)).
- **Integration (manual, one-shot):** on the docker host, run
  `python run_harbor.py input/CVE-2023-31122 --pass-at-k 4 --parallel 4
  --model-provider glm --glm-model-id glm-5.3`
  and confirm criteria 2-4. Record host CPU/mem headroom during the run —
  4 × (2 vCPU / 8 GiB) = 8 vCPU / 32 GiB required per `harness-config.json`
  sandbox spec.

## 7. Risks and mitigations

| Risk | Mitigation |
|------|-----------|
| Bind race between parent `_pick_free_port()` and child `bind()` | Bumped `BIND_ATTEMPTS`; children still fall back to consecutive ports |
| Host resource exhaustion at high `--parallel` on small machines | Default `--parallel 1`; explicit opt-in; document the 2 vCPU × 8 GiB per-run sandbox multiplier in the CLI help |
| Judge concurrency: rubric judge calls happen inside each child ([`run_harbor.py:2127`](../../run_harbor.py)); parallel runs multiply the judge API rate | Out of scope for correctness; note in README that judge rate-limit / concurrency budget is per-run, not per-batch, and cite `retryPolicy.maxRetries` from `harness-config.json:33-42` |
| `ensure_project()` cross-parent race remains | Documented; a follow-up plan can add a filesystem lock if needed |
| Deliverables split when someone runs two `--pass-at-k` on the same task | The new `print()` makes it visible; explicit `--trajectories-dir` is the fix |
| `pass_summary.json` written twice as runs complete out of order | `write_pass_summary()` is idempotent by construction ([`deliverables.py:562`](../../scripts/deliverables.py) — reads all `run*/result.json` files each call); confirm during implementation |

## 8. Rollout

1. Land the change behind default `--parallel 1` (zero behavior change).
2. Run acceptance criteria 1, 5, 6 on any host (no docker needed for 1 and 6).
3. Run criteria 2-4 on the linux docker host with GLM credentials once.
4. Regenerate `README.md` via `python3 scripts/render_readme.py`.
5. Flip the internal default (if desired) in a follow-up.

## 9. Open questions for the requester

1. Should `--parallel` default to `1` (safe) or to `min(K, cpu_count() // 2)`
   (fast on fat hosts)? Recommend `1`.
2. Do you want the parent to fail fast if any child errors, or always wait for
   all K (current behavior)? Recommend always wait — pass@k semantics need every
   sample.
3. Codex-oauth bridge — is it actually reachable from any current
   `--model-provider` in the harness, or dead code? If dead, drop 3.2's codex
   branch and skip the passthrough flag entirely.
