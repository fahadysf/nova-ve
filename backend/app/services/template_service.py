from dataclasses import dataclass, field
from pathlib import Path
import logging
import os
import shutil
import subprocess
from typing import Any, Iterator

import yaml

from app.config import get_settings


_logger = logging.getLogger("nova-ve.template_service")


SUPPORTED_TEMPLATE_TYPES = {"qemu", "docker", "iol", "dynamips"}

QEMU_MACHINE_OPTIONS = {"q35", "pc"}
_QEMU_MAX_NICS_HARD_CAP = 8
_DOCKER_MAX_NICS_DEFAULT = 99

# #206 — synthetic per-child template key separator. A paired template's child
# is exposed in the catalog as ``<paired_key>__<child_id>`` so node.template
# lookups (edit, capability, image validation) resolve to a real entry.
SYNTHETIC_PAIRED_CHILD_SEP = "__"


def synthetic_paired_child_key(paired_key: str, child_id: str) -> str:
    return f"{paired_key}{SYNTHETIC_PAIRED_CHILD_SEP}{child_id}"


def _kind_default_iface_names(kind: str, ethernet: int) -> list[str]:
    """Hardcoded fallback interface names by node kind. Mirrors the qemu
    branch and ``eth{n}`` else-branch in ``_default_interfaces``."""
    if kind == "qemu":
        return [f"Gi{i + 1}" for i in range(ethernet)]
    return [f"eth{i}" for i in range(ethernet)]


def _paired_child_iface_names(child: dict[str, Any]) -> list[str]:
    """Predict the interface names a paired child will have at creation
    time. Used by :func:`validate_paired_template` to pre-flight link
    resolution at catalog-load time so old (#202-pre) imports surface as
    invalid in the catalog rather than failing only at instantiation.

    Must mirror the runtime overlay sequence in
    ``backend/app/routers/labs.py::_build_paired_child_payload``:

    1. Build the base interface list:
       a. ``interface_naming.format`` (string or list[str]) → per-index
          via :func:`render_interface_name`, OR
       b. Hardcoded fallback by kind (qemu→``Gi{n+1}``, others→``eth{n}``).
    2. Overlay ``interface_naming.explicit:[name0, name1, ...]`` onto
       positions ``0..len(explicit)-1`` of the base.

    Codex-iter2 fix: previously this returned the explicit list verbatim,
    so a child with ``ethernet=2, explicit=["mgmt0"]`` was predicted as
    ``["mgmt0"]`` and any link to the second interface (``Gi2``/``eth1``)
    was wrongly flagged invalid. The runtime overlays explicit onto a
    full-length base, so the predictor must too.
    """
    ethernet = int(child.get("ethernet", 1))
    kind = str(child.get("kind") or "qemu").strip().lower()
    iface_naming = child.get("interface_naming") if isinstance(child.get("interface_naming"), dict) else None

    # Step 1 — base list (format if present, else kind-default).
    base: list[str]
    if iface_naming is not None:
        fmt = iface_naming.get("format")
        if isinstance(fmt, str) and fmt.strip():
            base = [render_interface_name(fmt, i) for i in range(ethernet)]
        elif isinstance(fmt, list) and fmt:
            # Pre-validation list shape (#179) — _validate_interface_naming
            # would normalize this to a comma-string before reaching the
            # runtime; the predictor sees raw template JSON so normalize
            # here too.
            normalized = ",".join(str(item).strip() for item in fmt if str(item).strip())
            base = (
                [render_interface_name(normalized, i) for i in range(ethernet)]
                if normalized
                else _kind_default_iface_names(kind, ethernet)
            )
        else:
            base = _kind_default_iface_names(kind, ethernet)
    else:
        base = _kind_default_iface_names(kind, ethernet)

    # Step 2 — overlay explicit names onto the front of the base list.
    if iface_naming is not None:
        explicit = iface_naming.get("explicit")
        if isinstance(explicit, list):
            for idx, name in enumerate(explicit):
                if idx >= len(base):
                    break
                if isinstance(name, str):
                    base[idx] = name

    return base


def validate_paired_template(data: dict[str, Any]) -> str | None:
    """Pre-flight a paired template (#207). Returns ``None`` when the template
    can be instantiated end-to-end, or a human-readable reason string when a
    link references an interface name that no child will expose. Used by the
    catalog builder to flag old imports + by the from-paired-template endpoint
    to return 422 instead of 500.
    """
    nodes = data.get("nodes") or []
    links = data.get("links") or []
    children_by_id: dict[str, dict[str, Any]] = {}
    for child in nodes:
        if isinstance(child, dict):
            children_by_id[str(child.get("id") or "")] = child

    for link in links:
        if not isinstance(link, dict):
            continue
        for endpoint_key, iface_key in (("from_node", "from_iface"), ("to_node", "to_iface")):
            child_id = str(link.get(endpoint_key) or "")
            iface_name = str(link.get(iface_key) or "")
            child = children_by_id.get(child_id)
            if child is None:
                return f"link references unknown child id {child_id!r}"
            available = _paired_child_iface_names(child)
            if iface_name not in available:
                return (
                    f"child {child_id!r} interface {iface_name!r} not in available "
                    f"set [{', '.join(available) or '<empty>'}]. Add "
                    f"interface_naming.explicit:[...] to the child block so "
                    f"the link resolves at instantiation time."
                )
    return None


