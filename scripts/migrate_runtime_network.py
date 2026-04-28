#!/usr/bin/env python3
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""One-shot migration script for existing labs — US-202b.

Backfills ``runtime.bridge_name`` on every network record that is missing
it.  Networks created by US-202 already have the field; this script is a
no-op for those labs (idempotent).

In the same read-modify-write cycle, this script also stamps
``node.machine_override = 'pc'`` on every QEMU node that lacks the field
(pre-Wave-7 labs — see ``.omc/plans/network-runtime-wiring.md:397-400``).
This is the load-bearing compatibility discriminator that prevents
US-301's ``_resolve_qemu_machine()`` resolver from silently pivoting
legacy labs from ``pc`` to ``q35`` on first restart post-migration.
Nodes whose ``machine_override`` is already set (e.g. user-chosen
``'q35'``) are left untouched, preserving idempotency.

For labs created *before* Wave 6 (US-202), networks may have a legacy
Docker network bound to them (created by the old ``_ensure_docker_network``
path).  The migration sequence for each such network is:

  1. Assert no containers attached to the Docker network.
  2. Capture ``docker network inspect`` JSON for rollback (fail-closed: abort
     if inspect fails or JSON is unparseable).
  3. ``docker network rm`` the old Docker network.
  4. If a Linux bridge with the canonical ``nove…n…`` name already exists,
     verify its ownership fingerprint matches the expected ``lab_id`` and
     ``network_id``. Unfingerprinted or mismatched bridges abort the lab
     migration fail-closed.
  5. Write back ``runtime.bridge_name`` to lab.json (atomic).

On per-lab failure the in-memory journal is used to roll back host-side
actions (bridge_del + docker network create from captured inspect).  The
lab.json is reverted to its pre-migration backup.  Subsequent labs continue
processing.

**PRECONDITION:** every node in every lab MUST have ``status != 2``
(not running) and the nova-ve-backend service MUST be stopped before
running this script.  The script checks for running nodes and exits 1 with
a clear error listing offending labs if the precondition is violated.

Usage::

    # Dry-run (no writes, no host changes):
    python scripts/migrate_runtime_network.py --labs-dir /var/lib/nova-ve/labs --dry-run

    # Live run (backend must be stopped first):
    sudo /home/ubuntu/nova-ve-git/.venv/bin/python \\
        scripts/migrate_runtime_network.py --labs-dir /var/lib/nova-ve/labs

Exit codes:
    0  — all labs migrated (or already migrated — no-op)
    1  — precondition violated (running nodes found); no changes made
    2  — one or more labs failed migration; backup dir printed on stderr
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the backend package is importable when run from the repo root or
# from backend/.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_BACKEND_DIR = _REPO_ROOT / "backend"
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from app.services import host_net  # noqa: E402
from app.services.lab_lock import lab_lock  # noqa: E402

# ---------------------------------------------------------------------------
# Docker subprocess helpers (thin wrappers, easily replaced in tests)
# ---------------------------------------------------------------------------


def _docker_network_inspect(net_name: str) -> "subprocess.CompletedProcess[str]":
    """Run ``docker network inspect <net_name>`` and return the result."""
    return subprocess.run(
        ["docker", "network", "inspect", net_name],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )


def _docker_network_rm(net_name: str) -> "subprocess.CompletedProcess[str]":
    """Run ``docker network rm <net_name>``."""
    return subprocess.run(
        ["docker", "network", "rm", net_name],
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )


