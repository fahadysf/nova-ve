from contextlib import ExitStack, suppress
import copy
from dataclasses import dataclass, field
import errno
from fnmatch import fnmatchcase
from pathlib import Path
import logging
import os
import shutil
from stat import S_ISREG
import subprocess
import threading
from typing import Any, BinaryIO, Iterable, Iterator
from uuid import uuid4

import yaml
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.schemas.node import validate_second_console_mode_for_type


_logger = logging.getLogger("nova-ve.template_service")


SUPPORTED_TEMPLATE_TYPES = {"qemu", "docker", "iol", "dynamips"}
_IMAGE_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_IMAGE_COPY_CHUNK_BYTES = 1024 * 1024
_IMAGE_ACCESS_ACL = "system.posix_acl_access"

QEMU_MACHINE_OPTIONS = {"q35", "pc"}
_QEMU_MAX_NICS_DEFAULT = 8
_QEMU_MAX_NICS_HARD_CAP = 25
_DOCKER_MAX_NICS_DEFAULT = 99

# #206 — synthetic per-child template key separator. A paired template's child
# is exposed in the catalog as ``<paired_key>__<child_id>`` so node.template
# lookups (edit, capability, image validation) resolve to a real entry.
SYNTHETIC_PAIRED_CHILD_SEP = "__"


def synthetic_paired_child_key(paired_key: str, child_id: str) -> str:
    return f"{paired_key}{SYNTHETIC_PAIRED_CHILD_SEP}{child_id}"


def normalize_paired_child_kind(raw: Any) -> str:
    """Normalize paired-child kinds for both predictor and runtime.

    Whitespace is stripped before the allowlist check because imported
    template data commonly arrives human-edited, and ``" docker "`` should
    behave like ``"docker"`` instead of silently falling back to ``qemu``.
    Keeping this rule in one helper prevents predictor/runtime drift.
    """
    kind = str(raw or "qemu").strip().lower()
    if kind not in {"qemu", "docker", "iol", "dynamips"}:
        return "qemu"
    return kind


def _standalone_template_type(payload: Any, path: Path) -> str | None:
    if not isinstance(payload, dict) or payload.get("kind") == "paired":
        return None
    template_type = str(payload.get("type") or path.parent.name).strip().lower()
    if template_type not in SUPPORTED_TEMPLATE_TYPES:
        return None
    return template_type


def _paired_child_collision_reason(
    paired_key: str,
    child_id: str,
    child: dict[str, Any],
    standalone_keys: set[tuple[str, str]],
) -> str | None:
    child_kind = normalize_paired_child_kind(child.get("kind"))
    generated_key = synthetic_paired_child_key(paired_key, child_id)
    if (child_kind, generated_key) not in standalone_keys:
        return None
    return (
        f"child {child_id!r} generated key {generated_key!r} for normalized "
        f"runtime kind {child_kind!r} collides with a standalone template. "
        "Change the paired child ID or rename the standalone template file so "
        "the generated key is unique."
    )