def _default_capabilities(template_type: str) -> dict[str, Any]:
    """Return capability defaults inferred from node type (backward-compat for templates without capabilities block)."""
    if template_type == "docker":
        return {"hotplug": True, "max_nics": _DOCKER_MAX_NICS_DEFAULT, "machine": None}
    # qemu, iol, dynamips default to the runtime-capable q35 profile
    return {"hotplug": True, "max_nics": _QEMU_MAX_NICS_HARD_CAP, "machine": "q35"}


def _validate_capabilities(payload: Any, template_type: str, source: str) -> dict[str, Any]:
    """Validate and normalise the ``capabilities`` block from a template YAML.

    Returns a fully-populated capabilities dict with all three fields resolved.
    Raises :class:`TemplateError` on invalid input.
    """
    defaults = _default_capabilities(template_type)

    if payload is None:
        # No capabilities block: infer from node type (backward compat)
        return defaults

    if not isinstance(payload, dict):
        raise TemplateError(
            f"capabilities on {source} must be an object with optional fields "
            f"hotplug (bool), max_nics (int), machine (str)."
        )

    result: dict[str, Any] = dict(defaults)

    # hotplug
    if "hotplug" in payload:
        val = payload["hotplug"]
        if not isinstance(val, bool):
            raise TemplateError(
                f"capabilities.hotplug on {source} must be a boolean."
            )
        result["hotplug"] = val

    # max_nics
    if "max_nics" in payload:
        val = payload["max_nics"]
        if not isinstance(val, int) or isinstance(val, bool):
            raise TemplateError(
                f"capabilities.max_nics on {source} must be an integer."
            )
        if template_type != "docker" and val > _QEMU_MAX_NICS_HARD_CAP:
            raise TemplateError(
                f"capabilities.max_nics on {source} exceeds the hard cap of "
                f"{_QEMU_MAX_NICS_HARD_CAP} for {template_type} templates (Principle 5)."
            )
        if val < 1:
            raise TemplateError(
                f"capabilities.max_nics on {source} must be at least 1."
            )
        result["max_nics"] = val

    # machine
    if "machine" in payload:
        val = payload["machine"]
        if template_type != "qemu":
            raise TemplateError(
                f"capabilities.machine on {source} is only valid for qemu templates."
            )
        if not isinstance(val, str) or val not in QEMU_MACHINE_OPTIONS:
            raise TemplateError(
                f"capabilities.machine on {source} must be one of "
                f"{sorted(QEMU_MACHINE_OPTIONS)}, got {val!r}."
            )
        result["machine"] = val

    # Consistency check: pc + hotplug=True is not allowed for QEMU
    if template_type == "qemu" and result.get("hotplug") and result.get("machine") == "pc":
        raise TemplateError(
            f"capabilities on {source}: hotplug=true requires machine='q35'; "
            f"'pc' does not support PCIe hot-plug."
        )

    return result


ICON_TYPE_TO_FILENAME = {
    "router": "Router.png",
    "server": "Server.png",
    "switch": "Switch.png",
    "firewall": "Firewall.png",
    "host": "Server.png",
}


def _icon_filename_for(icon_type: str) -> str:
    return ICON_TYPE_TO_FILENAME.get(icon_type.strip().lower(), "Router.png")


QEMU_NIC_OPTIONS = [
    {"value": "virtio-net-pci", "label": "virtio-net-pci"},
    {"value": "e1000", "label": "e1000"},
    {"value": "e1000e", "label": "e1000e"},
    {"value": "rtl8139", "label": "rtl8139"},
    {"value": "vmxnet3", "label": "vmxnet3"},
    {"value": "pcnet", "label": "pcnet"},
]

QEMU_ARCH_OPTIONS = [
    {"value": "x86_64", "label": "x86_64"},
    {"value": "aarch64", "label": "aarch64"},
    {"value": "i386", "label": "i386"},
]

DOCKER_RESTART_OPTIONS = [
    {"value": "no", "label": "no"},
    {"value": "on-failure", "label": "on-failure"},
    {"value": "unless-stopped", "label": "unless-stopped"},
    {"value": "always", "label": "always"},
]

IOL_CONFIG_OPTIONS = [
    {"value": "Unconfigured", "label": "Unconfigured"},
    {"value": "Saved", "label": "Saved"},
    {"value": "Exported", "label": "Exported"},
]

DYNAMIPS_NPE_OPTIONS = [
    {"value": "npe-100", "label": "npe-100"},
    {"value": "npe-150", "label": "npe-150"},
    {"value": "npe-175", "label": "npe-175"},
    {"value": "npe-200", "label": "npe-200"},
    {"value": "npe-225", "label": "npe-225"},
    {"value": "npe-300", "label": "npe-300"},
    {"value": "npe-400", "label": "npe-400"},
    {"value": "npe-g1", "label": "npe-g1"},
    {"value": "npe-g2", "label": "npe-g2"},
]

