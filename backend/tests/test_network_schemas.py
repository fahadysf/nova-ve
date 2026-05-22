# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Schema-level Pydantic enforcement tests for ``NetworkCreate``.

Covers AC7 from .omc/plans/bridge-cloud-feature.md §3: Pydantic must
reject a ``bridge_cloud`` payload without ``config.host_bridge``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.network import NetworkConfig, NetworkCreate


def test_bridge_cloud_requires_host_bridge_via_pydantic():
    nc = NetworkCreate(
        name="bc",
        type="bridge_cloud",
        config=NetworkConfig(host_bridge="br-eth0"),
    )
    assert nc.config is not None and nc.config.host_bridge == "br-eth0"


@pytest.mark.parametrize(
    "config",
    [
        None,
        NetworkConfig(),
        NetworkConfig(host_bridge=""),
        NetworkConfig(host_bridge="   "),
    ],
)
def test_bridge_cloud_rejects_missing_or_blank_host_bridge(config):
    with pytest.raises(ValidationError) as ei:
        NetworkCreate(name="bc", type="bridge_cloud", config=config)
    assert "host_bridge is required" in str(ei.value)


def test_non_bridge_cloud_types_do_not_require_host_bridge():
    """The validator is type-gated — other network types must keep
    working without a ``host_bridge`` in their config."""
    # Bare construction.
    NetworkCreate(name="lb", type="linux_bridge")
    # Empty NetworkConfig still allowed.
    NetworkCreate(name="natc", type="nat_cloud", config=NetworkConfig())
