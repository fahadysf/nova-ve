# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""
host_net — Linux bridge / TAP name helpers, instance-ID provisioning, and
thin Python wrappers around the privileged ``nova-ve-net.py`` helper.

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

Privileged helper (US-201) is invoked via:
  sudo $NOVA_VE_HELPER_BIN <verb> [args...]
where NOVA_VE_HELPER_BIN defaults to /opt/nova-ve/bin/nova-ve-net.py.
"""

import hashlib
import logging
import os
import subprocess
from pathlib import Path
from typing import Sequence

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


class HostNetError(RuntimeError):
    """Base class for nova-ve-net helper invocation failures."""

    def __init__(self, message: str, *, returncode: int = 0, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class HostNetValidationError(HostNetError):
    """Helper rejected an argument (regex / ownership) → exit code 2."""


class HostNetEEXIST(HostNetError):
    """The kernel object already exists (e.g. duplicate bridge name)."""


class HostNetEINVAL(HostNetError):
    """The helper's underlying ``ip`` invocation rejected the request."""


class HostNetUnknown(HostNetError):
    """Unknown verb or unparseable failure."""


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


def veth_host_name(lab_id: str, node_id: int, iface: int) -> str:
    """Return the host-side veth name for a docker container NIC.

    Format: nve{lab_hash:04x}d{node_id}i{iface}h
    Max length: 14 chars (TAP base + 'h' suffix).
    """
    instance_id = get_instance_id()
    h = _lab_hash(lab_id, instance_id)
    name = f"nve{h:04x}d{node_id}i{iface}h"
    assert len(name) <= 14, f"veth_host_name overflow: {name!r} ({len(name)} chars)"
    return name


def veth_peer_name(lab_id: str, node_id: int, iface: int) -> str:
    """Return the container-peer veth name (pre-rename).

    Format: nve{lab_hash:04x}d{node_id}i{iface}p
    The peer is renamed to ``eth{iface}`` after being moved into the netns.
    """
    instance_id = get_instance_id()
    h = _lab_hash(lab_id, instance_id)
    name = f"nve{h:04x}d{node_id}i{iface}p"
    assert len(name) <= 14, f"veth_peer_name overflow: {name!r} ({len(name)} chars)"
    return name


# ---------------------------------------------------------------------------
# Privileged helper invocation (US-201 wrapper)
# ---------------------------------------------------------------------------

_HELPER_BIN_DEFAULT = "/opt/nova-ve/bin/nova-ve-net.py"
_SUDO_BIN_DEFAULT = "/usr/bin/sudo"
_IP_BIN_DEFAULT = "/sbin/ip"


def _helper_bin() -> str:
    return os.environ.get("NOVA_VE_HELPER_BIN", _HELPER_BIN_DEFAULT)


def _sudo_bin() -> str:
    return os.environ.get("NOVA_VE_SUDO_BIN", _SUDO_BIN_DEFAULT)


def _ip_bin() -> str:
    return os.environ.get("NOVA_VE_IP_BIN", _IP_BIN_DEFAULT)


