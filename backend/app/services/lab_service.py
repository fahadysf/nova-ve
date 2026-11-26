import ipaddress
import json
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.lab import LabMeta
from app.services import host_net
from app.services.cloud_inventory_service import LINUX_BRIDGE_TYPE, bridge_dhcp_enabled
from app.services.console_recording import RECORDINGS_FOLDER
from app.services.lab_lock import lab_lock, lab_lock_async
from app.services.shared_network_service import (
    assert_no_external_members_for_scope,
    is_member,
)


SCHEMA_VERSION = 2
NAT_CLOUD_TYPE = "nat_cloud"
BRIDGE_CLOUD_TYPE = "bridge_cloud"
logger = logging.getLogger(__name__)

LEGACY_SCHEMA_ERROR = "Lab uses legacy schema; run scripts/migrate_lab_v1_to_v2.py"

# Fields that read_lab_json_static synthesises into the in-memory dict so that
# legacy router code keeps working. They MUST be stripped before persisting.
LEGACY_COMPAT_KEYS = ("topology",)
LEGACY_INTERFACE_COMPAT_KEYS = ("network_id",)
LEGACY_NETWORK_COMPAT_KEYS = ("count",)


def _labs_dir() -> Path:
    return get_settings().LABS_DIR.resolve()


def _normalize_relative_lab_path(raw_path: str) -> str:
    """Normalize a LABS_DIR-relative path and reject traversal."""
    candidate = raw_path.strip().replace("\\", "/").strip("/")
    parts = [part for part in candidate.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Invalid lab path: directory traversal detected")
    if RECORDINGS_FOLDER in parts:
        raise ValueError("The console recordings directory is reserved.")
    return Path(*parts).as_posix()


def _lab_file_path(relative_path: str) -> Path:
    labs_dir = _labs_dir()
    normalized_relative_path = _normalize_relative_lab_path(relative_path)
    filepath = (labs_dir / normalized_relative_path).resolve()
    if not str(filepath).startswith(str(labs_dir)):
        raise ValueError("Invalid lab path: directory traversal detected")
    return filepath


def _replace_json(filepath: Path, data: dict) -> None:
    """Write a lab file in one step.

    #319: other labs read this file without its lock to decide whether a
    shared bridge is in use, so it must never be seen empty or half-written.
    """
    # Not ``*.json``: lab scans must not pick the temporary file up.
    tmp_path = filepath.with_name(f".{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        if filepath.exists():
            shutil.copymode(filepath, tmp_path)
        os.replace(tmp_path, filepath)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _safe_lab_filename(name: str) -> str:
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-_")
    return safe_name or "lab"


def _api_lab_path(relative_path: str) -> str:
    return "/" + _normalize_relative_lab_path(relative_path)


def build_relative_lab_path(
    name: str,
    path: str | None = None,
    filename: str | None = None,
) -> str:
    """Build a relative lab path from API inputs."""
    if filename:
        relative_path = _normalize_relative_lab_path(filename)
        if not relative_path.endswith(".json"):
            relative_path += ".json"
        return relative_path

    generated_name = f"{_safe_lab_filename(name)}.json"
    if path:
        normalized_path = _normalize_relative_lab_path(path)
        if normalized_path.endswith(".json"):
            return normalized_path
        return f"{normalized_path}/{generated_name}"

    return generated_name


def _empty_lab_json(lab_id: str, meta: dict) -> dict:
    """Emit a fresh v2 lab.json shape."""
    return {
        "schema": SCHEMA_VERSION,
        "id": lab_id,
        "meta": meta,
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {},
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
        "textobjects": [],
        "lineobjects": [],
        "pictures": [],
        "tasks": [],
        "configsets": {},
    }


def _derive_legacy_topology(data: dict) -> list:
    topology: list[dict] = []
    nodes = data.get("nodes", {}) or {}
    for link in data.get("links", []) or []:
        endpoint_a = link.get("from") or {}
        endpoint_b = link.get("to") or {}

        if "node_id" in endpoint_a and "node_id" in endpoint_b:
            src_node = nodes.get(str(endpoint_a.get("node_id"))) or {}
            dst_node = nodes.get(str(endpoint_b.get("node_id"))) or {}
            topology.append(
                {
                    "type": "ethernet",
                    "source": f"node{endpoint_a.get('node_id')}",
                    "source_node_name": str(src_node.get("name", "")),
                    "source_type": "node",
                    "source_label": "",
                    "source_interfaceId": int(endpoint_a.get("interface_index", 0)),
                    "destination": f"node{endpoint_b.get('node_id')}",
                    "destination_node_name": str(dst_node.get("name", "")),
                    "destination_type": "node",
                    "destination_label": "",
                    "destination_interfaceId": int(endpoint_b.get("interface_index", 0)),
                    "network_id": 0,
                    "label": str(link.get("label", "") or ""),
                    "color": str(link.get("color", "") or ""),
                    "width": str(link.get("width", "1") or "1"),
                }
            )
            continue

        node_endpoint = endpoint_a if "node_id" in endpoint_a else endpoint_b
        network_endpoint = endpoint_b if "network_id" in endpoint_b else endpoint_a
        if "node_id" not in node_endpoint or "network_id" not in network_endpoint:
            continue

        src_node = nodes.get(str(node_endpoint.get("node_id"))) or {}
        network_id = int(network_endpoint.get("network_id", 0))
        topology.append(
            {
                "type": "ethernet",
                "source": f"node{node_endpoint.get('node_id')}",
                "source_node_name": str(src_node.get("name", "")),
                "source_type": "node",
                "source_label": "",
                "source_interfaceId": int(node_endpoint.get("interface_index", 0)),
                "destination": f"network{network_id}",
                "destination_node_name": "",
                "destination_type": "network",
                "destination_label": "",
                "destination_interfaceId": "network",
                "network_id": network_id,
                "label": str(link.get("label", "") or ""),
                "color": str(link.get("color", "") or ""),
                "width": str(link.get("width", "1") or "1"),
            }
        )

    return topology


def _derive_legacy_interface_network_ids(data: dict) -> None:
    """Inject legacy ``network_id`` into each interface entry from links[]."""

    nodes = data.get("nodes", {}) or {}

    for node in nodes.values():
        for interface in node.get("interfaces", []) or []:
            if isinstance(interface, dict):
                interface["network_id"] = 0

    for link in data.get("links", []) or []:
        endpoint_a = link.get("from") or {}
        endpoint_b = link.get("to") or {}

        node_endpoints: list[dict] = []
        network_endpoint: dict | None = None
        for endpoint in (endpoint_a, endpoint_b):
            if "node_id" in endpoint:
                node_endpoints.append(endpoint)
            elif "network_id" in endpoint:
                network_endpoint = endpoint

        if network_endpoint is None:
            continue
        network_id = int(network_endpoint.get("network_id", 0))
        if not network_id:
            continue
        for endpoint in node_endpoints:
            node = nodes.get(str(endpoint.get("node_id"))) or {}
            interfaces = node.get("interfaces") or []
            index = int(endpoint.get("interface_index", -1))
            if 0 <= index < len(interfaces) and isinstance(interfaces[index], dict):
                interfaces[index]["network_id"] = network_id


def _derive_network_counts(data: dict) -> None:
    """Populate ``count`` on each network from the live link filter."""

    counts: dict[int, int] = {}
    for link in data.get("links", []) or []:
        for endpoint in (link.get("from") or {}, link.get("to") or {}):
            if isinstance(endpoint, dict) and "network_id" in endpoint:
                network_id = int(endpoint.get("network_id", 0))
                if network_id:
                    counts[network_id] = counts.get(network_id, 0) + 1

    for key, network in (data.get("networks") or {}).items():
        if isinstance(network, dict):
            try:
                network_id = int(network.get("id", key))
            except (TypeError, ValueError):
                network_id = 0
            network["count"] = counts.get(network_id, 0)


def _bridge_server_cidr(config: dict) -> str:
    """``server/prefix`` that a bridge running DHCP carries on the host (#325)."""
    prefix = ipaddress.ip_network(str(config["cidr"])).prefixlen
    return f"{config['dhcp_server']}/{prefix}"


def _cleanup_bridge_dhcp_runtime(bridge: str, network: dict, context: str) -> bool:
    """Best-effort removal of an isolated bridge's DHCP server, address and guard.

    Deleting the bridge drops its address, but not the dnsmasq process or the
    nftables guard, which match the bridge by name (#233, #325).

    Returns True when the guard was kept because the address could not be
    removed; the caller drops it once the bridge itself is gone.
    """
    if str(network.get("type") or LINUX_BRIDGE_TYPE) != LINUX_BRIDGE_TYPE or is_member(network):
        return False
    config = network.get("config") if isinstance(network.get("config"), dict) else {}
    runtime = network.get("runtime") if isinstance(network.get("runtime"), dict) else {}
    if not (bridge_dhcp_enabled(network) or runtime.get("dhcp") is True):
        return False
    server = runtime.get("dhcp_server") or config.get("dhcp_server")
    try:
        address = _bridge_server_cidr({"cidr": config.get("cidr"), "dhcp_server": server})
    except ValueError:
        address = None
    steps = [("dnsmasq_stop", lambda: host_net.dnsmasq_stop(bridge))]
    if server and address:
        steps.append(("bridge_addr_del", lambda: host_net.bridge_addr_del(bridge, address)))
    steps.append(("bridge_guard_remove", lambda: host_net.bridge_guard_remove(bridge)))
    for name, step in steps:
        try:
            step()
        except host_net.HostNetError as exc:
            logger.warning("%s: %s(%s) failed (%s)", context, name, bridge, exc)
            if name == "bridge_addr_del":
                # Never leave an address unguarded. Rules for a bridge that
                # is gone match nothing; the next DHCP enable replaces them.
                logger.warning("%s: keeping the guard on %s", context, bridge)
                return True
    return False


def _drop_bridge_guard_after_delete(bridge: str, context: str) -> None:
    """Remove a guard kept by ``_cleanup_bridge_dhcp_runtime`` once its bridge is gone.

    The bridge's address went with it, so the guard protects nothing; left
    behind, a new bridge reusing the name would inherit the rules.
    """
    try:
        host_net.bridge_guard_remove(bridge)
    except host_net.HostNetError as exc:
        logger.warning("%s: bridge_guard_remove(%s) failed (%s)", context, bridge, exc)


def _cleanup_lab_network_runtime(data: dict, lab_filename: str) -> None:
    networks = data.get("networks", {}) or {}
    if not isinstance(networks, dict):
        return

    lab_id = str(data.get("id") or lab_filename)
    for raw_network_id, network in networks.items():
        if not isinstance(network, dict):
            continue
        runtime = network.get("runtime") or {}
        bridge = runtime.get("bridge_name") if isinstance(runtime, dict) else None
        if not bridge:
            try:
                bridge = host_net.bridge_name(lab_id, int(raw_network_id))
            except Exception:  # noqa: BLE001 - best-effort cleanup path
                bridge = None
        if not bridge:
            continue

        # Bridge-Cloud: host-owned bridge.  Never tear it down on lab
        # cleanup.  Match on both ``network.type`` and ``runtime.driver``
        # so legacy records without a driver still skip teardown.
        if (
            str(network.get("type", "")) == BRIDGE_CLOUD_TYPE
            or (isinstance(runtime, dict) and runtime.get("driver") == BRIDGE_CLOUD_TYPE)
        ):
            logger.info(
                "delete_lab: host-owned bridge %s preserved (driver=bridge_cloud)",
                bridge,
            )
            continue
        # #319: a joined network uses another lab's bridge.
        if is_member(network):
            logger.info("delete_lab: joined network preserved owner bridge %s", bridge)
            continue
        # Defense-in-depth: refuse to delete anything that LOOKS like a
        # host bridge even if the record's type/driver was stripped.  The
        # lowest-layer ``host_net.bridge_del`` will refuse too, but this
        # assertion keeps the cleanup loop from logging spurious
        # "refusing bridge_del" warnings.
        assert not str(bridge).startswith("br-eth"), (
            f"delete_lab: refusing host-owned name {bridge!r}"
        )

        if str(network.get("type", "")) == NAT_CLOUD_TYPE:
            config = network.get("config") or {}
            cidr = str(config.get("cidr") or "")
            egress = str(
                runtime.get("egress_interface")
                or config.get("egress_interface")
                or ""
            )
            try:
                host_net.dnsmasq_stop(bridge)
            except host_net.HostNetError as exc:
                logger.warning(
                    "delete_lab: dnsmasq_stop(%s) failed during lab cleanup (%s)",
                    bridge,
                    exc,
                )
            try:
                host_net.forward_remove(bridge, cidr, egress)
            except host_net.HostNetError as exc:
                logger.warning(
                    "delete_lab: forward_remove(%s) failed during lab cleanup (%s)",
                    bridge,
                    exc,
                )
            try:
                host_net.nat_remove(bridge)
            except host_net.HostNetError as exc:
                logger.warning(
                    "delete_lab: nat_remove(%s) failed during lab cleanup (%s)",
                    bridge,
                    exc,
                )

        guard_kept = _cleanup_bridge_dhcp_runtime(bridge, network, "delete_lab")
        try:
            host_net.bridge_del(bridge)
        except host_net.HostNetEINVAL:
            logger.info("delete_lab: bridge %s already absent", bridge)
        except host_net.HostNetError as exc:
            logger.warning(
                "delete_lab: bridge_del(%s) failed during lab cleanup (%s)",
                bridge,
                exc,
            )
            continue
        if guard_kept:
            _drop_bridge_guard_after_delete(bridge, "delete_lab")


def _cleanup_lab_node_runtime(data: dict, lab_filename: str) -> None:
    """Stop and wipe every node in a lab being deleted (#338).

    Mirrors single-node delete: each node's work dir (disks, configs)
    under ``TMP_DIR/<lab_id>/<node_id>`` is removed, then any leftover
    per-lab work dir and runtime records are dropped. Best-effort per
    node so one failing node does not block deleting the lab itself.
    """
    nodes = data.get("nodes", {}) or {}
    if not isinstance(nodes, dict):
        nodes = {}
    lab_id = str(data.get("id") or lab_filename).strip()
    try:
        from app.services.node_runtime_service import NodeRuntimeService

        service = NodeRuntimeService()
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup path
        logger.warning("delete_lab: node runtime unavailable (%s)", exc)
        service = None
    if service is not None:
        for raw_node_id in list(nodes):
            try:
                node_id = int(raw_node_id)
            except (TypeError, ValueError):
                continue
            try:
                service.wipe_node(data, node_id)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup path
                logger.warning(
                    "delete_lab: wipe_node(%s) failed during lab cleanup (%s)",
                    node_id,
                    exc,
                )
    if not lab_id or "/" in lab_id or "\\" in lab_id or lab_id in {".", ".."}:
        return
    tmp_dir = get_settings().TMP_DIR
    work_root = tmp_dir / lab_id
    try:
        if work_root.exists():
            shutil.rmtree(work_root)
    except OSError as exc:
        logger.warning("delete_lab: rmtree(%s) failed (%s)", work_root, exc)
    runtime_dir = tmp_dir / "node-runtime"
    try:
        for record in runtime_dir.glob(f"{lab_id}-*.json"):
            record.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("delete_lab: runtime record cleanup failed (%s)", exc)


def _legacy_topology_to_links(data: dict) -> list[dict]:
    links: list[dict] = []
    for index, entry in enumerate(data.get("topology", []) or []):
        if not isinstance(entry, dict):
            continue

        source = str(entry.get("source", "") or "")
        destination = str(entry.get("destination", "") or "")
        source_iface = entry.get("source_interfaceId", 0) or 0
        destination_iface = entry.get("destination_interfaceId", 0)
        network_id = int(entry.get("network_id", 0) or 0)

        def _node_id(value: str) -> int | None:
            if value.startswith("node"):
                tail = value[len("node"):]
                if tail.isdigit():
                    return int(tail)
            return None

        source_node_id = _node_id(source)
        destination_node_id = _node_id(destination)
        link: dict[str, object] = {
            "id": entry.get("id") or f"lnk_{index + 1:03d}",
            "style_override": None,
            "label": str(entry.get("label", "") or ""),
            "color": str(entry.get("color", "") or ""),
            "width": str(entry.get("width", "1") or "1"),
            "metrics": {
                "delay_ms": int(entry.get("source_delay", 0) or 0),
                "loss_pct": int(entry.get("source_loss", 0) or 0),
                "bandwidth_kbps": int(entry.get("source_bandwidth", 0) or 0),
                "jitter_ms": int(entry.get("source_jitter", 0) or 0),
            },
        }

        if source_node_id is not None and destination_node_id is not None and not network_id:
            try:
                dst_index = int(destination_iface)
            except (TypeError, ValueError):
                dst_index = 0
            link["from"] = {"node_id": source_node_id, "interface_index": int(source_iface)}
            link["to"] = {"node_id": destination_node_id, "interface_index": dst_index}
            links.append(link)
            continue

        if source_node_id is not None and network_id:
            link["from"] = {"node_id": source_node_id, "interface_index": int(source_iface)}
            link["to"] = {"network_id": network_id}
            links.append(link)
            continue

        if destination_node_id is not None and network_id:
            try:
                dst_index = int(destination_iface)
            except (TypeError, ValueError):
                dst_index = 0
            link["from"] = {"node_id": destination_node_id, "interface_index": dst_index}
            link["to"] = {"network_id": network_id}
            links.append(link)
            continue

    return links


def _has_v2_shape(data: dict) -> bool:
    if not isinstance(data, dict):
        return False
    if "links" in data:
        return True
    networks = data.get("networks")
    return isinstance(networks, dict)


def _strip_legacy_fields(data: dict) -> dict:
    cleaned = dict(data)
    for key in LEGACY_COMPAT_KEYS:
        cleaned.pop(key, None)

    if "nodes" in cleaned and isinstance(cleaned["nodes"], dict):
        new_nodes = {}
        for node_id, node in cleaned["nodes"].items():
            if not isinstance(node, dict):
                new_nodes[node_id] = node
                continue
            new_node = dict(node)
            interfaces = new_node.get("interfaces")
            if isinstance(interfaces, list):
                new_interfaces = []
                for interface in interfaces:
                    if isinstance(interface, dict):
                        cleaned_interface = {
                            k: v for k, v in interface.items()
                            if k not in LEGACY_INTERFACE_COMPAT_KEYS
                        }
                        new_interfaces.append(cleaned_interface)
                    else:
                        new_interfaces.append(interface)
                new_node["interfaces"] = new_interfaces
            new_nodes[node_id] = new_node
        cleaned["nodes"] = new_nodes

    if "networks" in cleaned and isinstance(cleaned["networks"], dict):
        new_networks = {}
        for network_id, network in cleaned["networks"].items():
            if isinstance(network, dict):
                new_networks[network_id] = {
                    k: v for k, v in network.items()
                    if k not in LEGACY_NETWORK_COMPAT_KEYS
                }
            else:
                new_networks[network_id] = network
        cleaned["networks"] = new_networks

    return cleaned


def keep_written_revision(data: dict, revision: int | None) -> None:
    """Carry the revision a write landed into ``data``.

    ``write_lab_json_static`` bumps a copy, so a later
    ``bump_revision=False`` write of the same dict would put the old
    revision back. Call this between the two writes.
    """
    if revision is None:
        return
    meta = data.get("meta")
    if not isinstance(meta, dict):
        meta = data["meta"] = {}
    meta["revision"] = revision


async def unlink_lab_file(filename: str) -> None:
    """Remove a lab file under its lock (#328).

    A writer that read the lab before the delete finishes first; one that
    comes after re-reads under the lock, finds no file and writes nothing.
    """
    normalized = _normalize_relative_lab_path(filename)
    async with lab_lock_async(normalized, get_settings().LABS_DIR):
        _lab_file_path(normalized).unlink(missing_ok=True)


class LockedLab:
    """A lab file read under its ``lab_lock``; :meth:`save` writes it back."""

    def __init__(self, path: str, data: dict):
        self.path = path
        self.data = data
        self.revision: int | None = None

    def save(self, *, bump_revision: bool = True) -> int | None:
        """Write ``data`` and return the ``meta.revision`` that landed."""
        self.revision = LabService.write_lab_json_static(
            self.path, self.data, bump_revision=bump_revision
        )
        # A later save(bump_revision=False) must keep this revision.
        keep_written_revision(self.data, self.revision)
        return self.revision

    def restore(self, snapshot: dict) -> None:
        """Write back a copy taken under this lock. A rollback is not an edit."""
        self.data = snapshot
        self.save(bump_revision=False)


@contextmanager
def locked_lab(filename: str, *, reader: Callable[[str], dict] | None = None) -> Iterator[LockedLab]:
    """Take the lab's ``lab_lock`` and read the lab inside it (#328).

    Every read-modify-write of a lab file goes through this or
    :func:`locked_lab_async`, so a change is applied to the file as it is now
    and no other writer can land between the read and the write.
    """
    normalized = _normalize_relative_lab_path(filename)
    with lab_lock(normalized, get_settings().LABS_DIR):
        yield LockedLab(normalized, (reader or LabService.read_lab_json_static)(normalized))


@asynccontextmanager
async def locked_lab_async(
    filename: str, *, reader: Callable[[str], dict] | None = None
) -> AsyncIterator[LockedLab]:
    """:func:`locked_lab` for coroutines: waits for the lock off the event loop.

    Keep the body free of awaits that need this lab's lock.
    """
    normalized = _normalize_relative_lab_path(filename)
    async with lab_lock_async(normalized, get_settings().LABS_DIR):
        yield LockedLab(normalized, (reader or LabService.read_lab_json_static)(normalized))


class LabService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_lab_by_filename(self, filename: str) -> LabMeta | None:
        normalized_filename = _normalize_relative_lab_path(filename)
        result = await self.db.execute(
            select(LabMeta).where(LabMeta.filename == normalized_filename)
        )
        return result.scalar_one_or_none()

    async def list_labs(self) -> list[LabMeta]:
        result = await self.db.execute(select(LabMeta))
        return list(result.scalars().all())

    @staticmethod
    def file_exists(filename: str) -> bool:
        """True when the lab JSON for *filename* exists under LABS_DIR (#284).

        Traversal-guarded via :func:`_lab_file_path`; used by the labs
        listing and the ghost-row reconciliation so DB rows whose backing
        file was removed out-of-band can be detected.
        """
        try:
            return _lab_file_path(filename).exists()
        except ValueError:
            # Malformed/stored-attacker filename: treat as missing.
            return False

    async def reconcile_missing_lab_files(self) -> list[str]:
        """Drop DB lab rows whose backing JSON file is gone (#284).

        The labs index merges the filesystem scan with the ``labs`` table;
        a row whose file was removed out-of-band (manual cleanup, crashed
        delete) keeps appearing as a ghost entry forever. This sweep
        converges the two sides — the same philosophy as the startup
        orphan-QEMU sweep (#225). Returns the removed filenames.
        """
        removed: list[str] = []
        for lab in await self.list_labs():
            if LabService.file_exists(lab.filename):
                continue
            logger.warning(
                "reconcile: lab row has no backing file — removing DB row (filename=%s)",
                lab.filename,
            )
            await self.db.delete(lab)
            removed.append(lab.filename)
        if removed:
            await self.db.commit()
        return removed

    async def create_lab(
        self,
        owner: str,
        name: str,
        path: str | None = None,
        filename: str | None = None,
        **meta_fields,
    ) -> LabMeta:
        """Create a new lab: DB record + JSON file."""
        lab_id = uuid.uuid4()
        relative_path = build_relative_lab_path(name=name, path=path, filename=filename)
        filepath = _lab_file_path(relative_path)
        if filepath.exists():
            raise FileExistsError(f"Lab file already exists: {relative_path}")

        meta = {
            "name": name,
            "author": meta_fields.get("author", ""),
            "description": meta_fields.get("description", ""),
            "body": meta_fields.get("body", ""),
            "version": meta_fields.get("version", "0"),
            "scripttimeout": meta_fields.get("scripttimeout", 300),
            "countdown": meta_fields.get("countdown", 0),
            "linkwidth": meta_fields.get("linkwidth", "1"),
            "grid": meta_fields.get("grid", True),
            "lock": meta_fields.get("lock", False),
            "sat": meta_fields.get("sat", "-1"),
        }
        lab_json = _empty_lab_json(str(lab_id), meta)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(lab_json, f, indent=2)

        db_meta = {k: v for k, v in meta.items() if hasattr(LabMeta, k) and k != "name"}
        lab = LabMeta(
            id=lab_id,
            owner=owner,
            filename=relative_path,
            name=name,
            path=_api_lab_path(relative_path),
            **db_meta,
        )
        self.db.add(lab)
        await self.db.commit()
        await self.db.refresh(lab)
        return lab

    async def update_lab(
        self,
        lab: LabMeta,
        **updates,
    ) -> LabMeta:
        """Update lab metadata in both DB and JSON file."""
        filepath = _lab_file_path(lab.filename)
        db_fields = {
            "name",
            "author",
            "description",
            "body",
            "version",
            "scripttimeout",
            "countdown",
            "linkwidth",
            "grid",
            "lock",
        }
        for field, value in updates.items():
            if field in db_fields and hasattr(lab, field) and value is not None:
                setattr(lab, field, value)

        await self.db.commit()
        await self.db.refresh(lab)

        if filepath.exists():
            async with locked_lab_async(lab.filename) as locked:
                for field in db_fields:
                    if field in updates and updates[field] is not None:
                        locked.data["meta"][field] = updates[field]
                locked.save()

        return lab

    async def delete_lab(self, lab: LabMeta) -> None:
        filepath = _lab_file_path(lab.filename)
        assert_no_external_members_for_scope(lab.filename)
        if filepath.exists():
            try:
                data = LabService.read_lab_json_static(lab.filename)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning(
                    "delete_lab: could not read %s for network cleanup (%s)",
                    lab.filename,
                    exc,
                )
            else:
                _cleanup_lab_network_runtime(data, lab.filename)
                _cleanup_lab_node_runtime(data, lab.filename)
            await unlink_lab_file(lab.filename)
        await self.db.delete(lab)
        await self.db.commit()

    @staticmethod
    def read_lab_json_static(filename: str) -> dict:
        filepath = _lab_file_path(filename)
        with open(filepath, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"{LEGACY_SCHEMA_ERROR} (lab_path={filepath})")

        data["topology"] = _derive_legacy_topology(data)
        _derive_legacy_interface_network_ids(data)
        _derive_network_counts(data)
        return data

    @staticmethod
    def read_meta_revision_static(filename: str) -> int | None:
        """Post-write `meta.revision` for endpoints that must report it (#248).

        Reads the value from disk rather than predicting it, so multi-write
        operations (e.g. create_link's persist + hot-attach stamp pass) report
        the revision that actually landed.
        """
        try:
            data = LabService.read_lab_json_static(filename)
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        revision = (data.get("meta") or {}).get("revision")
        return revision if isinstance(revision, int) else None

    @staticmethod
    def write_lab_json_static(filename: str, data: dict, *, bump_revision: bool = True) -> int | None:
        """Write a lab file; returns the ``meta.revision`` written.

        Callers that read-modify-write must hold the lab's ``lab_lock`` from
        the read to here: use :func:`locked_lab` / :func:`locked_lab_async`.
        """
        filepath = _lab_file_path(filename)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        # Issue #174 follow-up: only synthesize links[] from topology[]
        # for v1-shaped writes (no v2 links[] yet). Previously this ran
        # unconditionally, so a PUT /topology with topology=[] would
        # regenerate links[]=[] and silently wipe every link from the
        # lab — even though the caller never asked to touch links[].
        # For v2 labs, links[] is authoritative; topology[] is a derived
        # legacy view (`read_lab_json_static` re-derives it on every read).
        if "topology" in data and not data.get("links"):
            data["links"] = _legacy_topology_to_links(data)

        cleaned = _strip_legacy_fields(data)
        cleaned.setdefault("schema", SCHEMA_VERSION)
        cleaned.setdefault("links", [])
        cleaned.setdefault("viewport", {"x": 0, "y": 0, "zoom": 1.0})
        cleaned.setdefault("defaults", {"link_style": "orthogonal"})

        if _has_v2_shape(cleaned) and cleaned.get("schema_version") != SCHEMA_VERSION:
            cleaned["schema_version"] = SCHEMA_VERSION

        # #245: machine-owned optimistic-concurrency revision, bumped on
        # every user-facing lab.json write (this is the single write choke
        # point). Clients guard PUT /layout and PUT /topology with If-Match;
        # a stale client gets 412 instead of silently reverting newer state.
        # Kept under meta so GET /labs/{id}'s meta passthrough exposes it, and
        # separate from the user-authored meta.version display field.
        # #248: server-internal bookkeeping writes (heartbeat status sync,
        # planned_mac, runtime/bridge/link reconciliation, rollback restores,
        # runtime stamps) pass bump_revision=False — they are not edits, and
        # bumping here silently invalidated the editor's If-Match token,
        # producing spurious 412s ("Lab changed in another session") in
        # single-session use.
        if bump_revision:
            revision_meta = dict(cleaned.get("meta") or {})
            try:
                revision_meta["revision"] = int(revision_meta.get("revision") or 0) + 1
            except (TypeError, ValueError):
                revision_meta["revision"] = 1
            cleaned["meta"] = revision_meta

        _replace_json(filepath, cleaned)
        meta = cleaned.get("meta")
        revision = meta.get("revision") if isinstance(meta, dict) else None
        return revision if isinstance(revision, int) else None