DYNAMIPS_MIDPLANE_OPTIONS = [
    {"value": "std", "label": "std"},
    {"value": "vxr", "label": "vxr"},
]

DYNAMIPS_C7200_SLOT_OPTIONS = [
    {"value": "", "label": "(empty)"},
    {"value": "C7200-IO-FE", "label": "C7200-IO-FE"},
    {"value": "C7200-IO-2FE", "label": "C7200-IO-2FE"},
    {"value": "C7200-IO-GE-E", "label": "C7200-IO-GE-E"},
    {"value": "PA-FE-TX", "label": "PA-FE-TX"},
    {"value": "PA-2FE-TX", "label": "PA-2FE-TX"},
    {"value": "PA-GE", "label": "PA-GE"},
    {"value": "PA-4E", "label": "PA-4E"},
    {"value": "PA-8E", "label": "PA-8E"},
    {"value": "PA-4T+", "label": "PA-4T+"},
    {"value": "PA-8T", "label": "PA-8T"},
    {"value": "PA-A1", "label": "PA-A1"},
    {"value": "PA-POS-OC3", "label": "PA-POS-OC3"},
]


def _qemu_extras_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "architecture",
            "label": "Architecture",
            "type": "select",
            "options": QEMU_ARCH_OPTIONS,
            "default": "x86_64",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "qemu_nic",
            "label": "NIC model",
            "type": "select",
            "options": QEMU_NIC_OPTIONS,
            "default": "e1000",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "qemu_version",
            "label": "QEMU version",
            "type": "text",
            "default": "",
            "placeholder": "(auto)",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "boot_order",
            "label": "Boot order",
            "type": "select",
            "options": [
                {"value": "", "label": "Default (disk first when ISO attached)"},
                {"value": "cd", "label": "Disk, then CD-ROM"},
                {"value": "dc", "label": "CD-ROM, then disk"},
            ],
            "default": "",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "qemu_options",
            "label": "Extra qemu args",
            "type": "textarea",
            "default": "",
            "placeholder": "-nographic -enable-kvm",
            "description": "Flag-level override: flags present here replace all default instances of the same flag.",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "uuid",
            "label": "UUID",
            "type": "text",
            "default": "",
            "placeholder": "(auto-generated)",
            "stoppedOnly": True,
        },
        {
            "key": "firstmac",
            "label": "First MAC",
            "type": "text",
            "default": "",
            "placeholder": "(auto-generated)",
            "stoppedOnly": True,
        },
        {
            "key": "cpulimit",
            "label": "CPU limit",
            "type": "number",
            "default": 1,
            "stoppedOnly": True,
        },
    ]


def _docker_extras_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "cpulimit",
            "label": "CPU limit",
            "type": "number",
            "default": 1,
            "stoppedOnly": True,
        },
        {
            "key": "restart_policy",
            "label": "Restart policy",
            "type": "select",
            "options": DOCKER_RESTART_OPTIONS,
            "default": "no",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "environment",
            "label": "Environment variables",
            "type": "env",
            "default": [],
            "description": "Passed as -e KEY=value to docker run.",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "extra_args",
            "label": "Extra docker args",
            "type": "textarea",
            "default": "",
            "placeholder": "--privileged --cap-add=NET_ADMIN",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "command",
            "label": "Command (overrides image CMD)",
            "type": "textarea",
            "default": "",
            "placeholder": "tail -f /dev/null",
            "description": (
                "Override the container's default CMD. Appended after the image "
                "in the docker run argv. Shlex-split when given as a string; "
                "passed verbatim when given as a list of strings."
            ),
            "stoppedOnly": True,
            "runtime": True,
        },
    ]


def _iol_extras_schema() -> list[dict[str, Any]]:
    return [
        {
            "key": "serial",
            "label": "Serial groups",
            "type": "number",
            "default": 0,
            "description": "Each group adds 4 serial interfaces.",
            "stoppedOnly": True,
        },
        {
            "key": "nvram",
            "label": "NVRAM (KB)",
            "type": "number",
            "default": 1024,
            "stoppedOnly": True,
        },
        {
            "key": "config",
            "label": "Config",
            "type": "select",
            "options": IOL_CONFIG_OPTIONS,
            "default": "Unconfigured",
            "stoppedOnly": True,
        },
        {
            "key": "idle_pc",
            "label": "Idle PC",
            "type": "text",
            "default": "",
            "placeholder": "0x...",
            "stoppedOnly": True,
        },
    ]


