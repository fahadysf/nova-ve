# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Cross-lab network sharing (#319).

A shared network stays in the lab that created it (the *owner* record, flagged
``shared: true``). Another lab joins it with a *member* record that carries the
owner's identity in ``config`` and the owner's bridge in ``runtime``:

* ``config.shared_network_id`` for ordinary bridges;
* ``config.shared_cloud_id`` for NAT-Clouds (the older admin reuse path, which
  already routes IPAM and host L3 state to the owner).

Both ids end in ``<owner lab id>:<owner network id>``, which is the key used
here, so every lifecycle guard treats the two kinds alike.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterator

from app.services import cloud_inventory_service as inventory
from app.services import host_net

logger = logging.getLogger("nova-ve")

SHARED_NETWORK_PREFIX = "shared-net:"
NAT_CLOUD_PREFIX = "nat-cloud:"
NAT_CLOUD_TYPE = inventory.NAT_CLOUD_TYPE

# Config keys that make a record a member of someone else's network. Only
# ``create_network`` may write them.
MEMBERSHIP_CONFIG_KEYS = ("shared_network_id", "shared_cloud_id")


@dataclass(frozen=True)
class LabNetwork:
    lab_path: str
    lab_id: str
    lab_name: str
    network_id: int
    network: dict[str, Any]
    data: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.lab_id}:{self.network_id}"

    @property
    def name(self) -> str:
        return str(self.network.get("name") or f"Network {self.network_id}")

    @property
    def type(self) -> str:
        return str(self.network.get("type") or "linux_bridge")

    @property
    def shared(self) -> bool:
        return self.network.get("shared") is True

    @property
    def bridge_name(self) -> str | None:
        runtime = self.network.get("runtime")
        bridge = runtime.get("bridge_name") if isinstance(runtime, dict) else None
        return bridge if isinstance(bridge, str) and bridge else None


def shared_network_id(lab_id: str, network_id: int) -> str:
    return f"{SHARED_NETWORK_PREFIX}{lab_id}:{int(network_id)}"


