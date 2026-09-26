"""The native delivery release changes only its reviewed shared source seal."""

import hashlib
import json

import pytest
from proactive.behavior_profiles import registry as br


@pytest.mark.parametrize("project", ["ai_bizweek", "secondhand_commerce"])
def test_native_delivery_seal_preserves_other_sources_and_profile_contract(project):
    previous = json.loads((br.ROOT / "kernel-v80.json").read_text())
    current = json.loads((br.ROOT / "kernel-v81.json").read_text())
    assert set(current) == set(previous)
    assert {p for p in current if current[p] != previous[p]} == {
        "hermes_cli/kanban_db.py"
    }
    assert (
        current["hermes_cli/kanban_db.py"]
        == hashlib.sha256(
            (br.CODE_ROOT / "hermes_cli/kanban_db.py").read_bytes()
        ).hexdigest()
    )
    old_profile = br.profile(project, "v81")
    profile = br.profile(project, "v82")
    assert profile["safety_kernel_version"] == "81"
    assert profile["safety_kernel_hash"] == br.digest(current)
    assert profile["inflight_review_compatible_from"] == []
    excluded = {
        "version",
        "safety_kernel_version",
        "safety_kernel_hash",
        "inflight_review_compatible_from",
    }
    assert {k: v for k, v in profile.items() if k not in excluded} == {
        k: v for k, v in old_profile.items() if k not in excluded
    }