def _paired_template_collision_reason(
    paired_key: str,
    data: dict[str, Any],
    standalone_keys: set[tuple[str, str]],
) -> str | None:
    for index, child in enumerate(data.get("nodes") or []):
        if not isinstance(child, dict):
            continue
        child_id = str(child.get("id") or f"child-{index}")
        reason = _paired_child_collision_reason(
            paired_key, child_id, child, standalone_keys
        )
        if reason is not None:
            return reason
    return None


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
       b. Neutral ``eth{n}`` fallback.
    2. Overlay ``interface_naming.explicit:[name0, name1, ...]`` onto
       positions ``0..len(explicit)-1`` of the base.

    Codex-iter2 fix: previously this returned the explicit list verbatim,
    so a child with ``ethernet=2, explicit=["mgmt0"]`` was predicted as
    ``["mgmt0"]`` and any link to the second interface (``eth1``)
    was wrongly flagged invalid. The runtime overlays explicit onto a
    full-length base, so the predictor must too.
    """
    if normalize_paired_child_kind(child.get("kind")) == "dynamips":
        ethernet, _ = paired_dynamips_config(child)
    else:
        ethernet = int(child.get("ethernet", 1))
    iface_naming = child.get("interface_naming") if isinstance(child.get("interface_naming"), dict) else None

    base = [f"eth{i}" for i in range(ethernet)]
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
            if normalized:
                base = [render_interface_name(normalized, i) for i in range(ethernet)]

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


def paired_child_iface_index(child: dict, name: str, available: list[str]) -> int | None:
    """Resolve an exact name or the former QEMU fallback for the same port."""
    if name and name in available:
        return available.index(name)
    naming = child.get("interface_naming") or {}
    if normalize_paired_child_kind(child.get("kind")) != "qemu" or naming.get("format"):
        return None
    explicit = naming.get("explicit") or []
    for idx, label in enumerate(available):
        if idx < len(explicit) and isinstance(explicit[idx], str):
            continue
        if label == f"eth{idx}" and name == f"Gi{idx + 1}":
            return idx
    return None


_PAIRED_CHILD_INT_FIELDS: tuple[tuple[str, int], ...] = (
    ("cpu", 1),
    ("ram", 1024),
    ("ethernet", 1),
    ("cpulimit", 1),
)


def _paired_child_scalar_problem(child: dict[str, Any]) -> str | None:
    """Return a reason string if any of the int-typed scalar fields on a
    paired-child block can't be cast to int, else ``None``. Centralizes the
    scalar-shape check so :func:`validate_paired_template`,
    :meth:`TemplateService._build_paired_catalog`, and
    :meth:`TemplateService._load_synthetic_paired_child_templates` agree on
    what counts as a valid child without each having to defensively cast at
    consumption time (#208 codex-iter3).
    """
    for field_name, _default in _PAIRED_CHILD_INT_FIELDS:
        if field_name not in child:
            continue
        raw = child[field_name]
        try:
            int(raw)
        except (TypeError, ValueError):
            return (
                f"field {field_name!r} must be an integer "
                f"(got {raw!r} of type {type(raw).__name__})"
            )
    return None


def _safe_paired_child_int(child: dict[str, Any], field: str, default: int) -> int:
    """Cast a paired-child scalar to int with a fallback default. Used by the
    catalog/synthetic loaders so a malformed scalar doesn't crash the whole
    catalog response — :func:`validate_paired_template` already flags the
    template as ``valid:false`` so the operator sees the real reason."""
    if field not in child:
        return default
    try:
        return int(child[field])
    except (TypeError, ValueError):
        return default


def _safe_paired_ethernet_count(child: dict[str, Any]) -> int:
    if normalize_paired_child_kind(child.get("kind")) == "dynamips":
        try:
            return paired_dynamips_config(child)[0]
        except (TemplateError, TypeError, ValueError):
            pass
    return _safe_paired_child_int(child, "ethernet", 1)


def validate_paired_template(data: dict[str, Any]) -> str | None:
    """Pre-flight a paired template (#207). Returns ``None`` when the template
    can be instantiated end-to-end, or a human-readable reason string when a
    link references an interface name that no child will expose, a child has
    malformed scalar data (#208), or a child carries invalid interface naming
    / capabilities blocks that synthetic child loading would reject. Used by
    the catalog builder to flag old imports + by the from-paired-template
    endpoint to return 422 instead of 500.
    """
    nodes = data.get("nodes") or []
    links = data.get("links") or []
    children_by_id: dict[str, dict[str, Any]] = {}
    for child in nodes:
        if isinstance(child, dict):
            children_by_id[str(child.get("id") or "")] = child

    # #208 codex-iter3 — scalar-shape check up front so a bad cpu/ram/etc
    # fails pre-flight with a meaningful reason instead of crashing at
    # instantiation. Predictor only consumes ``ethernet`` so this also
    # covers the cpu/ram/cpulimit gap that previously slipped past
    # pre-flight to the node-creation handler.
    for child_id, child in children_by_id.items():
        scalar_reason = _paired_child_scalar_problem(child)
        if scalar_reason is not None:
            return f"child {child_id!r} {scalar_reason}"
        child_kind = normalize_paired_child_kind(child.get("kind"))
        child_source = f"paired child {child_id!r}"
        try:
            child_iface = child.get("interface_naming")
            if child_iface is not None:
                _validate_interface_naming(child_iface, source=child_source)
            if child_kind == "dynamips":
                paired_dynamips_config(child)
            _validate_capabilities(
                child.get("capabilities"), child_kind, source=child_source
            )
        except TemplateError as exc:
            return f"child {child_id!r} {exc}"

    for link in links:
        if not isinstance(link, dict):
            continue
        for endpoint_key, iface_key in (("from_node", "from_iface"), ("to_node", "to_iface")):
            child_id = str(link.get(endpoint_key) or "")
            iface_name = str(link.get(iface_key) or "")
            child = children_by_id.get(child_id)
            if child is None:
                return f"link references unknown child id {child_id!r}"
            # #208-MEDIUM — predictor casts ``ethernet`` to int. A child with
            # ``ethernet: "not-an-int"`` would otherwise propagate ValueError
            # uncaught out of pre-flight back to the FastAPI handler → 500.
            # Surface as a reason string so the endpoint returns 422.
            try:
                available = _paired_child_iface_names(child)
            except (ValueError, TypeError) as exc:
                return (
                    f"child {child_id!r} has malformed scalar field "
                    f"(ethernet must be an int): {exc}"
                )
            if paired_child_iface_index(child, iface_name, available) is None:
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
    if template_type == "iol":
        return {"hotplug": False, "max_nics": _QEMU_MAX_NICS_DEFAULT, "machine": None}
    # qemu and older/unknown runtime templates default to the runtime-capable q35 profile
    return {"hotplug": True, "max_nics": _QEMU_MAX_NICS_DEFAULT, "machine": "q35"}


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

ICON_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _icon_media_type(filename: str) -> str | None:
    if (
        not filename
        or filename.startswith(".")
        or len(os.fsencode(filename)) > 255
        or any(c in filename for c in ("/", "\\", "\0"))
    ):
        return None
    return ICON_MEDIA_TYPES.get(Path(filename).suffix.lower())


def _custom_icon_filename(icon: Any) -> str | None:
    return icon if isinstance(icon, str) and _icon_media_type(icon) else None


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

DYNAMIPS_PLATFORM_OPTIONS = [
    {"value": "c3725", "label": "c3725"},
    {"value": "c7200", "label": "c7200"},
]

DYNAMIPS_C3725_SLOT_OPTIONS = [
    {"value": "", "label": "(empty)"},
    {"value": "GT96100-FE", "label": "GT96100-FE (built-in 2x FE)"},
    {"value": "NM-1FE-TX", "label": "NM-1FE-TX"},
    {"value": "NM-4T", "label": "NM-4T (4 serial)"},
    {"value": "NM-16ESW", "label": "NM-16ESW (16-port switch)"},
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

# Maximum number of slot fields exposed per platform.  Used by
# _dynamips_extras_schema so the UI and the importer stay in sync when
# we add a new platform or increase slot count.
_DYNAMIPS_PLATFORM_MAX_SLOTS: dict[str, int] = {"c3725": 3, "c7200": 7}

# Saved blank slot 0 values historically inherited the c7200 I/O card.
# Use a distinct value when a user explicitly removes the card.
DYNAMIPS_NO_IO_CARD = "none"
# Older labs address every NIO by its flat slot order. New layouts expose
# Ethernet only. A saved lab keeps its ordering until a safe layout edit.
DYNAMIPS_ETHERNET_ONLY = "dynamips_ethernet_only"
_DYNAMIPS_SLOT_ZERO_DEFAULT = {"c3725": "GT96100-FE", "c7200": "C7200-IO-FE"}


def dynamips_slot_options(platform: str, index: int) -> list[dict[str, Any]]:
    if platform == "c3725":
        options = [
            option for option in DYNAMIPS_C3725_SLOT_OPTIONS
            if (option["value"] == "GT96100-FE") == (index == 0)
        ]
    elif index == 0:
        options = [
            {"value": DYNAMIPS_NO_IO_CARD, "label": "(empty)"},
            *(option for option in DYNAMIPS_C7200_SLOT_OPTIONS if option["value"].startswith("C7200-IO-")),
        ]
    else:
        options = [option for option in DYNAMIPS_C7200_SLOT_OPTIONS if not option["value"].startswith("C7200-IO-")]
    return [
        {**option, "ethernetPorts": _DYNAMIPS_PA_PORT_COUNT.get(option["value"], 0)}
        for option in options
    ]

# Ethernet port count contributed by each PA/NM module.  Zero means the
# module exists (serial, ATM, POS) but adds no Ethernet interfaces.
# NOTE: backend/scripts/import_eveng/adapters/dynamips.py imports this dict
# directly via ``from app.services.template_service import _DYNAMIPS_PA_PORT_COUNT``.
# If you add a new module here, add it there too (or it will silently be
# skipped during import — see the "Unknown PA" fallback comment).
_DYNAMIPS_PA_PORT_COUNT: dict[str, int] = {
    # c3725 NM modules
    "GT96100-FE": 2,    # built-in 2× FastEthernet
    "NM-1FE-TX": 1,
    "NM-4T": 0,         # serial only
    "NM-16ESW": 16,
    "NM-1E": 1,
    "NM-4E": 4,
    # c7200 PA modules
    "C7200-IO-FE": 1,
    "C7200-IO-2FE": 2,
    "C7200-IO-GE-E": 1,
    "PA-FE-TX": 1,
    "PA-2FE-TX": 2,
    "PA-GE": 1,
    "PA-4E": 4,
    "PA-8E": 8,
    "PA-4T+": 0,        # serial only
    "PA-8T": 0,         # serial only
    "PA-A1": 0,         # ATM
    "PA-POS-OC3": 0,    # POS
}

DYNAMIPS_NIO_PORT_COUNT = {
    **_DYNAMIPS_PA_PORT_COUNT,
    "NM-4T": 4,
    "PA-4T+": 4,
    "PA-8T": 8,
    "PA-A1": 1,
    "PA-POS-OC3": 1,
}


def dynamips_slot_inventory(
    platform: str, extras: dict[str, Any], *, validate_slots: bool = True
) -> list[tuple[int, str]]:
    """Resolve supported bindings, retaining the inherited slot 0 default."""
    max_slots = _DYNAMIPS_PLATFORM_MAX_SLOTS.get(platform)
    if max_slots is None:
        raise TemplateError(f"Unsupported Dynamips platform {platform!r}.")
    slot_keys = [f"slot{index}" for index in range(max_slots)]
    if validate_slots:
        for key, value in extras.items():
            if value and key.startswith("slot") and key[4:].isdigit() and key not in slot_keys:
                raise TemplateError(f"Dynamips {platform} does not have {key}.")
    inventory = []
    for index, key in enumerate(slot_keys):
        adapter = str(extras.get(key) or "").strip()
        if index == 0 and not adapter:
            adapter = _DYNAMIPS_SLOT_ZERO_DEFAULT[platform]
        if validate_slots:
            allowed = {option["value"] for option in dynamips_slot_options(platform, index)}
        else:
            allowed = {"", DYNAMIPS_NO_IO_CARD, *DYNAMIPS_NIO_PORT_COUNT}
        if adapter not in allowed:
            raise TemplateError(f"Dynamips port adapter {adapter!r} is not supported in {platform} slot {index}.")
        if adapter and adapter != DYNAMIPS_NO_IO_CARD:
            inventory.append((index, adapter))
    return inventory


def dynamips_attachment_inventory(
    inventory: list[tuple[int, str]], extras: dict[str, Any]
) -> list[tuple[int, str]]:
    if extras.get(DYNAMIPS_ETHERNET_ONLY) is True:
        return [(slot, name) for slot, name in inventory if _DYNAMIPS_PA_PORT_COUNT[name]]
    return inventory


def dynamips_port_layout(
    extras: dict[str, Any], *, validate_slots: bool = True
) -> list[tuple[str, int, str, int]]:
    """Return physical port identities in the launcher's attachment order."""
    platform = str(extras.get("platform") or "c7200").lower()
    inventory = dynamips_slot_inventory(platform, extras, validate_slots=validate_slots)
    inventory = dynamips_attachment_inventory(inventory, extras)
    return [
        (platform, slot, adapter, port)
        for slot, adapter in inventory
        for port in range(DYNAMIPS_NIO_PORT_COUNT[adapter])
    ]


def dynamips_nvram_size(extras: dict[str, Any]) -> int | None:
    raw_size = extras.get("nvram")
    if raw_size in (None, ""):
        return None
    try:
        size = int(raw_size)
    except (ValueError, TypeError, OverflowError) as exc:
        raise TemplateError("Dynamips NVRAM must be a positive size in KB.") from exc
    if size <= 0 or isinstance(raw_size, bool) or (isinstance(raw_size, float) and size != raw_size):
        raise TemplateError("Dynamips NVRAM must be a positive size in KB.")
    return size


def paired_dynamips_config(child: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    raw_extras = child.get("extras") or {}
    if not isinstance(raw_extras, dict):
        raise TemplateError("Dynamips paired child extras must be an object.")
    extras = dict(raw_extras)
    dynamips_nvram_size(extras)
    extras.pop(DYNAMIPS_ETHERNET_ONLY, None)
    if "ethernet" not in child:
        extras[DYNAMIPS_ETHERNET_ONLY] = True
        return len(dynamips_port_layout(extras)), extras
    available = len(dynamips_port_layout(extras))
    requested = int(child["ethernet"])
    if not 0 <= requested <= available:
        raise TemplateError(f"Dynamips paired child exposes {requested} interfaces but has {available} ports.")
    return requested, extras


def _qemu_version_options() -> list[dict[str, str]]:
    """Dropdown options for the #278 emulator selector.

    ``Host default`` (the distribution QEMU) first, then every side-by-side
    build present under ``QEMU_VERSIONS_ROOT``. Served fresh per catalog
    request, so a newly installed ``nova-ve-qemu-*`` deb shows up without a
    backend restart.
    """
    from app.services import qemu_versions

    default_label = "Host default"
    default_version = qemu_versions.default_version()
    if default_version != "unknown":
        default_label = f"Host default ({default_version})"
    options = [{"value": "", "label": default_label}]
    for version in qemu_versions.installed_versions():
        options.append({"value": version, "label": version})
    return options


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
            "key": "nested_virt",
            "label": "Nested virtualization",
            "type": "select",
            "options": [
                {"value": "false", "label": "Off"},
                {"value": "true", "label": "On"},
            ],
            "default": "false",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "disk_bus",
            "label": "Disk bus",
            "type": "select",
            "options": [
                {"value": "virtio", "label": "VirtIO (virtioa)"},
                {"value": "sata", "label": "SATA / AHCI (sataa)"},
                {"value": "ide", "label": "IDE (hda)"},
            ],
            "default": "virtio",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "qemu_version",
            "label": "QEMU version",
            "type": "select",
            "options": _qemu_version_options(),
            "default": "",
            "description": "Side-by-side emulator generation for this node (#278). Host default uses the distribution QEMU.",
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
            "key": "vnc_port",
            "label": "VNC port",
            "type": "number",
            "default": 5900,
            "placeholder": "5900",
            "description": "Container-side VNC port used by the HTML5 console when console mode is vnc.",
            "stoppedOnly": True,
            "runtime": True,
        },
        {
            "key": "mgmt_access",
            "label": "Management access (mgmt0)",
            "type": "select",
            "options": [
                {"value": "false", "label": "Disabled"},
                {"value": "true", "label": "Enabled"},
            ],
            "default": "false",
            "description": (
                "Attach an extra mgmt0 interface onto the lab's dedicated "
                "management bridge (172.16.X.0/24, per lab) with a "
                "host-assigned static IP. On-link reachability only — the "
                "host and mgmt peers in this lab; no default route, no "
                "internet. Lab interfaces keep their eth0..N numbering. "
                "Applied at node start."
            ),
            "stoppedOnly": True,
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


def _dynamips_extras_schema(platform: str = "c7200") -> list[dict[str, Any]]:
    """Platform-aware Dynamips extras schema.

    The first field is always the platform selector so the frontend can
    re-render the rest of the form when the user switches platforms.
    Slot count, slot options, NVRAM default, and the presence/absence of
    NPE/midplane fields all depend on the platform.
    """
    schema: list[dict[str, Any]] = [
        {
            "key": "platform",
            "label": "Platform",
            "type": "select",
            "options": DYNAMIPS_PLATFORM_OPTIONS,
            "default": platform,
            "stoppedOnly": True,
            "runtime": True,
        },
    ]

    if platform == "c3725":
        # c3725 exposes 3 slot fields: slot 0 (motherboard GT96100-FE),
        # slot 1 (NM slot), and slot 2 (NM slot).
        for index in range(_DYNAMIPS_PLATFORM_MAX_SLOTS["c3725"]):
            schema.append(
                {
                    "key": f"slot{index}",
                    "label": f"Slot {index}",
                    "type": "select",
                    "options": dynamips_slot_options(platform, index),
                    "default": "GT96100-FE" if index == 0 else "",
                    "stoppedOnly": True,
                    "runtime": True,
                }
            )
        nvram_default = 128
    else:  # c7200
        for index in range(_DYNAMIPS_PLATFORM_MAX_SLOTS["c7200"]):
            schema.append(
                {
                    "key": f"slot{index}",
                    "label": f"Slot {index}",
                    "type": "select",
                    "options": dynamips_slot_options(platform, index),
                    "default": "C7200-IO-FE" if index == 0 else "",
                    "stoppedOnly": True,
                    "runtime": True,
                }
            )
        nvram_default = 128

    schema.append(
        {
            "key": "nvram",
            "label": "NVRAM (KB)",
            "type": "number",
            "default": nvram_default,
            "stoppedOnly": True,
        }
    )

    disk0_default = 16 if platform == "c3725" else 64
    schema.append(
        {
            "key": "disk0",
            "label": "Flash (disk0) MB",
            "type": "number",
            "default": disk0_default,
            "stoppedOnly": True,
            "runtime": True,
        }
    )

    if platform == "c7200":
        schema += [
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
        ]

    schema.append(
        {
            "key": "idlepc",
            "label": "Idle PC",
            "type": "text",
            "default": "",
            "placeholder": "0x...",
            "stoppedOnly": True,
            "runtime": True,
        }
    )
    return schema


def _extras_schema_for(
    template_type: str, template_extras: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    if template_type == "qemu":
        return _qemu_extras_schema()
    if template_type == "docker":
        return _docker_extras_schema()
    if template_type == "iol":
        return _iol_extras_schema()
    if template_type == "dynamips":
        platform = "c7200"
        if template_extras:
            raw = str(template_extras.get("platform") or "").lower()
            if raw in {"c3725", "c7200"}:
                platform = raw
        return _dynamips_extras_schema(platform)
    return []


class TemplateError(Exception):
    pass


class ImageUploadConflict(TemplateError):
    pass


SECOND_CONSOLE_MODES = ("telnet", "vnc", "rdp")


def _validate_second_console(payload: Any, template_type: str, console_type: str, source: str) -> str | None:
    """Normalize + validate a template's ``second_console`` declaration (#268).

    ``None``/empty clears it. The mode must be one of the standard modes and
    must compose with the primary (mirror of the node-level rule so invalid
    combinations are rejected at template load and template save, not only
    at node create).
    """
    if payload is None or (isinstance(payload, str) and not payload.strip()):
        return None
    if not isinstance(payload, str) or payload.strip() not in SECOND_CONSOLE_MODES:
        modes = ", ".join(SECOND_CONSOLE_MODES)
        raise TemplateError(
            f"second_console on {source} must be one of: {modes} (or empty for no secondary console)."
        )
    second_console = payload.strip()
    try:
        validate_second_console_mode_for_type(template_type, console_type, second_console)
    except ValueError as exc:
        raise TemplateError(f"second_console on {source}: {exc}")
    return second_console


def _validate_image_aliases(payload: Any, source: str) -> list[str]:
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise TemplateError(f"image_aliases on {source} must be a list of strings.")
    aliases: list[str] = []
    for item in payload:
        if not isinstance(item, str) or not item.strip():
            raise TemplateError(
                f"image_aliases on {source} must contain only non-empty strings."
            )
        aliases.append(item.strip().lower())
    return aliases


def _matches_template_image_name(template: "TemplateDefinition", image_name: str) -> bool:
    image_match = image_name.strip().lower()
    if not image_match:
        return False

    display_name = template.name.strip().lower()
    if not display_name and not template.image_aliases:
        return True
    if display_name and (display_name in image_match or image_match in display_name):
        return True

    # Plain aliases retain the legacy containment check. A wildcard alias
    # matches the whole normalized name, so win-* and winserver-* stay distinct.
    return any(
        fnmatchcase(image_match, alias)
        if "*" in alias
        else alias in image_match or image_match in alias
        for alias in template.image_aliases
    )


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


# Process-wide cache for ``TemplateService._load_templates``: a single
# ``(files fingerprint, templates)`` entry under key ``"entry"``.
_TEMPLATE_CACHE: dict[str, tuple[tuple, list["TemplateDefinition"]]] = {}
_TEMPLATE_CACHE_LOCK = threading.Lock()


def invalidate_template_cache() -> None:
    """Drop cached template definitions; call after writing template files."""
    with _TEMPLATE_CACHE_LOCK:
        _TEMPLATE_CACHE.clear()


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
    # Optional second console mode declared on the template (#268); nodes
    # created from it inherit the value and get a second console session.
    second_console: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    interface_naming: dict[str, Any] | None = None
    capabilities: dict[str, Any] = field(default_factory=dict)
    image_aliases: list[str] = field(default_factory=list)
    inherited_image_aliases: list[str] | None = None
    user_template_path: Path | None = None
    # #206 — when this entry was synthesized from a paired-template child block
    # (vs loaded from a per-type YAML), ``paired_parent`` is the originating
    # paired template key. Surfaced in the catalog response so the frontend can
    # exclude these from the standalone "Add node" type-tab picker while still
    # resolving them by key for edit-mode lookups and capability gates.
    paired_parent: str | None = None

    @property
    def custom_icon(self) -> str | None:
        return _custom_icon_filename(self.raw.get("icon"))

    @property
    def icon(self) -> str:
        return self.custom_icon or _icon_filename_for(self.icon_type)

    @property
    def source(self) -> str:
        if self.user_template_path is None:
            return "builtin"
        return "override" if self.inherited_image_aliases is not None else "user"

    def as_response(self) -> dict[str, Any]:
        return {
            "id": self.key,
            "template": self.key,
            "type": self.type,
            "name": self.name,
            "description": self.description,
            "icon_type": self.icon_type,
            "icon": self.icon,
            "cpu": self.cpu,
            "ram": self.ram,
            "ethernet": self.ethernet,
            "console_type": self.console_type,
            "second_console": self.second_console,
            "cpulimit": self.cpulimit,
            "extras": dict(self.extras),
            "capabilities": dict(self.capabilities),
            "interface_naming": self.interface_naming,
            "image_aliases": list(self.image_aliases),
            "paired_parent": self.paired_parent,
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

    def _read_user_template_payloads(self) -> Iterator[tuple[Path, Any]]:
        user_templates_dir = getattr(self, "user_templates_dir", None)
        if user_templates_dir is None or not user_templates_dir.exists():
            return
        paths = [
            *user_templates_dir.rglob("*.yml"),
            *user_templates_dir.rglob("*.json"),
        ]
        for path in sorted(paths):
            try:
                data = yaml.safe_load(path.read_text()) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
            yield path, data

    def _iter_paired_user_templates(
        self, *, parsed_templates: Iterable[tuple[Path, Any]] | None = None
    ) -> Iterator[tuple[Path, dict[str, Any]]]:
        """Yield (path, payload) for every well-formed ``kind="paired"`` template
        under ``USER_TEMPLATES_DIR``. Tolerates I/O + parse errors, skips
        non-paired files, and enforces the minimal structural invariant
        (non-empty ``nodes`` list + ``links`` list). Shared by the loader,
        the catalog builder, and the per-key lookup so they cannot drift.
        """
        if parsed_templates is None:
            parsed_templates = self._read_user_template_payloads()
        for path, data in parsed_templates:
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
        malformed templates.
        """
        for path, data in self._iter_paired_user_templates():
            if path.stem == template_key:
                return data
        return None

    def _standalone_keys_for_paired_template(
        self, template_key: str, data: dict[str, Any]
    ) -> tuple[set[tuple[str, str]], str | None]:
        generated_keys = {
            synthetic_paired_child_key(
                template_key, str(child.get("id") or f"child-{index}")
            )
            for index, child in enumerate(data.get("nodes") or [])
            if isinstance(child, dict)
        }
        standalone_keys: set[tuple[str, str]] = set()
        for path, _source in self._iter_template_paths():
            if path.stem not in generated_keys:
                continue
            try:
                payload = yaml.safe_load(path.read_text()) or {}
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                payload = None
            if not isinstance(payload, dict):
                return set(), (
                    f"template candidate for generated key {path.stem!r} could "
                    "not be parsed as a standalone template. Change the paired "
                    "child ID or rename the standalone template file, or fix "
                    "the candidate's YAML."
                )
            standalone_type = _standalone_template_type(payload, path)
            if standalone_type is not None:
                standalone_keys.add((standalone_type, path.stem))
        return standalone_keys, None

    def paired_template_invalid_reason(
        self,
        template_key: str,
        data: dict[str, Any],
        *,
        standalone_templates: list[TemplateDefinition] | None = None,
    ) -> str | None:
        semantic_reason = validate_paired_template(data)
        if semantic_reason is not None:
            return semantic_reason
        if standalone_templates is None:
            standalone_keys, candidate_reason = (
                self._standalone_keys_for_paired_template(template_key, data)
            )
            if candidate_reason is not None:
                return candidate_reason
        else:
            standalone_keys = {
                (template.type, template.key) for template in standalone_templates
            }
        return _paired_template_collision_reason(
            template_key, data, standalone_keys
        )

    def is_paired_user_template(self, template_key: str) -> bool:
        """Boolean check used by the single-node from-template endpoint to 400 paired keys."""
        return self.get_paired_user_template(template_key) is not None

    def list_images(self, template_type: str, template_key: str) -> dict[str, dict[str, Any]]:
        template = self.get_template(template_type, template_key)
        return self._list_images(template)

    def _list_images(
        self,
        template: TemplateDefinition,
        docker_images: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        # #206 — synthetic paired children declare their image explicitly in
        # the paired template JSON. Surface only that image so validation in
        # validate_node_request matches what the paired-create endpoint uses.
        if template.paired_parent is not None:
            declared = str(template.raw.get("image") or "").strip()
            if declared:
                return {declared: {"image": declared, "source": "paired-child-declared"}}
            return {}

        images: dict[str, dict[str, Any]] = {}

        if template.type == "docker":
            # Platform container templates (#288) declare their image in the
            # YAML ``image:`` key — pin the template to it so the catalog's
            # ``defaults.image`` and node-request validation resolve to the
            # platform image instead of whichever marked image sorts first.
            declared = str(template.raw.get("image") or "").strip()
            if declared:
                return {declared: {"image": declared, "source": "template-declared"}}
            # Docker images live in the docker daemon, not the filesystem --
            # the marker-tag catalog is authoritative for the picker. Skip the
            # ``/var/lib/nova-ve/images/docker/`` walk entirely so legacy
            # placeholder directories cannot leak un-curated entries into the
            # add-node modal.
            if docker_images is None:
                docker_images = self._docker_image_catalog()
            images.update(docker_images)
            return images

        image_root = self.images_dir / template.type
        if image_root.exists():
            for child in sorted(image_root.iterdir()):
                image_info = self._image_info(child)
                if not image_info:
                    continue
                folder_match = str(image_info["image"])
                if not _matches_template_image_name(template, folder_match):
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
            icons.add(template.icon)
        return sorted(icons)

    def _icon_directory(self) -> Path | None:
        if self.user_templates_dir is None:
            return None
        directory = self.user_templates_dir / "icons"
        return None if directory.is_symlink() else directory

    def icon_asset(self, filename: str) -> tuple[Path, str] | None:
        media_type = _icon_media_type(filename)
        directory = self._icon_directory()
        if media_type is None or directory is None:
            return None
        path = directory / filename
        if not path.is_symlink() and path.is_file():
            return path, media_type
        return None

    def list_icon_assets(self) -> list[str]:
        try:
            directory = self._icon_directory()
            if directory is None:
                return []
            return sorted(
                path.name
                for path in directory.iterdir()
                if self.icon_asset(path.name)
            )
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return []

    def build_node_catalog(
        self, *, strict_docker: bool = False, discover_docker_images: bool = True,
    ) -> dict[str, Any]:
        templates: list[dict[str, Any]] = []
        parsed_templates = list(self._read_user_template_payloads())
        paired_templates = list(
            self._iter_paired_user_templates(parsed_templates=parsed_templates)
        )
        loaded_templates = self._load_templates(
            paired_templates=paired_templates,
            parsed_templates=parsed_templates,
        )
        icon_assets = self.list_icon_assets()
        icon_options = sorted(
            {"Router.png", "Server.png"}
            | {template.icon for template in loaded_templates}
            | set(icon_assets)
        )
        needs_docker_images = discover_docker_images and any(
            template.type == "docker"
            and template.paired_parent is None
            and not str(template.raw.get("image") or "").strip()
            for template in loaded_templates
        )
        docker_images = self._docker_image_catalog(strict=strict_docker) if needs_docker_images else {}
        for template in loaded_templates:
            images = list(self._list_images(template, docker_images).values())
            default_image = images[0]["image"] if images else ""
            extras_schema = _extras_schema_for(template.type, template.extras)
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
                        "icon": template.icon,
                        "cpu": template.cpu,
                        "ram": template.ram,
                        "ethernet": template.ethernet,
                        "console_type": template.console_type,
                        "second_console": template.second_console,
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
        paired_catalog = self._build_paired_catalog(
            standalone_templates=[
                template for template in loaded_templates if template.paired_parent is None
            ],
            paired_templates=paired_templates,
        )
        return {
            "templates": templates,
            "paired_templates": paired_catalog,
            "icon_options": icon_options,
            "icon_assets": icon_assets,
            "dynamips_profiles": {
                platform: _dynamips_extras_schema(platform)
                for platform in _DYNAMIPS_PLATFORM_MAX_SLOTS
            },
        }

    def _build_paired_catalog(
        self,
        *,
        standalone_templates: list[TemplateDefinition] | None = None,
        paired_templates: list[tuple[Path, dict[str, Any]]] | None = None,
    ) -> list[dict[str, Any]]:
        """Enumerate paired user-templates (#202) for the node catalog response.

        Paired templates live as YAML or legacy JSON files under ``USER_TEMPLATES_DIR``
        with ``kind="paired"`` and sibling ``nodes:[...]`` + ``links:[...]``
        arrays (see :class:`scripts.import_eveng.adapters.juniper_vmx.JuniperVMXAdapter`).
        Each entry is a self-contained multi-node instantiation directive — the
        frontend renders these with a distinct visual treatment ("2 nodes" badge)
        and routes submissions to ``POST /nodes/from-paired-template`` instead of
        the single-node ``/nodes/batch`` path.
        """
        if standalone_templates is None or paired_templates is None:
            parsed_templates = list(self._read_user_template_payloads())
        if standalone_templates is None:
            standalone_templates = self._load_standalone_templates(
                parsed_templates=parsed_templates
            )
        if paired_templates is None:
            paired_templates = list(
                self._iter_paired_user_templates(parsed_templates=parsed_templates)
            )

        result: list[dict[str, Any]] = []
        for path, data in paired_templates:
            nodes = data["nodes"]
            links = data["links"]
            paired_key = path.stem
            # #208 codex-iter3 — int casts are scoped to ``_safe_paired_child_int``
            # so a malformed scalar can't crash the whole node-catalog response;
            # Pre-flight below sets ``valid:false`` so the operator still sees
            # the real reason in the catalog payload.
            child_summary = [
                {
                    "id": str(child.get("id") or f"child-{i}"),
                    "name": str(child.get("name") or child.get("id") or f"child-{i}"),
                    "kind": str(child.get("kind") or "qemu"),
                    "image": str(child.get("image") or ""),
                    "cpu": _safe_paired_child_int(child, "cpu", 1),
                    "ram": _safe_paired_child_int(child, "ram", 1024),
                    "ethernet": _safe_paired_ethernet_count(child),
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
            invalid_reason = self.paired_template_invalid_reason(
                paired_key, data, standalone_templates=standalone_templates
            )
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
        return self._compose_extras(
            _extras_schema_for(template_type, template.extras), template.extras
        )

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
        source: BinaryIO,
        image_name: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        image_type = self.get_template(template_type, template_key).type
        return await self.upload_disk_image(image_type, filename, source, image_name, replace_existing=True)

    async def upload_disk_image(
        self,
        image_type: str,
        filename: str,
        source: BinaryIO,
        image_name: str | None = None,
        *,
        replace_existing: bool = False,
    ) -> tuple[dict[str, Any], int]:
        safe_filename = Path(filename.replace("\\", "/")).name
        if safe_filename in {"", ".", ".."} or "\x00" in safe_filename:
            raise TemplateError("Uploaded image filename is invalid.")

        target_image_name = (image_name or Path(safe_filename).stem).strip()
        if target_image_name in {"", ".", ".."} or any(
            char in target_image_name for char in ("/", "\\", "\x00")
        ):
            raise TemplateError("Image name must be a single directory name.")

        try:
            return await run_in_threadpool(
                self._store_image, image_type, target_image_name, safe_filename, source, replace_existing,
            )
        except OSError as exc:
            raise TemplateError("Could not save the image inside the image directory.") from exc

    def _store_image(
        self, image_type: str, image_name: str, filename: str, source: BinaryIO, replace_existing: bool,
    ) -> tuple[dict[str, Any], int]:
        self.images_dir.mkdir(parents=True, exist_ok=True)
        with ExitStack() as opened:
            root = os.open(self.images_dir, _IMAGE_DIR_FLAGS)
            opened.callback(os.close, root)
            with suppress(FileExistsError):
                os.mkdir(image_type, dir_fd=root)
            type_dir = os.open(image_type, _IMAGE_DIR_FLAGS | os.O_NOFOLLOW, dir_fd=root)
            opened.callback(os.close, type_dir)

            # A loose file and an image folder can share the same Library name.
            with os.scandir(type_dir) as entries:
                for entry in entries:
                    if Path(entry.name).stem == image_name and entry.is_file():
                        raise ImageUploadConflict(
                            f"The file {entry.name} already uses the image name {image_name}. "
                            "Choose a different image name or review references and rename the conflicting file on the host."
                        )

            created = False
            try:
                os.mkdir(image_name, dir_fd=type_dir)
                created = True
            except FileExistsError:
                pass
            try:
                directory = os.open(image_name, _IMAGE_DIR_FLAGS | os.O_NOFOLLOW, dir_fd=type_dir)
                opened.callback(os.close, directory)
                with os.scandir(directory) as entries:
                    files = {entry.name for entry in entries if entry.is_file()}
                size = self._publish_image(directory, filename, source, replace_existing)
            except BaseException:
                if created:
                    # It may already have been used by another uploader.
                    self._remove_image_upload_dir(type_dir, image_name)
                raise
        return {
            "image": image_name,
            "files": sorted(files | {filename}),
            "path": str(self.images_dir / image_type / image_name),
        }, size

    @staticmethod
    def _remove_image_upload_dir(directory: int, name: str) -> None:
        try:
            os.rmdir(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        except OSError:
            _logger.warning("Could not remove an image upload directory.", exc_info=True)

    @staticmethod
    def _publish_image(directory: int, filename: str, source: BinaryIO, replace_existing: bool) -> int:
        existing_stat = None
        access_acl = None
        conflict_message = f"{filename} already exists in this image folder. Choose a different image name or file."
        try:
            entry = os.stat(filename, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not replace_existing:
                raise ImageUploadConflict(conflict_message)
            if not S_ISREG(entry.st_mode):
                raise TemplateError("Only regular image files can be replaced.")
            # Preserve the previous requirement for write access to the file itself.
            existing = os.open(filename, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
            try:
                existing_stat = os.fstat(existing)
                if not S_ISREG(existing_stat.st_mode):
                    raise TemplateError("Only regular image files can be replaced.")
                if hasattr(os, "getxattr"):
                    try:
                        access_acl = os.getxattr(existing, _IMAGE_ACCESS_ACL)
                    except OSError as exc:
                        if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                            raise
            finally:
                os.close(existing)

        # A same-filesystem staging directory stays out of the inventory's file list.
        staging = f".upload-{uuid4().hex}"
        os.mkdir(staging, mode=0o700, dir_fd=directory)
        try:
            staging_fd = os.open(staging, _IMAGE_DIR_FLAGS | os.O_NOFOLLOW, dir_fd=directory)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                with os.fdopen(os.open("image", flags, 0o666, dir_fd=staging_fd), "wb") as destination:
                    shutil.copyfileobj(source, destination, length=_IMAGE_COPY_CHUNK_BYTES)
                    size = destination.tell()
                    destination.flush()
                    descriptor = destination.fileno()
                    if existing_stat is not None:
                        staged_stat = os.fstat(descriptor)
                        if (staged_stat.st_uid, staged_stat.st_gid) != (existing_stat.st_uid, existing_stat.st_gid):
                            os.fchown(descriptor, existing_stat.st_uid, existing_stat.st_gid)
                        os.fchmod(descriptor, existing_stat.st_mode & 0o777)
                        if access_acl is not None:
                            os.setxattr(descriptor, _IMAGE_ACCESS_ACL, access_acl)
                        elif hasattr(os, "removexattr"):
                            # The new file may have inherited a directory ACL.
                            try:
                                os.removexattr(descriptor, _IMAGE_ACCESS_ACL)
                            except OSError as exc:
                                if exc.errno not in (errno.ENODATA, errno.ENOTSUP):
                                    raise
                    os.fsync(descriptor)
                if replace_existing:
                    os.replace("image", filename, src_dir_fd=staging_fd, dst_dir_fd=directory)
                else:
                    # Publish a complete file and fail even if another upload won the race.
                    try:
                        os.link("image", filename, src_dir_fd=staging_fd, dst_dir_fd=directory, follow_symlinks=False)
                    except FileExistsError as exc:
                        raise ImageUploadConflict(conflict_message) from exc
                return size
            finally:
                try:
                    os.unlink("image", dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    _logger.warning("Could not discard a staged image file.", exc_info=True)
                finally:
                    os.close(staging_fd)
        finally:
            TemplateService._remove_image_upload_dir(directory, staging)

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

    def _definition_from_payload(
        self,
        payload: dict[str, Any],
        path: Path,
        template_type: str,
        *,
        user: bool,
        inherited_image_aliases: list[str] | None = None,
    ) -> TemplateDefinition:
        source = str(path)
        yaml_extras = payload.get("extras")
        yaml_extras = dict(yaml_extras) if isinstance(yaml_extras, dict) else {}
        for schema_field in _extras_schema_for(template_type, yaml_extras):
            schema_key = schema_field["key"]
            if schema_key in payload and schema_key not in yaml_extras:
                yaml_extras[schema_key] = payload[schema_key]
        interface_naming = payload.get("interface_naming")
        if interface_naming is not None:
            interface_naming = _validate_interface_naming(interface_naming, source=source)
        resolved_console_type = str(
            payload.get("console_type") or self._default_console_type(template_type)
        )
        inherited = list(inherited_image_aliases) if inherited_image_aliases is not None else None
        aliases = (
            _validate_image_aliases(payload["image_aliases"], source=source)
            if "image_aliases" in payload
            else list(inherited or [])
        )
        return TemplateDefinition(
            key=path.stem,
            type=template_type,
            name=str(payload.get("name") or path.stem),
            description=str(payload.get("description") or ""),
            icon_type=str(payload.get("icon_type") or self._default_icon_type(template_type)),
            cpu=int(payload.get("cpu", 1)),
            ram=int(payload.get("ram", 1024)),
            ethernet=int(payload.get("ethernet", 1)),
            console_type=resolved_console_type,
            second_console=_validate_second_console(
                payload.get("second_console"), template_type, resolved_console_type, source=source
            ),
            cpulimit=int(payload.get("cpulimit", 1)),
            extras=yaml_extras,
            raw=payload,
            interface_naming=interface_naming,
            capabilities=_validate_capabilities(
                payload.get("capabilities"), template_type, source=source
            ),
            image_aliases=aliases,
            inherited_image_aliases=inherited,
            user_template_path=path if user else None,
        )

    def _load_standalone_templates(
        self, *, parsed_templates: list[tuple[Path, Any]] | None = None
    ) -> list[TemplateDefinition]:
        if not self.templates_dir.exists() and (
            self.user_templates_dir is None or not self.user_templates_dir.exists()
        ):
            return []

        # Track keys we've already loaded so we can detect shadowing.
        seen_keys_by_type: dict[tuple[str, str], str] = {}  # (type, key) -> source
        builtin_aliases: dict[tuple[str, str], list[str]] = {}

        parsed_by_path = dict(parsed_templates) if parsed_templates is not None else {}
        templates: list[TemplateDefinition] = []
        for template_path, source in self._iter_template_paths():
            if template_path in parsed_by_path:
                payload = parsed_by_path[template_path]
            else:
                payload = yaml.safe_load(template_path.read_text()) or {}
            if not isinstance(payload, dict):
                raise TemplateError(
                    f"Template {template_path.stem!r} must contain a mapping at the YAML document root."
                )
            template_type = _standalone_template_type(payload, template_path)
            if template_type is None:
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
            template = self._definition_from_payload(
                payload,
                template_path,
                template_type,
                user=source == "user",
                inherited_image_aliases=builtin_aliases.get(type_key) if source == "user" else None,
            )
            if source == "builtin":
                builtin_aliases[type_key] = template.image_aliases
            templates.append(template)
        return templates

    def _template_files_fingerprint(self) -> tuple:
        """Identity of every file template loading reads: both template dirs
        plus (path, inode, mtime, ctime, size) of each ``*.yml``/``*.json``
        in them. Any create, edit, rename or delete changes it; writers in
        this codebase also call :func:`invalidate_template_cache`."""
        entries: list[tuple[str, int, int, int, int]] = []
        # Same globs as the loaders: builtin *.yml, user *.yml and *.json.
        for directory, patterns in (
            (self.templates_dir, ("*.yml",)),
            (self.user_templates_dir, ("*.yml", "*.json")),
        ):
            if directory is None or not directory.exists():
                continue
            for pattern in patterns:
                for path in directory.rglob(pattern):
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    entries.append(
                        (str(path), stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
                    )
        return (str(self.templates_dir), str(self.user_templates_dir), tuple(sorted(entries)))

    def _load_templates(
        self,
        *,
        paired_templates: list[tuple[Path, dict[str, Any]]] | None = None,
        parsed_templates: list[tuple[Path, Any]] | None = None,
    ) -> list[TemplateDefinition]:
        """Every template definition (builtin, user and synthetic paired).

        Parsing all template YAML is slow, and lookups like
        :meth:`get_template` run it per call, so a loop over templates was
        quadratic. The default call is cached process-wide and reused until a
        template file changes; callers get a deep copy they may mutate.
        """
        if paired_templates is not None or parsed_templates is not None:
            return self._parse_templates(
                paired_templates=paired_templates, parsed_templates=parsed_templates
            )
        return copy.deepcopy(self._shared_templates())

    def _shared_templates(self) -> list[TemplateDefinition]:
        """The cached template list itself. Read-only: never mutate it or
        hand it out; copy what you return."""
        fingerprint = self._template_files_fingerprint()
        with _TEMPLATE_CACHE_LOCK:
            cached = _TEMPLATE_CACHE.get("entry")
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        templates = self._parse_templates()
        with _TEMPLATE_CACHE_LOCK:
            _TEMPLATE_CACHE["entry"] = (fingerprint, templates)
        return templates

    def _parse_templates(
        self,
        *,
        paired_templates: list[tuple[Path, dict[str, Any]]] | None = None,
        parsed_templates: list[tuple[Path, Any]] | None = None,
    ) -> list[TemplateDefinition]:
        if parsed_templates is None:
            parsed_templates = list(self._read_user_template_payloads())
        if paired_templates is None:
            paired_templates = list(
                self._iter_paired_user_templates(parsed_templates=parsed_templates)
            )
        standalone_templates = self._load_standalone_templates(
            parsed_templates=parsed_templates
        )

        # #206 — append synthetic per-child entries for every paired template so
        # node.template lookups (edit, capability gates, image validation) resolve
        # cleanly. These entries advertise paired_parent so frontends can hide
        # them from the standalone create-flow type-tab picker.
        return [
            *standalone_templates,
            *self._load_synthetic_paired_child_templates(
                standalone_templates=standalone_templates,
                paired_templates=paired_templates,
            ),
        ]

    def _load_synthetic_paired_child_templates(
        self,
        *,
        standalone_templates: list[TemplateDefinition] | None = None,
        paired_templates: list[tuple[Path, dict[str, Any]]] | None = None,
    ) -> list[TemplateDefinition]:
        """Synthesize per-child :class:`TemplateDefinition` entries from paired
        user templates (``USER_TEMPLATES_DIR`` files with ``kind="paired"``).

        Each child becomes a first-class catalog entry keyed
        ``<paired_key>__<child_id>``. Capability/interface_naming blocks on the
        child are validated via the same helpers used for real templates, and a
        broken child is skipped. The child's ``image`` is recorded on the
        synthetic ``raw`` so :meth:`list_images` can surface it (see the override
        in that method).
        """
        if standalone_templates is None or paired_templates is None:
            parsed_templates = list(self._read_user_template_payloads())
        if standalone_templates is None:
            standalone_templates = self._load_standalone_templates(
                parsed_templates=parsed_templates
            )
        if paired_templates is None:
            paired_templates = list(
                self._iter_paired_user_templates(parsed_templates=parsed_templates)
            )
        standalone_keys = {
            (template.type, template.key) for template in standalone_templates
        }

        synthetic: list[TemplateDefinition] = []
        for path, data in paired_templates:
            nodes = data["nodes"]
            paired_key = path.stem
            for index, child in enumerate(nodes):
                if not isinstance(child, dict):
                    continue
                child_id = str(child.get("id") or f"child-{index}")
                child_kind = normalize_paired_child_kind(child.get("kind"))
                synthetic_key = synthetic_paired_child_key(paired_key, child_id)
                collision_reason = _paired_child_collision_reason(
                    paired_key, child_id, child, standalone_keys
                )
                if collision_reason is not None:
                    _logger.warning(
                        "Synthetic paired-child template skipped: %s",
                        collision_reason,
                    )
                    continue
                source = f"{path}::nodes[{index}]"
                # #208 codex-iter3 — paired imports may be malformed (bad
                # scalars, busted interface_naming/capabilities). Don't take
                # down the entire template catalog because of one broken
                # child; the parent paired entry already gets ``valid:false``
                # in ``_build_paired_catalog`` via the shared pre-flight
                # so the operator sees the real reason. Skip-but-log the
                # synthetic child instead.
                try:
                    child_iface = child.get("interface_naming")
                    if child_iface is not None:
                        child_iface = _validate_interface_naming(child_iface, source=source)
                    child_caps = _validate_capabilities(
                        child.get("capabilities"), child_kind, source=source
                    )
                    child_console_type = str(
                        child.get("console") or self._default_console_type(child_kind)
                    )
                    child_second_console = _validate_second_console(
                        child.get("second_console"),
                        child_kind,
                        child_console_type,
                        source=source,
                    )
                    yaml_extras = (
                        dict(child.get("extras") or {})
                        if isinstance(child.get("extras"), dict)
                        else {}
                    )
                    synthetic.append(
                        TemplateDefinition(
                            key=synthetic_key,
                            type=child_kind,
                            name=str(child.get("name") or synthetic_key),
                            description=f"Paired child of {paired_key} (auto-synthesized).",
                            icon_type=str(
                                child.get("icon_type") or self._default_icon_type(child_kind)
                            ),
                            cpu=_safe_paired_child_int(child, "cpu", 1),
                            ram=_safe_paired_child_int(child, "ram", 1024),
                            ethernet=_safe_paired_ethernet_count(child),
                            console_type=child_console_type,
                            second_console=child_second_console,
                            cpulimit=_safe_paired_child_int(child, "cpulimit", 1),
                            extras=yaml_extras,
                            raw=dict(child),
                            interface_naming=child_iface,
                            capabilities=child_caps,
                            paired_parent=paired_key,
                        )
                    )
                except (TemplateError, ValueError, TypeError) as exc:
                    _logger.warning(
                        "Synthetic paired-child template skipped at load time: "
                        "paired=%s child=%s reason=%s",
                        paired_key,
                        child_id,
                        exc,
                    )
                    continue
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

    def _docker_image_catalog(self, *, strict: bool = False) -> dict[str, dict[str, Any]]:
        """Surface only docker images that have been marked for lab use.

        See ``app.services.docker_image_service`` for the marker-tag
        convention. Returning the curated subset here is what filters the
        node-creation modal so guacamole/system images never appear.
        """
        from app.services.docker_image_service import DockerImageService

        images: dict[str, dict[str, Any]] = {}
        service = DockerImageService()
        for image_name in service.list_marked_image_names(strict=strict):
            hints = service.console_hints(image_name, strict=strict)
            images[image_name] = {
                "image": image_name,
                "files": [],
                "path": image_name,
                "source": "docker",
            }
            if hints.vnc_port is not None:
                images[image_name]["vnc_port"] = hints.vnc_port
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
