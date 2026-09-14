"""Keep both service installation sources aligned for cold boot."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("directive", ["After", "Wants"])
def test_cold_boot_unit_ordering_sources_match(directive):
    sources = [
        ROOT / "scripts/systemd/pinkybot.service",
        ROOT / "scripts/install-service.sh",
    ]
    expected = {"network-online.target", "tailscaled.service"}
    for source in sources:
        tokens = [
            set(line.split("=", 1)[1].split())
            for line in source.read_text().splitlines()
            if line.startswith(f"{directive}=")
        ]
        assert tokens == [expected], f"{source.name}: {directive} must be {expected}"
        assert "Requires=tailscaled.service" not in source.read_text()


def test_user_unit_installer_explains_system_ordering_boundary():
    text = (ROOT / "scripts/install-service.sh").read_text().lower()
    assert "user" in text and "cannot order" in text and "system unit" in text
