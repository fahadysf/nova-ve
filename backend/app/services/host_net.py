# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""
host_net — Linux bridge / TAP name helpers and instance-ID provisioning.

Bridge name format : nove{lab_hash:04x}n{network_id}   (≤14 chars, network_id ≤ 99999)
TAP name format    : nve{lab_hash:04x}d{node_id}i{iface}  (≤13 chars, node_id ≤ 999, iface ≤ 99)

lab_hash is a 16-bit value derived from blake2b(instance_id + ":" + lab_id).
Using blake2b (not Python's built-in hash()) because hash() is salted per-process since Python 3.3.

Instance ID is read from:
  $NOVA_VE_INSTANCE_DIR/instance_id  (default dir: /etc/nova-ve)

Precedence rules (file-wins with explicit env override):
  1. If the file exists and is non-empty → use file value (authoritative).
  2. If NOVA_VE_INSTANCE_ID is set AND NOVA_VE_INSTANCE_ID_OVERRIDE_OK=1 → use env value
     (WARNING logged on every call).
  3. Otherwise → raise HostNetInstanceIdMissing.
"""

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger("nova-ve")

# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------


class HostNetInstanceIdMissing(RuntimeError):
    """Raised when the instance ID cannot be resolved.

    This means deploy/scripts/provision-ubuntu-2604.sh did not run (or the
    instance_id file was removed).  The backend MUST NOT start without a
    valid instance ID — bridge name collisions across hosts would result.
    """


# ---------------------------------------------------------------------------
# Instance-ID resolution
# ---------------------------------------------------------------------------

_INSTANCE_DIR_DEFAULT = "/etc/nova-ve"
_INSTANCE_FILE_NAME = "instance_id"


def _instance_id_file() -> Path:
    """Return the path to the instance_id file, honouring NOVA_VE_INSTANCE_DIR."""
    instance_dir = os.environ.get("NOVA_VE_INSTANCE_DIR", _INSTANCE_DIR_DEFAULT)
    return Path(instance_dir) / _INSTANCE_FILE_NAME


def get_instance_id() -> str:
    """Return the instance ID string, applying the file-wins precedence rules.

    Raises HostNetInstanceIdMissing if the ID cannot be resolved.
    """
    id_file = _instance_id_file()

    # --- Try the file first (authoritative when present and non-empty) ----
    if id_file.exists():
        value = id_file.read_text(encoding="ascii").strip()
        if value:
            return value
        # File exists but is empty — treat as missing.

    # --- Env-var override (only with explicit OVERRIDE_OK flag) -----------
    env_id = os.environ.get("NOVA_VE_INSTANCE_ID", "").strip()
    override_ok = os.environ.get("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", "").strip() == "1"

    if env_id and override_ok:
        logger.warning(
            "WARNING: using NOVA_VE_INSTANCE_ID env override (file ignored); "
            "persist via deploy script for production"
        )
        return env_id

    # --- Neither source is usable — fail hard ----------------------------
    raise HostNetInstanceIdMissing(
        f"Instance ID file '{id_file}' is missing or empty and no valid env override "
        "is configured (set both NOVA_VE_INSTANCE_ID and NOVA_VE_INSTANCE_ID_OVERRIDE_OK=1 "
        "for a temporary override, or run deploy/scripts/provision-ubuntu-2604.sh). "
        "Cannot derive collision-resistant bridge names without a per-host instance ID."
    )


# ---------------------------------------------------------------------------
# Bridge / TAP name derivation
# ---------------------------------------------------------------------------


def _lab_hash(lab_id: str, instance_id: str) -> int:
    """Return a deterministic 16-bit hash of (instance_id, lab_id).

    blake2b with digest_size=2 → 2 bytes → 0x0000..0xFFFF.
    """
    raw = hashlib.blake2b(
        f"{instance_id}:{lab_id}".encode("utf-8"), digest_size=2
    ).hexdigest()
    return int(raw, 16)


def bridge_name(lab_id: str, network_id: int) -> str:
    """Return the Linux bridge name for a network.

    Format: nove{lab_hash:04x}n{network_id}
    Max length: 4 + 4 + 1 + 5 = 14 chars  (network_id ≤ 99999)
    IFNAMSIZ limit: 15 usable chars.
    """
    instance_id = get_instance_id()
    h = _lab_hash(lab_id, instance_id)
    name = f"nove{h:04x}n{network_id}"
    assert len(name) <= 14, f"bridge_name overflow: {name!r} ({len(name)} chars)"
    return name


def tap_name(lab_id: str, node_id: int, iface: int) -> str:
    """Return the TAP interface name for a node NIC.

    Format: nve{lab_hash:04x}d{node_id}i{iface}
    Max length: 3 + 4 + 1 + 3 + 1 + 2 = 14 chars  (node_id ≤ 999, iface ≤ 99)
    """
    instance_id = get_instance_id()
    h = _lab_hash(lab_id, instance_id)
    name = f"nve{h:04x}d{node_id}i{iface}"
    assert len(name) <= 14, f"tap_name overflow: {name!r} ({len(name)} chars)"
    return name
