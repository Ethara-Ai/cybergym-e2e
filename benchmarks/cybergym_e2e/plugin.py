"""CyberGym-E2E plugin (delegates to run_harbor.py during transition)."""
import json
from pathlib import Path

from lib.base import Benchmark, Instance, Reward, Sandbox
from lib.registry import register


class CyberGymE2E(Benchmark):
    """CyberGym-E2E vulnerability-repair benchmark."""

    name = "cybergym_e2e"

    def prepare_instance(self, bundle_path: Path) -> Instance:
        """Read task.toml from a resident bundle."""
        return Instance(
            bundle_path=bundle_path,
            task_id=bundle_path.name,
            metadata={"task_toml": str(bundle_path / "task.toml")},
        )

    def prepare_sandbox(self, instance: Instance) -> Sandbox:
        """Container build and lockdown is delegated to run_harbor.py."""
        return Sandbox(working_dir=instance.bundle_path)

    def evaluate_instance(self, instance: Instance, rollout_path: Path) -> Reward:
        """Read reward.json produced by the bundle's tests/test.sh."""
        reward_file = rollout_path / "verifier" / "reward.json"
        if not reward_file.is_file():
            return Reward()
        data = json.loads(reward_file.read_text())
        return Reward(
            value=float(data.get("reward", 0.0)),
            stages={k: v.get("status", "unknown") for k, v in (data.get("stages") or {}).items()},
            metadata=data,
        )


register("cybergym_e2e", CyberGymE2E)
