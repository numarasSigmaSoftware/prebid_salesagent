"""Exercise the composite action's shell against controlled runner disk readings.

This is a CI process contract, with no buyer-facing wire or database. Only the
external disk probe and destructive cleanup commands are substituted; Bash runs
all of the action's branching and its real exit status is the assertion subject.
"""

import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize(
    ("readings", "expected_status", "reclaims"),
    [
        ([85 * 1024**3], 0, False),
        ([14 * 1024**3, 85 * 1024**3], 0, True),
        ([1024**3, 14 * 1024**3], 1, True),
        ([1024**3, 1024**3], 1, True),
        ([], 1, False),
        ([1024**3], 1, True),
    ],
    ids=[
        "enough-before-cleanup",
        "reclaim-sufficient",
        "historic-overflow",
        "reclaim-insufficient",
        "initial-probe-fails",
        "post-reclaim-probe-fails",
    ],
)
def test_free_disk_action(tmp_path: Path, readings: list[int], expected_status: int, reclaims: bool) -> None:
    action_path = Path(__file__).resolve().parents[2] / ".github/actions/_free-disk/action.yml"
    action = yaml.safe_load(action_path.read_text())
    script = action["runs"]["steps"][0]["run"]
    (tmp_path / "readings").write_text("".join(f"{value}\n" for value in readings))
    # A file preserves probe state across the action's command substitutions.
    # Never run the real sudo/rm or Docker cleanup on the test host.
    commands = """
    df() {
      local available
      read -r available < readings || return 1
      tail -n +2 readings > remaining
      mv remaining readings
      printf 'Avail\\n%s\\n' "$available"
    }
    sudo() { printf 'sudo\\n' >> cleanup; }
    docker() { printf 'docker\\n' >> cleanup; }
    """
    result = subprocess.run(["bash", "-c", commands + script], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == expected_status, result.stderr
    cleanup = tmp_path / "cleanup"
    assert cleanup.exists() is reclaims
    if reclaims:
        assert cleanup.read_text().splitlines() == ["sudo", "docker"]
