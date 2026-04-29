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
  $NOVA_VE_INSTANCE_ID_FILE
  $NOVA_VE_INSTANCE_DIR/instance_id  (default dir: /etc/nova-ve)

Precedence rules:
  1. If NOVA_VE_INSTANCE_ID_FILE is set → use that file path.
  2. Else if NOVA_VE_INSTANCE_DIR is set → use $DIR/instance_id.
  3. Else → use /etc/nova-ve/instance_id.
  4. Missing or empty file → raise HostNetInstanceIdMissing.

Privileged helper (US-201) is invoked via:
  sudo $NOVA_VE_HELPER_BIN <verb> [args...]
where NOVA_VE_HELPER_BIN defaults to /opt/nova-ve/bin/nova-ve-net.py.
"""

import hashlib
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Sequence

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


class HostNetBridgeOwnershipError(HostNetError):
    """A bridge name collision failed ownership verification."""


# ---------------------------------------------------------------------------
# Instance-ID resolution
# ---------------------------------------------------------------------------

_INSTANCE_DIR_DEFAULT = "/etc/nova-ve"
_INSTANCE_FILE_NAME = "instance_id"
_INSTANCE_ID_FILE_ENV = "NOVA_VE_INSTANCE_ID_FILE"
_INSTANCE_ID_DIR_ENV = "NOVA_VE_INSTANCE_DIR"


def _instance_id_file() -> Path:
    """Return the instance_id path using file override, dir override, then default."""
    instance_file = os.environ.get(_INSTANCE_ID_FILE_ENV, "").strip()
    if instance_file:
        return Path(instance_file)

    instance_dir = os.environ.get(_INSTANCE_ID_DIR_ENV, _INSTANCE_DIR_DEFAULT)
    return Path(instance_dir) / _INSTANCE_FILE_NAME


def get_instance_id() -> str:
    """Return the instance ID string from the resolved instance-id file.

    Raises HostNetInstanceIdMissing if the ID cannot be resolved.
    """
    id_file = _instance_id_file()
    try:
        value = id_file.read_text(encoding="ascii").strip()
    except FileNotFoundError as exc:
        raise HostNetInstanceIdMissing(
            f"Instance ID file '{id_file}' is missing. Set {_INSTANCE_ID_FILE_ENV} to a "
            f"readable file, set {_INSTANCE_ID_DIR_ENV} to a directory containing "
            f"'{_INSTANCE_FILE_NAME}', or provision '{_INSTANCE_DIR_DEFAULT}/{_INSTANCE_FILE_NAME}'."
        ) from exc
    except OSError as exc:
        raise HostNetInstanceIdMissing(
            f"Instance ID file '{id_file}' could not be read: {exc}"
        ) from exc

    if value:
        return value

    raise HostNetInstanceIdMissing(
        f"Instance ID file '{id_file}' is empty. Populate it with a non-empty host ID "
        f"or point {_INSTANCE_ID_FILE_ENV} at a valid override file."
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
_RUNTIME_ROOT_DEFAULT = Path("/var/lib/nova-ve")
_BRIDGE_FINGERPRINT_DIRNAME = "bridges"


def _helper_bin() -> str:
    return os.environ.get("NOVA_VE_HELPER_BIN", _HELPER_BIN_DEFAULT)


def _sudo_bin() -> str:
    return os.environ.get("NOVA_VE_SUDO_BIN", _SUDO_BIN_DEFAULT)


def _ip_bin() -> str:
    return os.environ.get("NOVA_VE_IP_BIN", _IP_BIN_DEFAULT)


def _runtime_root() -> Path:
    override = os.environ.get("NOVA_VE_RUNTIME_ROOT")
    if override:
        return Path(override)
    return _RUNTIME_ROOT_DEFAULT


def _bridge_fingerprint_root() -> Path:
    override = os.environ.get("NOVA_VE_BRIDGE_FINGERPRINT_ROOT")
    if override:
        return Path(override)
    return _runtime_root() / _BRIDGE_FINGERPRINT_DIRNAME


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


def _classify_helper_error(
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
    if "file exists" in lower:
        return HostNetEEXIST(msg, returncode=returncode, stderr=stderr)
    if (
        "invalid argument" in lower
        or "does not exist" in lower
        or "no such" in lower
    ):
        return HostNetEINVAL(msg, returncode=returncode, stderr=stderr)
    return HostNetUnknown(msg, returncode=returncode, stderr=stderr)


_classify_helper_failure = _classify_helper_error


def _invoke_helper(verb: str, *args: str) -> "subprocess.CompletedProcess[str]":
    """Run ``sudo <helper> <verb> [args...]`` and raise typed errors on failure."""
    argv = [_sudo_bin(), "-n", _helper_bin(), verb, *args]
    proc = _run(argv)
    if proc.returncode != 0:
        raise _classify_helper_error(proc.stderr or "", proc.returncode)
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


def bridge_fingerprint_path(name: str) -> Path:
    """Return the ownership fingerprint path for ``name``."""
    return _bridge_fingerprint_root() / f"{name}.json"


def bridge_fingerprint_read(name: str) -> dict[str, Any] | None:
    """Return the decoded ownership fingerprint or ``None`` if absent.

    Malformed or unreadable files are surfaced as synthetic dict payloads so
    callers can fail closed and include the on-disk state in diagnostics.
    """
    path = bridge_fingerprint_path(name)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return {"_error": f"could not read fingerprint: {exc}"}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"_error": f"invalid JSON: {exc}", "_raw": raw}

    if not isinstance(payload, dict):
        return {
            "_error": f"expected JSON object, found {type(payload).__name__}",
            "_raw": payload,
        }
    return payload


def bridge_fingerprint_write(name: str, lab_id: str, network_id: int) -> None:
    """Atomically persist bridge ownership metadata for ``name``."""
    path = bridge_fingerprint_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
    payload = {
        "lab_id": str(lab_id),
        "network_id": int(network_id),
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    try:
        with open(tmp_path, "w", encoding="utf-8") as fd:
            fd.write(json.dumps(payload, indent=2, sort_keys=True))
            fd.flush()
            os.fsync(fd.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def bridge_fingerprint_check(
    name: str, lab_id: str, network_id: int
) -> Literal["match", "mismatch", "absent"]:
    """Compare the stored ownership fingerprint for ``name`` with expectations."""
    payload = bridge_fingerprint_read(name)
    if payload is None:
        return "absent"
    if payload.get("lab_id") == str(lab_id) and payload.get("network_id") == int(
        network_id
    ):
        return "match"
    return "mismatch"


def bridge_fingerprint_remove(name: str) -> None:
    """Remove the ownership fingerprint for ``name`` if present."""
    bridge_fingerprint_path(name).unlink(missing_ok=True)


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
    bridge_fingerprint_remove(name)


def tap_add(name: str) -> None:
    """Create a TAP interface via the privileged helper."""
    _invoke_helper("tap-add", name)


def tap_del(name: str) -> None:
    """Delete a TAP interface via the privileged helper."""
    _invoke_helper("tap-del", name)


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


def console_proxy_start(node_pid: int, listen_port: int, target_port: int) -> int:
    """Spawn the console TCP forwarder for a manual-veth Docker container.

    Returns the daemonized proxy pid so the caller can persist it on the
    runtime record and tear it down later via :func:`console_proxy_stop`.
    Required because ``docker run --network=none -p`` does not bring up the
    Docker userland proxy — there's no container IP to forward to — so the
    advertised host port is unreachable until we splice in via setns.
    """
    proc = _invoke_helper(
        "console-proxy-start",
        str(int(node_pid)),
        str(int(listen_port)),
        str(int(target_port)),
    )
    text = (proc.stdout or "").strip()
    if not text.isdigit():
        raise HostNetError(
            f"console-proxy-start did not return a pid: stdout={text!r} "
            f"stderr={(proc.stderr or '').strip()!r}",
            returncode=proc.returncode,
            stderr=proc.stderr or "",
        )
    return int(text)


def console_proxy_stop(proxy_pid: int) -> None:
    """Terminate a previously-spawned console proxy. Idempotent."""
    _invoke_helper("console-proxy-stop", str(int(proxy_pid)))


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


def read_iface_mac(pid: int, iface: str) -> str:
    """Return the MAC of ``iface`` inside ``pid``'s netns (US-205b).

    Wraps the helper's ``read-iface-mac`` verb (``nsenter -t <pid> -n cat
    /sys/class/net/<iface>/address``).  Used by ``_read_docker_live_mac``
    after US-207 made ``--network=none`` containers leave Docker's
    ``.NetworkSettings.Networks`` empty — the only place a Docker
    container's NIC MAC lives is now sysfs inside its own netns.
    """
    proc = _invoke_helper("read-iface-mac", str(int(pid)), iface)
    return (proc.stdout or "").strip()


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


def try_bridge_del(name: str) -> None:
    """Best-effort bridge deletion — swallows :class:`HostNetEINVAL` (already gone).

    Used in orphan-sweep paths.
    """
    try:
        bridge_del(name)
    except HostNetEINVAL:
        return
    except HostNetError:
        logger.warning("try_bridge_del(%s): non-fatal cleanup failure", name)


# ---------------------------------------------------------------------------
# Orphan sweep (US-206)
# ---------------------------------------------------------------------------

import re as _re

# Patterns that match nova-ve-owned kernel objects.
# Bridges: nove<4-hex>n<network_id>
_RE_BRIDGE = _re.compile(r"^nove[0-9a-f]{4}n\d{1,5}$")
# TAPs / veth host-ends: nve<4-hex>d<node_id>i<iface>[h|p]?
_RE_NVE_IFACE = _re.compile(r"^nve[0-9a-f]{4}d\d{1,3}i\d{1,2}[hp]?$")


def _list_host_ifaces_prefixed(prefix: str) -> list[str]:
    """Return names of host interfaces whose name starts with ``prefix``.

    Uses ``ip -o link show`` (unprivileged read) and parses the output.
    Returns an empty list on any failure so callers degrade gracefully.
    """
    try:
        proc = _run([_ip_bin(), "-o", "link", "show"])
    except FileNotFoundError:
        return []
    if proc.returncode != 0:
        return []
    names: list[str] = []
    for line in proc.stdout.splitlines():
        # Format: "<index>: <name>[@<peer>]: <flags> ..."
        parts = line.split(":", 2)
        if len(parts) < 2:
            continue
        raw_name = parts[1].strip().split("@")[0]
        if raw_name.startswith(prefix):
            names.append(raw_name)
    return names


def sweep_node_host_ifaces(lab_id: str, node_id: int) -> list[str]:
    """Remove all host-side veth/TAP interfaces belonging to ``(lab_id, node_id)``.

    Sweeps interfaces matching ``nve<hash>d<node_id>i*[h|p]?`` for the given
    lab/node.  Best-effort: failures on individual interfaces are logged and
    skipped so a single stuck interface does not block the rest.

    Returns the list of interface names that were successfully deleted.
    """
    try:
        instance_id = get_instance_id()
    except HostNetInstanceIdMissing:
        return []

    h = _lab_hash(lab_id, instance_id)
    prefix = f"nve{h:04x}d{node_id}i"
    candidates = _list_host_ifaces_prefixed(prefix)
    removed: list[str] = []
    for name in candidates:
        if not _RE_NVE_IFACE.match(name):
            continue
        try:
            try_link_del(name)
            removed.append(name)
            logger.info("orphan-sweep: removed iface %s (lab=%s node=%s)", name, lab_id, node_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "orphan-sweep: failed to remove iface %s (lab=%s node=%s)",
                name, lab_id, node_id, exc_info=True,
            )
    return removed


def sweep_lab_host_ifaces(lab_id: str) -> list[str]:
    """Remove all host-side veth/TAP interfaces belonging to ``lab_id``.

    Sweeps all ``nve<hash>*`` interfaces for the lab.  Called on lab stop.
    Best-effort: individual failures are logged and skipped.

    Returns the list of interface names that were successfully deleted.
    """
    try:
        instance_id = get_instance_id()
    except HostNetInstanceIdMissing:
        return []

    h = _lab_hash(lab_id, instance_id)
    prefix = f"nve{h:04x}"
    candidates = _list_host_ifaces_prefixed(prefix)
    removed: list[str] = []
    for name in candidates:
        if not _RE_NVE_IFACE.match(name):
            continue
        try:
            try_link_del(name)
            removed.append(name)
            logger.info("orphan-sweep: removed lab iface %s (lab=%s)", name, lab_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "orphan-sweep: failed to remove lab iface %s (lab=%s)",
                name, lab_id, exc_info=True,
            )
    return removed


def sweep_orphan_bridges(known_lab_ids: "set[str]") -> list[str]:
    """Scan host bridges and delete any ``nove*`` bridge not owned by a known lab.

    ``known_lab_ids`` is the set of lab IDs currently on disk.  Any ``nove*``
    bridge whose hash prefix does not match any of those labs is an orphan
    (left behind by a crashed backend or a deleted lab) and is removed.

    Best-effort: individual failures are logged and skipped.

    Returns the list of bridge names that were successfully deleted.
    """
    try:
        instance_id = get_instance_id()
    except HostNetInstanceIdMissing:
        return []

    # Build a set of known 4-hex hash prefixes from known labs.
    known_hashes: set[str] = set()
    for lab_id in known_lab_ids:
        h = _lab_hash(lab_id, instance_id)
        known_hashes.add(f"{h:04x}")

    candidates = _list_host_ifaces_prefixed("nove")
    removed: list[str] = []
    for name in candidates:
        if not _RE_BRIDGE.match(name):
            continue
        # Extract the 4-hex portion after "nove"
        bridge_hash = name[4:8]
        if bridge_hash in known_hashes:
            continue
        # Orphan — no live lab owns this bridge.
        logger.warning("orphan-sweep: found orphan bridge %s (no matching lab)", name)
        try:
            try_bridge_del(name)
            removed.append(name)
            logger.info("orphan-sweep: deleted orphan bridge %s", name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "orphan-sweep: failed to delete orphan bridge %s", name, exc_info=True,
            )
    return removed


def sweep_orphan_ifaces(known_lab_ids: "set[str]") -> list[str]:
    """Scan host interfaces and delete any ``nve*`` iface not owned by a known lab.

    Mirrors :func:`sweep_orphan_bridges` for TAP/veth host-ends.
    Best-effort: individual failures are logged and skipped.

    Returns the list of interface names that were successfully deleted.
    """
    try:
        instance_id = get_instance_id()
    except HostNetInstanceIdMissing:
        return []

    known_hashes: set[str] = set()
    for lab_id in known_lab_ids:
        h = _lab_hash(lab_id, instance_id)
        known_hashes.add(f"{h:04x}")

    candidates = _list_host_ifaces_prefixed("nve")
    removed: list[str] = []
    for name in candidates:
        if not _RE_NVE_IFACE.match(name):
            continue
        iface_hash = name[3:7]
        if iface_hash in known_hashes:
            continue
        logger.warning("orphan-sweep: found orphan iface %s (no matching lab)", name)
        try:
            try_link_del(name)
            removed.append(name)
            logger.info("orphan-sweep: deleted orphan iface %s", name)
        except Exception:  # noqa: BLE001
            logger.warning(
                "orphan-sweep: failed to delete orphan iface %s", name, exc_info=True,
            )
    return removed