def _docker_network_create_from_inspect(inspect_entry: dict[str, Any]) -> "subprocess.CompletedProcess[str]":
    """Reconstruct a Docker network from a captured inspect entry (rollback).

    This is intentionally lossy — Docker's internal IDs and auto-added labels
    cannot be restored.  The intent (name + driver + options) is preserved.
    """
    name = inspect_entry.get("Name", "")
    driver = inspect_entry.get("Driver", "bridge")
    options = inspect_entry.get("Options") or {}
    ipam = inspect_entry.get("IPAM") or {}
    ipam_config = (ipam.get("Config") or [{}])[0] if ipam.get("Config") else {}

    argv = ["docker", "network", "create", "--driver", driver]
    for k, v in options.items():
        argv += ["--opt", f"{k}={v}"]
    if ipam_config.get("Subnet"):
        argv += ["--subnet", ipam_config["Subnet"]]
    if ipam_config.get("Gateway"):
        argv += ["--gateway", ipam_config["Gateway"]]
    argv.append(name)

    return subprocess.run(
        argv,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# Precondition check
# ---------------------------------------------------------------------------


def _check_no_running_nodes(labs_dir: Path) -> list[str]:
    """Return a list of error strings for any lab that has running nodes.

    An empty list means the precondition is satisfied.
    """
    violations: list[str] = []
    for lab_path in sorted(labs_dir.rglob("*.json")):
        if lab_path.stem.endswith(".tmp"):
            continue
        try:
            data = json.loads(lab_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("schema") != 2:
            continue
        nodes = data.get("nodes") or {}
        running = [
            str(nid)
            for nid, node in nodes.items()
            if isinstance(node, dict) and node.get("status") == 2
        ]
        if running:
            violations.append(
                f"  {lab_path.relative_to(labs_dir)}: nodes still running: {', '.join(running)}"
            )
    return violations


# ---------------------------------------------------------------------------
# Per-network migration logic
# ---------------------------------------------------------------------------


class _NetworkJournalEntry:
    """Records host-side actions taken for one network (for rollback)."""

    def __init__(self, network_id: int) -> None:
        self.network_id = network_id
        self.old_docker_net: str | None = None
        self.old_docker_inspect: dict[str, Any] | None = None
        self.docker_net_removed: bool = False
        self.new_bridge: str | None = None
        self.bridge_added: bool = False


def _get_docker_net_name(network: dict[str, Any], lab_id: str) -> str | None:
    """Derive the likely legacy Docker network name from a network record.

    Pre-US-202 code named the Docker network after the lab_id + network name.
    Check ``config.docker_network`` first (explicit), then derive the
    conventional name ``nova-ve-{lab_id}-{network_name}``.
    """
    config = network.get("config") or {}
    explicit = config.get("docker_network")
    if explicit:
        return str(explicit)
    # Conventional name used by old _ensure_docker_network.
    name = network.get("name") or ""
    if not name:
        return None
    return f"nova-ve-{lab_id}-{name}"


def _migrate_network(
    network_id: int,
    network: dict[str, Any],
    lab_id: str,
    journal: _NetworkJournalEntry,
    *,
    dry_run: bool,
) -> None:
    """Migrate a single network. Mutates ``journal`` to record host-side actions.

    Raises on unrecoverable errors (caller handles per-lab rollback).
    The caller must append ``journal`` to its list BEFORE calling this
    function so that partial state is captured even if an exception is raised.
    """
    bridge = host_net.bridge_name(lab_id, network_id)
    journal.new_bridge = bridge

    # Check for a legacy Docker network that needs to be removed.
    docker_net = _get_docker_net_name(network, lab_id)
    if docker_net:
        # Step 1: Assert no containers attached.
        inspect_proc = _docker_network_inspect(docker_net)
        if inspect_proc.returncode != 0:
            # Network does not exist — nothing to clean up on the Docker side.
            docker_net = None
        else:
            try:
                inspect_data = json.loads(inspect_proc.stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"could not parse docker network inspect output for '{docker_net}': {exc}"
                )
            if not inspect_data:
                docker_net = None
            else:
                entry = inspect_data[0] if isinstance(inspect_data, list) else inspect_data
                containers = entry.get("Containers") or {}
                if containers:
                    raise RuntimeError(
                        f"network '{docker_net}' has running containers despite "
                        "labs-stopped precondition; investigate manually"
                    )
                # Step 2: Capture inspect JSON for rollback (fail-closed).
                journal.old_docker_net = docker_net
                journal.old_docker_inspect = entry

                if not dry_run:
                    # Step 3: Remove the old Docker network.
                    rm_proc = _docker_network_rm(docker_net)
                    if rm_proc.returncode != 0:
                        raise RuntimeError(
                            f"docker network rm '{docker_net}' failed: "
                            f"{rm_proc.stderr.strip()}"
                        )
                    journal.docker_net_removed = True

    if not dry_run and host_net.bridge_exists(bridge):
        status = host_net.bridge_fingerprint_check(bridge, lab_id, network_id)
        if status != "match":
            actual = host_net.bridge_fingerprint_read(bridge)
            actual_desc = "absent" if actual is None else json.dumps(actual, sort_keys=True)
            raise RuntimeError(
                "bridge ownership check failed for "
                f"{bridge}: expected lab_id={lab_id!r}, network_id={network_id}; "
                f"actual fingerprint={actual_desc}"
            )


# ---------------------------------------------------------------------------
# Per-lab rollback
# ---------------------------------------------------------------------------


def _rollback_lab(
    journals: list[_NetworkJournalEntry],
    lab_path: Path,
    backup_path: Path,
    labs_dir: Path,
    orphan_dir: Path,
    *,
    dry_run: bool,
) -> None:
    """Attempt to undo host-side actions recorded in journals.

    Restores lab.json from backup. If host rollback also fails, writes a
    migration-orphans file with manual remediation steps.
    """
    if not dry_run:
        # Restore lab.json from backup.
        try:
            shutil.copy2(backup_path, lab_path)
        except Exception as exc:
            print(
                f"[migrate_runtime_network] CRITICAL: could not restore backup "
                f"{backup_path} → {lab_path}: {exc}",
                file=sys.stderr,
            )

    orphans: list[dict[str, Any]] = []
    for j in journals:
        # Undo bridge_add.
        if j.bridge_added and j.new_bridge:
            try:
                if not dry_run:
                    host_net.bridge_del(j.new_bridge)
            except Exception as exc:
                orphans.append(
                    {
                        "action": "bridge_del",
                        "bridge": j.new_bridge,
                        "error": str(exc),
                        "remediation": f"Run: sudo ip link delete {j.new_bridge} type bridge",
                    }
                )

        # Recreate old Docker network.
        if j.docker_net_removed and j.old_docker_inspect:
            try:
                if not dry_run:
                    proc = _docker_network_create_from_inspect(j.old_docker_inspect)
                    if proc.returncode != 0:
                        raise RuntimeError(proc.stderr.strip())
            except Exception as exc:
                net_name = j.old_docker_net or "<unknown>"
                orphans.append(
                    {
                        "action": "docker_network_create",
                        "docker_net": net_name,
                        "error": str(exc),
                        "remediation": (
                            f"Manually recreate docker network '{net_name}' "
                            "with the driver/subnet from the captured inspect JSON below.\n"
                            f"inspect_snapshot: {json.dumps(j.old_docker_inspect)}"
                        ),
                    }
                )

    if orphans:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        orphan_file = orphan_dir / f"migration-orphans-{ts}.json"
        try:
            orphan_dir.mkdir(parents=True, exist_ok=True)
            orphan_file.write_text(json.dumps(orphans, indent=2), encoding="utf-8")
            print(
                f"[migrate_runtime_network] Rollback partial — orphan manifest written to "
                f"{orphan_file}",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"[migrate_runtime_network] Could not write orphan manifest: {exc}",
                file=sys.stderr,
            )


# ---------------------------------------------------------------------------
# Pre-Wave-7 node.machine_override backfill (compat discriminator)
# ---------------------------------------------------------------------------


def _stamp_machine_override(nodes: dict[str, Any]) -> int:
    """Mutate ``nodes`` in place: stamp ``machine_override='pc'`` on every
    QEMU node that lacks the field. Returns the count of nodes stamped.

    Only QEMU nodes are touched. Docker/iol/dynamips nodes are left alone.
    A node is considered "lacking" the field if ``machine_override`` is
    missing OR explicitly ``None``. Any other value (e.g. user-chosen
    ``'q35'``) is preserved — that is the idempotency contract required by
    ``.omc/plans/network-runtime-wiring.md:397-400``.
    """
    stamped = 0
    for _key, node in nodes.items():
        if not isinstance(node, dict):
            continue
        if node.get("type") != "qemu":
            continue
        if node.get("machine_override") is not None:
            continue
        node["machine_override"] = "pc"
        stamped += 1
    return stamped


# ---------------------------------------------------------------------------
# Per-lab processing
# ---------------------------------------------------------------------------


def process_lab(
    lab_path: Path,
    labs_dir: Path,
    backup_dir: Path,
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Process a single lab file.

    Returns ``(networks_checked, networks_backfilled, nodes_stamped)``.
    ``nodes_stamped`` counts QEMU nodes that received
    ``machine_override='pc'`` in this pass.

    Raises on unrecoverable per-lab errors (caller catches and continues).
    """
    try:
        rel = lab_path.relative_to(labs_dir)
    except ValueError:
        rel = Path(lab_path.name)
    lab_id_path = Path(rel).as_posix()

    with lab_lock(lab_id_path, labs_dir):
        raw = lab_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)

        if not isinstance(data, dict) or data.get("schema") != 2:
            return 0, 0, 0

        networks: dict[str, Any] = data.get("networks") or {}
        nodes: dict[str, Any] = data.get("nodes") or {}

        lab_id = str(data.get("id") or lab_id_path)
        checked = 0
        to_migrate: list[tuple[int, dict[str, Any]]] = []

        for key, network in networks.items():
            if not isinstance(network, dict):
                continue
            try:
                net_id = int(network.get("id", key))
            except (TypeError, ValueError):
                continue
            checked += 1
            runtime = network.get("runtime") or {}
            if not runtime.get("bridge_name"):
                to_migrate.append((net_id, network))

        # Count pre-Wave-7 QEMU nodes that need machine_override='pc'.
        nodes_to_stamp = sum(
            1
            for n in nodes.values()
            if isinstance(n, dict)
            and n.get("type") == "qemu"
            and n.get("machine_override") is None
        )

        if not to_migrate and nodes_to_stamp == 0:
            # Already fully migrated — idempotent no-op.
            return checked, 0, 0

        if dry_run:
            for net_id, _net in to_migrate:
                bridge = host_net.bridge_name(lab_id, net_id)
                print(
                    f"[migrate_runtime_network] DRY RUN: {lab_path.name} "
                    f"network {net_id} → would stamp runtime.bridge_name={bridge}"
                )
            if nodes_to_stamp:
                print(
                    f"[migrate_runtime_network] DRY RUN: {lab_path.name} "
                    f"would stamp machine_override='pc' on {nodes_to_stamp} "
                    "QEMU node(s)"
                )
            return checked, len(to_migrate), nodes_to_stamp

        # Take a backup of the lab.json before any modifications.
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / lab_path.name
        shutil.copy2(lab_path, backup_path)

        journals: list[_NetworkJournalEntry] = []
        failed = False
        fail_msg = ""

        for net_id, network in to_migrate:
            # Pre-append journal so partial host-side state is captured even
            # if _migrate_network raises mid-way (e.g. after docker network rm
            # but before bridge_add succeeds).
            journal = _NetworkJournalEntry(net_id)
            journals.append(journal)
            try:
                _migrate_network(net_id, network, lab_id, journal, dry_run=dry_run)
            except Exception as exc:
                failed = True
                fail_msg = str(exc)
                print(
                    f"[migrate_runtime_network] ERROR migrating {lab_path.name} "
                    f"network {net_id}: {exc}",
                    file=sys.stderr,
                )
                break

        if failed:
            # Roll back all host-side actions for this lab.
            _rollback_lab(
                journals,
                lab_path,
                backup_path,
                labs_dir,
                backup_dir,
                dry_run=dry_run,
            )
            raise RuntimeError(
                f"migration failed for {lab_path.name}: {fail_msg}"
            )

        # Step 5: Stamp runtime.bridge_name for all successfully migrated networks.
        for net_id, network in to_migrate:
            bridge = host_net.bridge_name(lab_id, net_id)
            runtime = network.setdefault("runtime", {})
            runtime["bridge_name"] = bridge
            networks[str(net_id)] = network

        # Step 6: Stamp machine_override='pc' on pre-Wave-7 QEMU nodes
        # (US-202b compat discriminator — see plan :397-400). Done in the
        # same read-modify-write cycle so we never double-write the file.
        nodes_stamped = _stamp_machine_override(nodes)

        data["networks"] = networks
        data["nodes"] = nodes
        data.pop("topology", None)

        # Atomic write.
        tmp_path = lab_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp_path, lab_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    return checked, len(to_migrate), nodes_stamped


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--labs-dir",
        type=Path,
        default=Path("/var/lib/nova-ve/labs"),
        help="Path to the labs directory (default: /var/lib/nova-ve/labs)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would change without writing any files or modifying host state",
    )
    args = parser.parse_args(argv)

    labs_dir: Path = args.labs_dir.resolve()
    dry_run: bool = args.dry_run

    if not labs_dir.is_dir():
        print(
            f"[migrate_runtime_network] ERROR: labs_dir does not exist: {labs_dir}",
            file=sys.stderr,
        )
        return 1

    # ---------------------------------------------------------------------------
    # PRECONDITION: no running nodes.
    # ---------------------------------------------------------------------------
    violations = _check_no_running_nodes(labs_dir)
    if violations:
        print(
            "[migrate_runtime_network] PRECONDITION FAILED: the following labs have "
            "running nodes (status=2).",
            file=sys.stderr,
        )
        for v in violations:
            print(v, file=sys.stderr)
        print(
            "\nStop the backend with:  sudo systemctl stop nova-ve-backend\n"
            "Stop running labs via:  nova-ve cli stop-lab <id>\n"
            "Then re-run this script.",
            file=sys.stderr,
        )
        return 1

    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = labs_dir / f".migration-backup-{ts}"

    if dry_run:
        print(
            f"[migrate_runtime_network] DRY RUN — no files written, no host changes. "
            f"labs_dir={labs_dir}"
        )
    else:
        print(
            f"[migrate_runtime_network] Starting bridge_name backfill migration. "
            f"labs_dir={labs_dir}"
        )

    lab_files = sorted(labs_dir.rglob("*.json"))
    total_labs = 0
    total_backfilled = 0
    total_nodes_stamped = 0
    errors = 0

    for lab_path in lab_files:
        if lab_path.stem.endswith(".tmp"):
            continue
        # Skip files inside backup dirs.
        if ".migration-backup-" in str(lab_path):
            continue

        try:
            checked, backfilled, nodes_stamped = process_lab(
                lab_path, labs_dir, backup_dir, dry_run=dry_run
            )
        except Exception as exc:
            print(
                f"[migrate_runtime_network] ERROR processing {lab_path.name}: {exc}",
                file=sys.stderr,
            )
            errors += 1
            continue

        if checked == 0 and nodes_stamped == 0:
            continue

        total_labs += 1
        total_backfilled += backfilled
        total_nodes_stamped += nodes_stamped

        if backfilled > 0 or nodes_stamped > 0:
            action = "would backfill" if dry_run else "backfilled"
            parts: list[str] = []
            if backfilled > 0:
                parts.append(f"runtime.bridge_name on {backfilled} network(s)")
            if nodes_stamped > 0:
                parts.append(f"machine_override='pc' on {nodes_stamped} QEMU node(s)")
            print(
                f"[migrate_runtime_network] {lab_path.name}: {action} "
                + " and ".join(parts)
            )
        else:
            print(
                f"[migrate_runtime_network] {lab_path.name}: already migrated (no-op)"
            )

    suffix = " (dry run)" if dry_run else ""
    print(
        f"[migrate_runtime_network] Done{suffix}. "
        f"labs={total_labs}, networks_backfilled={total_backfilled}, "
        f"nodes_stamped={total_nodes_stamped}, errors={errors}"
    )
    if errors and not dry_run:
        print(
            f"[migrate_runtime_network] Backup dir (for manual inspection): {backup_dir}",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