def _run(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    """Spawn ``argv`` (shell=False) and return the completed process.

    Captures stdout+stderr as text. Tests monkey-patch this function.
    """
    return subprocess.run(
        list(argv),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )


def _classify_helper_failure(
    stderr: str, returncode: int
) -> HostNetError:
    """Map helper exit code + stderr to a typed exception."""
    msg = stderr.strip() or f"nova-ve-net.py exited with {returncode}"
    if returncode == 2:
        return HostNetValidationError(msg, returncode=returncode, stderr=stderr)
    if returncode == 3:
        return HostNetUnknown(msg, returncode=returncode, stderr=stderr)
    # exit 1 — underlying ``ip`` invocation failed.
    lower = stderr.lower()
    if "exists" in lower or "file exists" in lower:
        return HostNetEEXIST(msg, returncode=returncode, stderr=stderr)
    if "does not exist" in lower or "cannot find" in lower or "no such" in lower:
        return HostNetEINVAL(msg, returncode=returncode, stderr=stderr)
    return HostNetEINVAL(msg, returncode=returncode, stderr=stderr)


def _invoke_helper(verb: str, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run ``sudo <helper> <verb> [args...]`` and raise typed errors on failure."""
    argv = [_sudo_bin(), "-n", _helper_bin(), verb, *args]
    proc = _run(argv)
    if proc.returncode != 0:
        raise _classify_helper_failure(proc.stderr or "", proc.returncode)
    return proc


def bridge_exists(name: str) -> bool:
    """Return True if a Linux interface with ``name`` is currently present.

    Used for idempotency in ``create_network``: we re-use a pre-existing
    bridge with the right name rather than failing on EEXIST. The query
    runs without sudo (``ip link show`` is unprivileged read access).
    """
    try:
        proc = _run([_ip_bin(), "link", "show", name])
    except FileNotFoundError:
        # ``ip`` missing entirely — treat as "no" so callers see a real
        # bridge_add error if they go on to provision.
        return False
    return proc.returncode == 0


def bridge_add(name: str) -> None:
    """Create a Linux bridge with ``name`` via the privileged helper.

    Raises :class:`HostNetEEXIST` if the bridge already exists, or other
    :class:`HostNetError` subclasses on validation / kernel failures.
    Idempotency is the *caller's* responsibility — call
    :func:`bridge_exists` first if no-op-on-exists semantics are desired.
    """
    _invoke_helper("bridge-add", name)


def bridge_del(name: str) -> None:
    """Delete the Linux bridge ``name`` via the privileged helper.

    Raises :class:`HostNetEINVAL` if the bridge does not exist.
    """
    _invoke_helper("bridge-del", name)


# ---------------------------------------------------------------------------
# Veth / link helpers (US-203 / US-204 — manual veth + nsenter rename)
# ---------------------------------------------------------------------------


def veth_pair_add(host_end: str, peer_end: str) -> None:
    """Create a veth pair via the privileged helper.

    ``host_end`` and ``peer_end`` must match ``RE_VETH_NAME`` in the helper
    (``nve<hash>d<node>i<iface>[hp]``). Raises :class:`HostNetEEXIST` if a
    link with either name already exists.
    """
    _invoke_helper("veth-pair-add", host_end, peer_end)


def link_master(iface: str, bridge: str) -> None:
    """Attach ``iface`` to ``bridge`` (``ip link set <iface> master <bridge>``)."""
    _invoke_helper("link-master", iface, bridge)


def link_set_nomaster(iface: str) -> None:
    """Detach ``iface`` from its current master."""
    _invoke_helper("link-set-nomaster", iface)


def link_netns(iface: str, pid: int) -> None:
    """Move ``iface`` into the netns of ``pid`` (``ip link set ... netns <pid>``).

    The pid MUST be present in the runtime registry (``runtime_pids.register``)
    before this call — the helper rejects unregistered pids with exit 2.
    """
    _invoke_helper("link-netns", iface, str(int(pid)))


def link_up(iface: str) -> None:
    """Bring ``iface`` up on the host (``ip link set <iface> up``)."""
    _invoke_helper("link-up", iface)


def link_set_name_in_netns(pid: int, oldname: str, newname: str) -> None:
    """Rename ``oldname`` to ``newname`` inside ``pid``'s netns.

    Used to rename the veth peer (``...p``) to ``eth{iface}`` after it has
    been moved into the container's netns.
    """
    _invoke_helper("link-set-name-in-netns", str(int(pid)), oldname, newname)


def addr_up_in_netns(pid: int, iface: str) -> None:
    """Bring ``iface`` up inside ``pid``'s netns."""
    _invoke_helper("addr-up-in-netns", str(int(pid)), iface)


def tap_add(name: str) -> None:
    """Create a Linux TAP device with ``name`` via the privileged helper.

    Used by the QEMU start path (US-302): one TAP per NIC, attached to the
    network's bridge before QEMU launches with ``-netdev tap,ifname=...``.
    Raises :class:`HostNetEEXIST` if a link with ``name`` already exists.
    """
    _invoke_helper("tap-add", name)


def tap_del(name: str) -> None:
    """Delete a Linux TAP device via the privileged helper.

    The helper's ``tap-del`` verb is ``ip link del <name>``; it also works
    for veth host-ends. Raises :class:`HostNetEINVAL` if the link is gone.
    """
    _invoke_helper("tap-del", name)


def link_del(name: str) -> None:
    """Delete a host-side link (TAP or veth host-end) via the helper.

    The helper reuses the ``tap-del`` verb (``ip link del``) which works for
    veth host-ends as well — deleting the host end auto-removes the peer.
    """
    _invoke_helper("tap-del", name)


def try_link_del(name: str) -> None:
    """Best-effort link deletion — swallows :class:`HostNetEINVAL` (already gone).

    Used in rollback / cleanup paths where the kernel object may already
    have been removed by an earlier step or an external sweeper.
    """
    try:
        link_del(name)
    except HostNetEINVAL:
        return
    except HostNetValidationError:
        # Validation failures should not be silenced — propagate so the
        # caller sees the bug.
        raise
    except HostNetError:
        # Any other helper failure is still best-effort: log and continue.
        logger.warning("try_link_del(%s): non-fatal cleanup failure", name)