def _dynamips_c7200_extras_schema() -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = []
    for index in range(7):
        schema.append(
            {
                "key": f"slot{index}",
                "label": f"Slot {index}",
                "type": "select",
                "options": DYNAMIPS_C7200_SLOT_OPTIONS,
                "default": "C7200-IO-FE" if index == 0 else "",
                "stoppedOnly": True,
                "runtime": True,
            }
        )
    schema += [
        {
            "key": "nvram",
            "label": "NVRAM (KB)",
            "type": "number",
            "default": 128,
            "stoppedOnly": True,
        },
        {
            "key": "npe",
            "label": "NPE",
            "type": "select",
            "options": DYNAMIPS_NPE_OPTIONS,
            "default": "npe-400",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "midplane",
            "label": "Midplane",
            "type": "select",
            "options": DYNAMIPS_MIDPLANE_OPTIONS,
            "default": "vxr",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "idlepc",
            "label": "Idle PC",
            "type": "text",
            "default": "",
            "placeholder": "0x...",
            "stoppedOnly": True,
            "runtime": True,
        },
    ]
    return schema


def _extras_schema_for(template_type: str) -> list[dict[str, Any]]:
    if template_type == "qemu":
        return _qemu_extras_schema()
    if template_type == "docker":
        return _docker_extras_schema()
    if template_type == "iol":
        return _iol_extras_schema()
    if template_type == "dynamips":
        return _dynamips_c7200_extras_schema()
    return []


class TemplateError(Exception):
    pass


_INTERFACE_NAMING_FORMAT_PLACEHOLDERS = ("{n}", "{slot}", "{port}")


def render_interface_name(fmt: str, index: int) -> str:
    """Render an interface name from a format string and a 0-based interface index.

    Supports comma-separated lists where only the last entry may carry a
    placeholder; earlier entries are fixed names that map to one interface
    each, in order.

    Placeholders (in the trailing entry, with relative numbering):
    - ``{n}``    — 0-based index (e.g. ``eth{n}`` → ``eth0``)
    - ``{slot}`` — alias for ``{n}``
    - ``{port}`` — 1-based index (e.g. ``Gi{port}`` → ``Gi1``)
    """
    items = [s.strip() for s in str(fmt).split(",") if s.strip()]
    if not items:
        return ""
    fixed_count = len(items) - 1
    if index < fixed_count:
        return items[index]
    last = items[-1]
    rel = index - fixed_count
    return last.replace("{n}", str(rel)).replace("{slot}", str(rel)).replace("{port}", str(rel + 1))


def _validate_interface_naming(payload: dict[str, Any], source: str) -> dict[str, Any]:
    """Validate the optional ``interface_naming`` block on a template YAML.

    Either ``format`` OR ``explicit: [<str>, ...]`` may be supplied — exactly
    one. Any other combination raises :class:`TemplateError`.

    ``format`` may be either:

    * a single string carrying a placeholder (``{n}``, ``{slot}``, or
      ``{port}``), e.g. ``"eth{n}"`` — the historical shape; or
    * a non-empty ``list[str]`` of fixed names with a trailing placeholder
      entry (#179), e.g. ``["fxp0", "ge-0/0/{n}"]``. Earlier entries are
      fixed; only the last entry may carry a placeholder. The list is
      normalized to a comma-separated string for downstream consumers
      (``render_interface_name`` already understands that form).
    """

    if not isinstance(payload, dict):
        raise TemplateError(
            f"interface_naming on {source} must be an object with 'format' or 'explicit'."
        )

    has_format = "format" in payload
    has_explicit = "explicit" in payload

    if has_format and has_explicit:
        raise TemplateError(
            f"interface_naming on {source} must specify exactly one of 'format' or 'explicit', not both."
        )
    if not has_format and not has_explicit:
        raise TemplateError(
            f"interface_naming on {source} must specify either 'format' or 'explicit'."
        )

    if has_format:
        fmt = payload["format"]
        if isinstance(fmt, list):
            if not fmt:
                raise TemplateError(
                    f"interface_naming.format on {source} must be a non-empty list when given as a list."
                )
            for i, item in enumerate(fmt):
                if not isinstance(item, str) or not item.strip():
                    raise TemplateError(
                        f"interface_naming.format on {source} list entries must be non-empty strings."
                    )
                has_placeholder = any(t in item for t in _INTERFACE_NAMING_FORMAT_PLACEHOLDERS)
                if i < len(fmt) - 1 and has_placeholder:
                    raise TemplateError(
                        f"interface_naming.format on {source}: only the last list entry "
                        f"may contain {', '.join(_INTERFACE_NAMING_FORMAT_PLACEHOLDERS)} (got placeholder in entry {i!r})."
                    )
            last_has_placeholder = any(t in fmt[-1] for t in _INTERFACE_NAMING_FORMAT_PLACEHOLDERS)
            if not last_has_placeholder:
                raise TemplateError(
                    f"interface_naming.format on {source}: last list entry must contain "
                    f"{', '.join(_INTERFACE_NAMING_FORMAT_PLACEHOLDERS)} "
                    f"(use 'explicit: [...]' for a fixed-only list)."
                )
            return {"format": ",".join(item.strip() for item in fmt)}
        if not isinstance(fmt, str) or not fmt.strip():
            raise TemplateError(
                f"interface_naming.format on {source} must be a non-empty string or list of strings."
            )
        if not any(token in fmt for token in _INTERFACE_NAMING_FORMAT_PLACEHOLDERS):
            raise TemplateError(
                f"interface_naming.format on {source} must contain at least one of "
                f"{', '.join(_INTERFACE_NAMING_FORMAT_PLACEHOLDERS)}."
            )
        return {"format": fmt}

    explicit = payload["explicit"]
    if not isinstance(explicit, list) or not explicit:
        raise TemplateError(
            f"interface_naming.explicit on {source} must be a non-empty list of strings."
        )
    if not all(isinstance(item, str) and item.strip() for item in explicit):
        raise TemplateError(
            f"interface_naming.explicit on {source} must contain only non-empty strings."
        )
    return {"explicit": list(explicit)}


