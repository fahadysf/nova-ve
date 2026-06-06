# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Host-wide cloud inventory helpers.

NAT-Cloud runtime is host-owned, even though its source of truth lives in
lab.json. These helpers keep cross-lab scans and ownership checks in one
place so create/delete/IPAM paths agree on which record owns a cloud.
"""

from __future__ import annotations

import fcntl
import ipaddress
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from app.config import get_settings

NAT_CLOUD_TYPE = "nat_cloud"


@dataclass(frozen=True)
class CloudNetworkRef:
    cloud_id: str
    lab_path: str
    lab_id: str
    lab_name: str
    network_id: int
    network_name: str
    network: dict[str, Any]
    data: dict[str, Any]
    is_reference: bool
    safe_for_reuse: bool = True
    warning: str | None = None

    @property
    def bridge_name(self) -> str | None:
        runtime = self.network.get("runtime") or {}
        if isinstance(runtime, dict):
            bridge = runtime.get("bridge_name")
            if isinstance(bridge, str) and bridge:
                return bridge
        return None


class SharedCloudReferenceError(Exception):
    """Raised when deleting an owner would strand shared cloud references."""

    def __init__(self, message: str, *, references: list[dict[str, Any]]):
        super().__init__(message)
        self.code = 409
        self.message = message
        self.references = references


def _labs_dir() -> Path:
    return Path(get_settings().LABS_DIR).resolve()


def _base_data_dir() -> Path:
    settings = get_settings()
    raw = getattr(settings, "BASE_DATA_DIR", None)
    return Path(raw).resolve() if raw else _labs_dir().parent.resolve()


def _normalize_relative_path(raw_path: str) -> str:
    candidate = raw_path.strip().replace("\\", "/").strip("/")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Invalid lab path: directory traversal detected")
    return Path(*parts).as_posix()


@contextmanager
def nat_cloud_ipam_lock(timeout_s: float = 5.0) -> Iterator[None]:
    lock_path = _base_data_dir() / "nat-cloud-ipam.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as fh:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for NAT-Cloud IPAM lock")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def nat_cloud_id(lab_id: str, network_id: int) -> str:
    return f"nat-cloud:{lab_id}:{int(network_id)}"


def _lab_json_files(labs_dir: Path | None = None) -> list[Path]:
    root = (labs_dir or _labs_dir()).resolve()
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*.json")
        if path.is_file() and not path.name.endswith(".lock")
    )


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _relative_lab_path(path: Path, labs_dir: Path) -> str:
    return path.resolve().relative_to(labs_dir.resolve()).as_posix()


def _cloud_refs(labs_dir: Path | None = None) -> list[CloudNetworkRef]:
    root = (labs_dir or _labs_dir()).resolve()
    refs: list[CloudNetworkRef] = []
    for path in _lab_json_files(root):
        data = _read_json(path)
        if not data:
            continue
        networks = data.get("networks") or {}
        if not isinstance(networks, dict):
            continue
        lab_path = _relative_lab_path(path, root)
        lab_id = str(data.get("id") or lab_path)
        meta = data.get("meta") or {}
        lab_name = str(meta.get("name") or lab_path) if isinstance(meta, dict) else lab_path
        for raw_network_id, network in networks.items():
            if not isinstance(network, dict):
                continue
            if str(network.get("type", "")) != NAT_CLOUD_TYPE:
                continue
            try:
                network_id = int(network.get("id", raw_network_id))
            except (TypeError, ValueError):
                continue
            config = network.get("config") or {}
            shared_cloud_id = config.get("shared_cloud_id") if isinstance(config, dict) else None
            cloud_id = str(shared_cloud_id) if shared_cloud_id else nat_cloud_id(lab_id, network_id)
            refs.append(
                CloudNetworkRef(
                    cloud_id=cloud_id,
                    lab_path=lab_path,
                    lab_id=lab_id,
                    lab_name=lab_name,
                    network_id=network_id,
                    network_name=str(network.get("name") or f"Network {network_id}"),
                    network=network,
                    data=data,
                    is_reference=bool(shared_cloud_id),
                )
            )
    return refs


def list_nat_clouds(labs_dir: Path | None = None) -> list[dict[str, Any]]:
    refs = _cloud_refs(labs_dir)
    owners = [ref for ref in refs if not ref.is_reference]
    colliding: dict[str, str] = {}
    owner_networks: dict[str, ipaddress.IPv4Network] = {}
    for owner in owners:
        config = owner.network.get("config") or {}
        cidr = config.get("cidr") if isinstance(config, dict) else None
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(str(cidr), strict=True)
        except ValueError:
            colliding[owner.cloud_id] = "invalid CIDR"
            continue
        if isinstance(net, ipaddress.IPv6Network):
            colliding[owner.cloud_id] = "IPv6 CIDR is not supported for NAT-Cloud"
            continue
        for other_id, other_net in owner_networks.items():
            if net.overlaps(other_net):
                colliding[owner.cloud_id] = f"CIDR overlaps {other_id}"
                colliding[other_id] = f"CIDR overlaps {owner.cloud_id}"
        owner_networks[owner.cloud_id] = net

    out: list[dict[str, Any]] = []
    for ref in refs:
        config = ref.network.get("config") or {}
        runtime = ref.network.get("runtime") or {}
        safe_for_reuse = not ref.is_reference and ref.cloud_id not in colliding
        out.append(
            {
                "id": ref.cloud_id,
                "type": NAT_CLOUD_TYPE,
                "lab_id": ref.lab_id,
                "lab_path": ref.lab_path,
                "lab_name": ref.lab_name,
                "network_id": ref.network_id,
                "network_name": ref.network_name,
                "bridge_name": runtime.get("bridge_name") if isinstance(runtime, dict) else None,
                "cidr": config.get("cidr") if isinstance(config, dict) else None,
                "gateway": config.get("gateway") if isinstance(config, dict) else None,
                "dhcp": config.get("dhcp", True) if isinstance(config, dict) else True,
                "dhcp_start": config.get("dhcp_start") if isinstance(config, dict) else None,
                "dhcp_end": config.get("dhcp_end") if isinstance(config, dict) else None,
                "egress_interface": config.get("egress_interface") if isinstance(config, dict) else None,
                "shared_cloud_id": config.get("shared_cloud_id") if isinstance(config, dict) else None,
                "is_reference": ref.is_reference,
                "safe_for_reuse": safe_for_reuse,
                "warning": colliding.get(ref.cloud_id),
            }
        )
    return out


def owner_cidrs(labs_dir: Path | None = None, *, exclude_cloud_id: str | None = None) -> list[ipaddress.IPv4Network]:
    out: list[ipaddress.IPv4Network] = []
    for item in list_nat_clouds(labs_dir):
        if item.get("is_reference"):
            continue
        if exclude_cloud_id and item.get("id") == exclude_cloud_id:
            continue
        cidr = item.get("cidr")
        if not cidr:
            continue
        try:
            net = ipaddress.ip_network(str(cidr), strict=True)
        except ValueError:
            continue
        if isinstance(net, ipaddress.IPv4Network):
            out.append(net)
    return out


def find_nat_cloud_owner(shared_cloud_id: str, labs_dir: Path | None = None) -> CloudNetworkRef | None:
    for ref in _cloud_refs(labs_dir):
        if ref.is_reference:
            continue
        if ref.cloud_id == shared_cloud_id:
            return ref
    return None


def references_to_cloud(shared_cloud_id: str, labs_dir: Path | None = None) -> list[CloudNetworkRef]:
    return [
        ref
        for ref in _cloud_refs(labs_dir)
        if ref.is_reference and ref.cloud_id == shared_cloud_id
    ]


def external_reference_summaries_for_scope(scope_path: str) -> list[dict[str, Any]]:
    normalized = _normalize_relative_path(scope_path)
    in_scope_prefix = normalized.rstrip("/")
    refs = _cloud_refs()
    owner_ids: set[str] = set()
    for ref in refs:
        if ref.is_reference:
            continue
        if ref.lab_path == in_scope_prefix or ref.lab_path.startswith(f"{in_scope_prefix}/"):
            owner_ids.add(ref.cloud_id)

    blockers: list[dict[str, Any]] = []
    for ref in refs:
        if not ref.is_reference or ref.cloud_id not in owner_ids:
            continue
        if ref.lab_path == in_scope_prefix or ref.lab_path.startswith(f"{in_scope_prefix}/"):
            continue
        blockers.append(
            {
                "cloud_id": ref.cloud_id,
                "lab_path": ref.lab_path,
                "lab_id": ref.lab_id,
                "network_id": ref.network_id,
                "network_name": ref.network_name,
            }
        )
    return blockers


def assert_no_external_nat_cloud_references_for_scope(scope_path: str) -> None:
    blockers = external_reference_summaries_for_scope(scope_path)
    if blockers:
        raise SharedCloudReferenceError(
            "Cannot delete NAT-Cloud owner while it is used by another lab.",
            references=blockers,
        )