def _key_from_id(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    for prefix in (SHARED_NETWORK_PREFIX, NAT_CLOUD_PREFIX):
        if raw.startswith(prefix):
            return raw[len(prefix):] or None
    return None


def member_key(network: Any) -> str | None:
    """Owner key when ``network`` is a member record, else ``None``."""
    config = network.get("config") if isinstance(network, dict) else None
    if not isinstance(config, dict):
        return None
    # A NAT-Cloud is joined through its own id; that id means nothing on any
    # other type of record.
    if str(network.get("type", "")) == NAT_CLOUD_TYPE:
        return _key_from_id(config.get("shared_cloud_id"))
    return _key_from_id(config.get("shared_network_id"))


def is_member(network: Any) -> bool:
    """True when the record must never own (or tear down) its bridge.

    ``runtime.shared_reference`` is service-managed, so it still identifies a
    member whose ``config`` was edited by hand.
    """
    if not isinstance(network, dict):
        return False
    config = network.get("config")
    if isinstance(config, dict):
        is_nat = str(network.get("type", "")) == NAT_CLOUD_TYPE
        if config.get("shared_cloud_id" if is_nat else "shared_network_id"):
            return True
    runtime = network.get("runtime")
    return isinstance(runtime, dict) and runtime.get("shared_reference") is True


def bridge_identity(lab_id: str, network_id: int, network: Any) -> tuple[str, int]:
    """``(lab_id, network_id)`` the bridge's ownership fingerprint must carry."""
    key = member_key(network)
    if key:
        owner_lab_id, _, raw_id = key.rpartition(":")
        if owner_lab_id and raw_id.isdigit():
            return owner_lab_id, int(raw_id)
    return str(lab_id), int(network_id)


def unshareable_reason(network: dict[str, Any]) -> str | None:
    """Why this network cannot be offered to other labs, or ``None``."""
    if is_member(network):
        return "This network is joined from another lab; change sharing in the lab that owns it."
    network_type = str(network.get("type") or "linux_bridge")
    if network_type == "bridge_cloud":
        return "Bridge-Cloud networks already span labs through their host bridge."
    if network_type.startswith("pnet"):
        return "Host-attached pnet networks already span labs."
    return None


def patch_violation(network: dict[str, Any], patch: dict[str, Any]) -> str | None:
    """Message when a generic update would break or forge sharing."""
    new_type = patch.get("type")
    type_changes = new_type is not None and new_type != network.get("type", "linux_bridge")
    new_config = patch.get("config")
    if is_member(network):
        if type_changes or new_config is not None:
            return (
                "This network is joined from another lab; its type and config "
                "are managed by the lab that owns it."
            )
        return None
    if isinstance(new_config, dict) and any(new_config.get(k) for k in MEMBERSHIP_CONFIG_KEYS):
        return "Joining a shared network is only possible when the network is created."
    stored = network.get("config")
    if type_changes and isinstance(stored, dict) and any(
        stored.get(k) for k in MEMBERSHIP_CONFIG_KEYS
    ):
        # Left by an earlier release; a type change would bring it to life.
        return "This network carries a leftover sharing id; delete and recreate it instead."
    if type_changes and network.get("shared") is True:
        return "Stop sharing this network before changing its type."
    return None


def iter_lab_networks() -> Iterator[LabNetwork]:
    """Every network record of every lab on this host."""
    root = inventory._labs_dir()
    for path in inventory._lab_json_files(root):
        data = inventory._read_json(path)
        networks = data.get("networks") if data else None
        if not isinstance(networks, dict):
            continue
        lab_path = inventory._relative_lab_path(path, root)
        meta = data.get("meta")
        lab_name = meta.get("name") if isinstance(meta, dict) else None
        for raw_id, network in networks.items():
            if not isinstance(network, dict):
                continue
            try:
                network_id = int(network.get("id", raw_id))
            except (TypeError, ValueError):
                continue
            yield LabNetwork(
                lab_path=lab_path,
                lab_id=str(data.get("id") or lab_path),
                lab_name=str(lab_name or path.stem),
                network_id=network_id,
                network=network,
                data=data,
            )


def find_owner(key: str | None) -> LabNetwork | None:
    if not key:
        return None
    return _pick_owner(
        [i for i in iter_lab_networks() if i.key == key and not is_member(i.network)]
    )


def _pick_owner(candidates: list[LabNetwork]) -> LabNetwork | None:
    """Copies of a lab file under LABS_DIR repeat its id; prefer the one sharing."""
    if len(candidates) > 1:
        logger.warning(
            "shared network %s is claimed by several lab files: %s",
            candidates[0].key,
            ", ".join(item.lab_path for item in candidates),
        )
    return next((item for item in candidates if item.shared), candidates[0] if candidates else None)


def find_shared_owner(network_id: str) -> LabNetwork | None:
    """Owner behind a ``shared_network_id``, only while it is still shared."""
    if not str(network_id).startswith(SHARED_NETWORK_PREFIX):
        return None
    owner = find_owner(_key_from_id(network_id))
    return owner if owner is not None and owner.shared else None


def members_of(key: str) -> list[LabNetwork]:
    return [item for item in iter_lab_networks() if member_key(item.network) == key]


def segment(key: str) -> list[LabNetwork]:
    """Owner (first) and members of one shared layer 2 segment."""
    owner: list[LabNetwork] = []
    members: list[LabNetwork] = []
    for item in iter_lab_networks():
        if member_key(item.network) == key:
            members.append(item)
        elif item.key == key and not is_member(item.network):
            owner.append(item)
    chosen = _pick_owner(owner)
    return [*([chosen] if chosen else []), *members]


def reclaim_fingerprint(bridge: str, owner_lab_id: str, owner_network_id: int) -> bool:
    """Give a shared bridge's fingerprint back to its owner.

    Earlier releases stamped a bridge recreated from a joined lab with that
    lab's identity, which made the owner refuse its own bridge. Only a stamp
    naming a current member is replaced; anything else stays a mismatch.
    """
    stamp = host_net.bridge_fingerprint_read(bridge) or {}
    claimed = (stamp.get("lab_id"), stamp.get("network_id"))
    members = members_of(f"{owner_lab_id}:{int(owner_network_id)}")
    if claimed not in {(item.lab_id, item.network_id) for item in members}:
        return False
    host_net.bridge_fingerprint_write(bridge, owner_lab_id, owner_network_id)
    return True


def lab_names(items: list[LabNetwork]) -> str:
    return ", ".join(sorted({item.lab_name for item in items}))


def list_shared_networks(for_lab_path: str | None = None) -> list[dict[str, Any]]:
    """Networks any lab may join. Carries no path or host details.

    ``for_lab_path`` leaves out what that lab owns or already joined.
    """
    items = list(iter_lab_networks())
    taken: set[str] = set()
    if for_lab_path:
        for_lab_path = inventory._normalize_relative_path(for_lab_path)
        taken = {
            member_key(i.network) or i.key for i in items if i.lab_path == for_lab_path
        }
    out: list[dict[str, Any]] = []
    for item in items:
        if item.key in taken:
            continue
        if not item.shared or item.network.get("implicit") is True:
            continue
        if unshareable_reason(item.network):
            continue
        config = item.network.get("config")
        out.append(
            {
                "id": shared_network_id(item.lab_id, item.network_id),
                "type": item.type,
                "name": item.name,
                "lab_id": item.lab_id,
                "lab_name": item.lab_name,
                "cidr": config.get("cidr") if isinstance(config, dict) else None,
            }
        )
    return sorted(out, key=lambda entry: (entry["lab_name"].lower(), entry["name"].lower()))


def external_members_for_scope(scope_path: str) -> list[LabNetwork]:
    """Members outside a lab/folder that depend on an owner inside it."""
    prefix = inventory._normalize_relative_path(scope_path).rstrip("/")

    def in_scope(path: str) -> bool:
        return path == prefix or path.startswith(f"{prefix}/")

    items = list(iter_lab_networks())
    owned = {i.key for i in items if in_scope(i.lab_path) and not is_member(i.network)}
    return [i for i in items if not in_scope(i.lab_path) and member_key(i.network) in owned]


def assert_no_external_members_for_scope(scope_path: str) -> None:
    blockers = external_members_for_scope(scope_path)
    if blockers:
        raise inventory.SharedCloudReferenceError(
            f"Cannot delete: a shared network is used by another lab ({lab_names(blockers)}).",
            references=[
                {
                    "lab_path": item.lab_path,
                    "lab_id": item.lab_id,
                    "network_id": item.network_id,
                    "network_name": item.name,
                }
                for item in blockers
            ],
        )
