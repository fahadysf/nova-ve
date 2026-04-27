import json
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.lab import LabMeta


SCHEMA_VERSION = 2

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
    return Path(*parts).as_posix()


def _lab_file_path(relative_path: str) -> Path:
    labs_dir = _labs_dir()
    normalized_relative_path = _normalize_relative_lab_path(relative_path)
    filepath = (labs_dir / normalized_relative_path).resolve()
    if not str(filepath).startswith(str(labs_dir)):
        raise ValueError("Invalid lab path: directory traversal detected")
    return filepath


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
    """Synthesise legacy ``topology[]`` entries from v2 ``links[]``.

    Wave 1 will retire this shim; for now it lets the existing routers and
    tests keep operating on the legacy keys without modification.
    """

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


def _legacy_topology_to_links(data: dict) -> list[dict]:
    """Translate legacy ``topology[]`` entries back into v2 ``links[]``.

    This is the inverse of :func:`_derive_legacy_topology`. Used by
    ``write_lab_json_static`` so router code that mutates ``topology`` is
    transparently persisted to v2 link records. ``id`` is generated from the
    link index when the source legacy record doesn't carry one.
    """

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


def _strip_legacy_fields(data: dict) -> dict:
    """Return a deep-ish copy of ``data`` with synthesised legacy keys removed.

    Used before persisting so we never write the v1 compat shim to disk.
    """

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
            data = LabService.read_lab_json_static(lab.filename)
            for field in db_fields:
                if field in updates and updates[field] is not None:
                    data["meta"][field] = updates[field]
            LabService.write_lab_json_static(lab.filename, data)

        return lab

    async def delete_lab(self, lab: LabMeta) -> None:
        filepath = _lab_file_path(lab.filename)
        if filepath.exists():
            filepath.unlink()
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
    def write_lab_json_static(filename: str, data: dict) -> None:
        filepath = _lab_file_path(filename)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        if "topology" in data:
            data["links"] = _legacy_topology_to_links(data)

        cleaned = _strip_legacy_fields(data)
        cleaned.setdefault("schema", SCHEMA_VERSION)
        cleaned.setdefault("links", [])
        cleaned.setdefault("viewport", {"x": 0, "y": 0, "zoom": 1.0})
        cleaned.setdefault("defaults", {"link_style": "orthogonal"})
        with open(filepath, "w") as f:
            json.dump(cleaned, f, indent=2)
