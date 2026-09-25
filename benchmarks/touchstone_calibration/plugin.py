"""Touchstone-calibration plugin (Bucket-N advisory; never SHIP evidence).

trinity/FORGE.md line 32 marks ``.seed/probe.yaml`` output as Bucket-N and
never difficulty evidence, and rule 6 ("difficulty is measured, never
claimed") forbids any local pass from promoting a task. This plugin therefore
never yields SHIP: every rollout it evaluates carries ``bucket = "N"`` and
``never_ship = true`` inherited from the shimmed bundle's ``task.toml``.
"""
import json
from pathlib import Path

from lib.base import Benchmark, Instance, Reward, Sandbox
from lib.registry import register


class TouchstoneCalibration(Benchmark):
    """Calibration probe benchmark.

    Reads shimmed touchstone bundles under ``derived/touchstones/<case>/`` that
    ``scripts/touchstone_shim.py`` has canonicalised into Harbor shape.
    Container build and lockdown reuse ``run_harbor.py`` unchanged; scoring
    uses only the deterministic four-stage grader in each bundle's
    ``tests/test.sh``, with the rubric channel disabled by default per this
    benchmark's ``benchmark.toml`` ``judge_disabled_by_default = true``.
    """

    name = "touchstone_calibration"

    def prepare_instance(self, bundle_path: Path) -> Instance:
        """Read task.toml from a shimmed bundle under derived/touchstones/."""
        return Instance(
            bundle_path=bundle_path,
            task_id=bundle_path.name,
            metadata={
                "task_toml": str(bundle_path / "task.toml"),
                "bucket": "N",
                "never_ship": True,
                "provenance": "trinity/FORGE.md line 32 (.seed/probe.yaml); rule 6",
            },
        )

    def prepare_sandbox(self, instance: Instance) -> Sandbox:
        """Container build and lockdown reused from cybergym_e2e via run_harbor.py."""
        return Sandbox(working_dir=instance.bundle_path)

    def evaluate_instance(self, instance: Instance, rollout_path: Path) -> Reward:
        """Read reward.json produced by the shimmed bundle's tests/test.sh.

        Every returned Reward is annotated ``bucket = "N"`` so a downstream
        consumer that treats it as difficulty evidence is refused by the
        contract's Bucket-N carve-out.
        """
        reward_file = rollout_path / "verifier" / "reward.json"
        if not reward_file.is_file():
            return Reward(metadata={"bucket": "N", "never_ship": True})
        data = json.loads(reward_file.read_text())
        return Reward(
            value=float(data.get("reward", 0.0)),
            stages={k: v.get("status", "unknown") for k, v in (data.get("stages") or {}).items()},
            metadata={**data, "bucket": "N", "never_ship": True},
        )


register("touchstone_calibration", TouchstoneCalibration)