@dataclass
class TemplateDefinition:
    key: str
    type: str
    name: str
    description: str
    icon_type: str
    cpu: int
    ram: int
    ethernet: int
    console_type: str
    cpulimit: int
    extras: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    interface_naming: dict[str, Any] | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    # #206 — when this entry was synthesized from a paired-template child block
    # (vs loaded from a per-type YAML), ``paired_parent`` is the originating
    # paired template key. Surfaced in the catalog response so the frontend can
    # exclude these from the standalone "Add node" type-tab picker while still
    # resolving them by key for edit-mode lookups and capability gates.
    paired_parent: str | None = None

    def as_response(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "template": self.key,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "icon_type": self.icon_type,
            "icon": _icon_filename_for(self.icon_type),
            "cpu": self.cpu,
            "ram": self.ram,
            "ethernet": self.ethernet,
            "console_type": self.console_type,
            "cpulimit": self.cpulimit,
            "extras": dict(self.extras),
            "capabilities": dict(self.capabilities),
        }


class TemplateService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.templates_dir = self.settings.TEMPLATES_DIR
        # USER_TEMPLATES_DIR (#185): operator-imported templates. Walked alongside
        # the builtin TEMPLATES_DIR; user-dir entries shadow builtin on key
        # collision and a WARNING is logged. Tolerate older settings shapes that
        # have not yet been refreshed by reading the attribute defensively.
        self.user_templates_dir = getattr(
            self.settings, "USER_TEMPLATES_DIR", None
        )
        self.images_dir = self.settings.IMAGES_DIR

    def list_templates(self, template_type: str) -> dict[str, dict[str, Any]]:
        template_type = self._normalize_type(template_type)
        templates: dict[str, dict[str, Any]] = {}
        for template in self._load_templates():
            if template.type == template_type:
                templates[template.key] = template.as_response()
        return dict(sorted(templates.items()))

    def get_template(self, template_type: str, key: str) -> TemplateDefinition:
        template_type = self._normalize_type(template_type)
        for template in self._load_templates():
            if template.type == template_type and template.key == key:
                return template
        raise TemplateError(f"Template {key} does not exist for type {template_type}.")

    def _iter_paired_user_templates(self) -> Iterator[tuple[Path, dict[str, Any]]]:
        """Yield (path, payload) for every well-formed ``kind="paired"`` JSON
        under ``USER_TEMPLATES_DIR``. Tolerates I/O + parse errors, skips
        non-paired files, and enforces the minimal structural invariant
        (non-empty ``nodes`` list + ``links`` list). Shared by the loader,
        the catalog builder, and the per-key lookup so they cannot drift.
        """
        if self.user_templates_dir is None or not self.user_templates_dir.exists():
            return
        for path in sorted(self.user_templates_dir.rglob("*.json")):
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except (OSError, yaml.YAMLError):
                continue
            if not isinstance(data, dict) or data.get("kind") != "paired":
                continue
            nodes = data.get("nodes")
            links = data.get("links")
            if not (isinstance(nodes, list) and nodes and isinstance(links, list)):
                continue
            yield path, data

    def get_paired_user_template(self, template_key: str) -> dict[str, Any] | None:
        """Load a paired-node template (``kind="paired"``) from USER_TEMPLATES_DIR.

        Returns the parsed dict (with ``nodes:[...]`` + ``links:[...]`` siblings
        validated as lists) or ``None`` when no matching paired template exists.
        Used by ``POST /api/labs/.../nodes/from-paired-template`` (#202) to
        instantiate multi-node templates atomically. Read-only; tolerant of
        malformed JSON.
        """
        for path, data in self._iter_paired_user_templates():
            if path.stem == template_key:
                return data
        return None

    def is_paired_user_template(self, template_key: str) -> bool:
        """Boolean check used by the single-node from-template endpoint to 400 paired keys."""
        return self.get_paired_user_template(template_key) is not None

    def list_images(self, template_type: str, template_key: str) -> dict[str, dict[str, Any]]:
        template = self.get_template(template_type, template_key)

        # #206 — synthetic paired children declare their image explicitly in
        # the paired template JSON. Surface only that image so validation in
        # validate_node_request matches what the paired-create endpoint uses.
        if template.paired_parent is not None:
            declared = str(template.raw.get("image") or "").strip()
            if declared:
                return {declared: {"image": declared, "source": "paired-child-declared"}}
            return {}

        images: dict[str, dict[str, Any]] = {}

        if template_type == "docker":
            images.update(self._docker_image_catalog())

        image_root = self.images_dir / template_type
        if image_root.exists():
            apply_name_filter = template_type != "docker"
            template_match = template.name.strip().lower()
            for child in sorted(image_root.iterdir()):
                image_info = self._image_info(child)
                if not image_info:
                    continue
                if apply_name_filter and template_match:
                    folder_match = str(image_info["image"]).strip().lower()
                    if (
                        folder_match
                        and template_match not in folder_match
                        and folder_match not in template_match
                    ):
                        continue
                images[image_info["image"]] = image_info

        return images

    def validate_node_request(self, template_type: str, template_key: str, image_name: str) -> TemplateDefinition:
        template = self.get_template(template_type, template_key)
        images = self.list_images(template_type, template_key)
        if image_name not in images:
            raise TemplateError(
                f"Image {image_name} is not available for template {template_key} ({template_type})."
            )
        return template

    def list_icon_options(self) -> list[str]:
        icons = {
            "Router.png",
            "Server.png",
        }
        for template in self._load_templates():
            icon = _icon_filename_for(template.icon_type)
            if icon:
                icons.add(icon)
        return sorted(icons)

    def build_node_catalog(self) -> dict[str, Any]:
        templates: list[dict[str, Any]] = []
        icon_options = self.list_icon_options()
        for template in self._load_templates():
            images = list(self.list_images(template.type, template.key).values())
            default_image = images[0]["image"] if images else ""
            extras_schema = _extras_schema_for(template.type)
            defaults_extras = self._compose_extras(extras_schema, template.extras)
            templates.append(
                {
                    "key": template.key,
                    "type": template.type,
                    "name": template.name,
                    "description": template.description,
                    "defaults": {
                        "type": template.type,
                        "template": template.key,
                        "image": default_image,
                        "icon_type": template.icon_type,
                        "icon": _icon_filename_for(template.icon_type),
                        "cpu": template.cpu,
                        "ram": template.ram,
                        "ethernet": template.ethernet,
                        "console_type": template.console_type,
                        "delay": 0,
                        "cpulimit": template.cpulimit,
                        "extras": defaults_extras,
                    },
                    "images": images,
                    "icon_options": icon_options,
                    "extras_schema": extras_schema,
                    "capabilities": dict(template.capabilities),
                    # #206 — set on synthetic per-child entries; frontends should
                    # filter these out of the standalone create-flow type tabs
                    # but still resolve them by key for edit + capability lookups.
                    "paired_parent": template.paired_parent,
                }
            )
        return {
            "templates": templates,
            "paired_templates": self._build_paired_catalog(),
            "icon_options": icon_options,
        }

    def _build_paired_catalog(self) -> list[dict[str, Any]]:
        """Enumerate paired user-templates (#202) for the node catalog response.

        Paired templates live as JSON files at the root of ``USER_TEMPLATES_DIR``
        with ``kind="paired"`` and sibling ``nodes:[...]`` + ``links:[...]``
        arrays (see :class:`scripts.import_eveng.adapters.juniper_vmx.JuniperVMXAdapter`).
        Each entry is a self-contained multi-node instantiation directive — the
        frontend renders these with a distinct visual treatment ("2 nodes" badge)
        and routes submissions to ``POST /nodes/from-paired-template`` instead of
        the single-node ``/nodes/batch`` path.
        """
        result: list[dict[str, Any]] = []
        for path, data in self._iter_paired_user_templates():
            nodes = data["nodes"]
            links = data["links"]
            paired_key = path.stem
            child_summary = [
                {
                    "id": str(child.get("id") or f"child-{i}"),
                    "name": str(child.get("name") or child.get("id") or f"child-{i}"),
                    "kind": str(child.get("kind") or "qemu"),
                    "image": str(child.get("image") or ""),
                    "cpu": int(child.get("cpu", 1)),
                    "ram": int(child.get("ram", 1024)),
                    "ethernet": int(child.get("ethernet", 1)),
                    # #206 — synthetic template key the child node carries on
                    # its ``template`` field once instantiated; lets the frontend
                    # cross-reference the matching catalog entry for capabilities,
                    # extras schema, etc.
                    "template_key": synthetic_paired_child_key(
                        paired_key, str(child.get("id") or f"child-{i}")
                    ),
                }
                for i, child in enumerate(nodes)
                if isinstance(child, dict)
            ]
            link_summary = [
                {
                    "from_node": str(link.get("from_node") or ""),
                    "from_iface": str(link.get("from_iface") or ""),
                    "to_node": str(link.get("to_node") or ""),
                    "to_iface": str(link.get("to_iface") or ""),
                }
                for link in links
                if isinstance(link, dict)
            ]
            invalid_reason = validate_paired_template(data)
            if invalid_reason is not None:
                _logger.warning(
                    "Paired template %s pre-flight failed: %s — instantiation "
                    "via /from-paired-template will return 422 until the "
                    "template is updated.",
                    path.stem,
                    invalid_reason,
                )
            result.append(
                {
                    "key": path.stem,
                    "name": str(data.get("name") or path.stem),
                    "vendor": str(data.get("vendor") or ""),
                    "child_count": len(child_summary),
                    "link_count": len(link_summary),
                    "children": child_summary,
                    "links": link_summary,
                    # #207 — pre-flighted at catalog-load time so old imports
                    # surface a banner-friendly reason. Endpoint converts this
                    # to a 422 if the operator tries to instantiate.
                    "valid": invalid_reason is None,
                    "invalid_reason": invalid_reason,
                }
            )
        return result

    def template_extras(self, template_type: str, template_key: str) -> dict[str, Any]:
        """Return the merged default extras (schema defaults + YAML overrides) for a template."""
        template = self.get_template(template_type, template_key)
        return self._compose_extras(_extras_schema_for(template_type), template.extras)

    def interface_naming(self, template_type: str, template_key: str) -> dict[str, Any]:
        """Return the validated ``interface_naming`` block for a template, or ``{}``."""
        template = self.get_template(template_type, template_key)
        return dict(template.interface_naming or {})

    @staticmethod
    def _compose_extras(schema: list[dict[str, Any]], yaml_overrides: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for field in schema:
            result[field["key"]] = field.get("default")
        for key, value in (yaml_overrides or {}).items():
            result[key] = value
        return result

    async def upload_image(
        self,
        template_type: str,
        template_key: str,
        filename: str,
        content: bytes,
        image_name: str | None = None,
    ) -> dict[str, Any]:
        self.get_template(template_type, template_key)
        safe_filename = Path(filename).name
        if not safe_filename:
            raise TemplateError("Uploaded image filename is empty.")

        target_image_name = (image_name or Path(safe_filename).stem).strip()
        if not target_image_name:
            raise TemplateError("Image name is invalid.")

        target_dir = self.images_dir / template_type / target_image_name
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / safe_filename
        target_path.write_bytes(content)
        return self._image_info(target_dir) or {
            "image": target_image_name,
            "filename": safe_filename,
            "path": str(target_path),
        }

    def _iter_template_paths(self) -> list[tuple[Path, str]]:
        """Return (path, source) for every *.yml/*.json template file across both dirs.

        ``source`` is ``"builtin"`` for entries from ``TEMPLATES_DIR`` and
        ``"user"`` for entries from ``USER_TEMPLATES_DIR``. User-dir entries
        come AFTER builtin entries so they win on key collision (#185).
        """
        paths: list[tuple[Path, str]] = []
        if self.templates_dir.exists():
            for p in sorted(self.templates_dir.rglob("*.yml")):
                paths.append((p, "builtin"))
        user_dir = self.user_templates_dir
        if user_dir is not None and user_dir.exists():
            for p in sorted(user_dir.rglob("*.yml")):
                paths.append((p, "user"))
            for p in sorted(user_dir.rglob("*.json")):
                paths.append((p, "user"))
        return paths

    def _load_templates(self) -> list[TemplateDefinition]:
        if not self.templates_dir.exists() and (
            self.user_templates_dir is None or not self.user_templates_dir.exists()
        ):
            return []

        # Track keys we've already loaded so we can detect shadowing.
        seen_keys_by_type: dict[tuple[str, str], str] = {}  # (type, key) -> source

        templates: list[TemplateDefinition] = []
        for template_path, source in self._iter_template_paths():
            payload = yaml.safe_load(template_path.read_text()) or {}
            template_type = str(payload.get("type") or template_path.parent.name).strip().lower()
            if template_type not in SUPPORTED_TEMPLATE_TYPES:
                continue

            key = template_path.stem
            type_key = (template_type, key)
            if type_key in seen_keys_by_type:
                prior_source = seen_keys_by_type[type_key]
                if source == "user" and prior_source == "builtin":
                    _logger.warning(
                        "USER_TEMPLATES_DIR template shadows builtin",
                        extra={
                            "template_type": template_type,
                            "template_key": key,
                            "user_path": str(template_path),
                        },
                    )
                # Remove the prior entry; the new one (last-loaded) wins.
                templates[:] = [
                    t for t in templates if not (t.type == template_type and t.key == key)
                ]
            seen_keys_by_type[type_key] = source
            yaml_extras = payload.get("extras") or {}
            if not isinstance(yaml_extras, dict):
                yaml_extras = {}
            yaml_extras = dict(yaml_extras)
            for schema_field in _extras_schema_for(template_type):
                schema_key = schema_field["key"]
                if schema_key in payload and schema_key not in yaml_extras:
                    yaml_extras[schema_key] = payload[schema_key]
            interface_naming = payload.get("interface_naming")
            if interface_naming is not None:
                interface_naming = _validate_interface_naming(
                    interface_naming, source=str(template_path)
                )
            capabilities = _validate_capabilities(
                payload.get("capabilities"), template_type, source=str(template_path)
            )
            templates.append(
                TemplateDefinition(
                    key=key,
                    type=template_type,
                    name=str(payload.get("name") or key),
                    description=str(payload.get("description") or ""),
                    icon_type=str(payload.get("icon_type") or self._default_icon_type(template_type)),
                    cpu=int(payload.get("cpu", 1)),
                    ram=int(payload.get("ram", 1024)),
                    ethernet=int(payload.get("ethernet", 1)),
                    console_type=str(payload.get("console_type") or self._default_console_type(template_type)),
                    cpulimit=int(payload.get("cpulimit", 1)),
                    extras=yaml_extras,
                    raw=payload,
                    interface_naming=interface_naming,
                    capabilities=capabilities,
                )
            )

        # #206 — append synthetic per-child entries for every paired template so
        # node.template lookups (edit, capability gates, image validation) resolve
        # cleanly. These entries advertise paired_parent so frontends can hide
        # them from the standalone create-flow type-tab picker.
        templates.extend(self._load_synthetic_paired_child_templates())
        return templates

    def _load_synthetic_paired_child_templates(self) -> list[TemplateDefinition]:
        """Synthesize per-child :class:`TemplateDefinition` entries from paired
        user templates (``USER_TEMPLATES_DIR/*.json`` with ``kind="paired"``).

        Each child becomes a first-class catalog entry keyed
        ``<paired_key>__<child_id>``. Capability/interface_naming blocks on the
        child are validated via the same helpers used for real templates so a
        broken child raises :class:`TemplateError` at load time rather than
        silently flowing through to runtime. The child's ``image`` is recorded
        on the synthetic ``raw`` so :meth:`list_images` can surface it (see
        the override in that method).
        """
        synthetic: list[TemplateDefinition] = []
        for path, data in self._iter_paired_user_templates():
            nodes = data["nodes"]
            paired_key = path.stem
            for index, child in enumerate(nodes):
                if not isinstance(child, dict):
                    continue
                child_id = str(child.get("id") or f"child-{index}")
                child_kind = str(child.get("kind") or "qemu").strip().lower()
                if child_kind not in SUPPORTED_TEMPLATE_TYPES:
                    child_kind = "qemu"
                synthetic_key = synthetic_paired_child_key(paired_key, child_id)
                source = f"{path}::nodes[{index}]"
                child_iface = child.get("interface_naming")
                if child_iface is not None:
                    child_iface = _validate_interface_naming(child_iface, source=source)
                child_caps = _validate_capabilities(
                    child.get("capabilities"), child_kind, source=source
                )
                yaml_extras = dict(child.get("extras") or {}) if isinstance(child.get("extras"), dict) else {}
                synthetic.append(
                    TemplateDefinition(
                        key=synthetic_key,
                        type=child_kind,
                        name=str(child.get("name") or synthetic_key),
                        description=f"Paired child of {paired_key} (auto-synthesized).",
                        icon_type=str(child.get("icon_type") or self._default_icon_type(child_kind)),
                        cpu=int(child.get("cpu", 1)),
                        ram=int(child.get("ram", 1024)),
                        ethernet=int(child.get("ethernet", 1)),
                        console_type=str(child.get("console") or self._default_console_type(child_kind)),
                        cpulimit=int(child.get("cpulimit", 1)),
                        extras=yaml_extras,
                        raw=dict(child),
                        interface_naming=child_iface,
                        capabilities=child_caps,
                        paired_parent=paired_key,
                    )
                )
        return synthetic

    def _normalize_type(self, template_type: str) -> str:
        normalized = template_type.strip().lower()
        if normalized not in SUPPORTED_TEMPLATE_TYPES:
            raise TemplateError(f"Unsupported template type: {template_type}")
        return normalized

    def _image_info(self, path: Path) -> dict[str, Any] | None:
        if path.is_dir():
            files = sorted(child.name for child in path.iterdir() if child.is_file())
            if not files:
                return None
            return {
                "image": path.name,
                "files": files,
                "path": str(path),
            }

        if path.is_file():
            return {
                "image": path.stem,
                "files": [path.name],
                "path": str(path),
            }
        return None

    def _docker_image_catalog(self) -> dict[str, dict[str, Any]]:
        docker_binary = shutil.which("docker")
        if not docker_binary:
            return {}

        env = os.environ.copy()
        if getattr(self.settings, "DOCKER_HOST", ""):
            env["DOCKER_HOST"] = self.settings.DOCKER_HOST

        command = [
            docker_binary,
            "image",
            "ls",
            "--format",
            "{{.Repository}}:{{.Tag}}",
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
        if result.returncode != 0:
            return {}

        images: dict[str, dict[str, Any]] = {}
        for line in result.stdout.splitlines():
            image_name = line.strip()
            if not image_name or image_name.endswith(":<none>") or image_name.startswith("<none>:"):
                continue
            images[image_name] = {
                "image": image_name,
                "files": [],
                "path": image_name,
                "source": "docker",
            }
        return images

    @staticmethod
    def _default_icon_type(template_type: str) -> str:
        if template_type == "docker":
            return "server"
        return "router"

    @staticmethod
    def _default_console_type(template_type: str) -> str:
        if template_type == "docker":
            return "rdp"
        return "telnet"
