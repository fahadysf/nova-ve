from typing import Any, Dict, Optional, Literal, List
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.port import PortPosition


ConsoleMode = Literal["telnet", "vnc", "rdp"]

SUPPORTED_CONSOLE_MODES_BY_TYPE: dict[str, set[str]] = {
    "qemu": {"telnet", "vnc"},
    "docker": {"telnet", "vnc", "rdp"},
    "iol": {"telnet"},
    "dynamips": {"telnet"},
}


def validate_console_mode_for_type(node_type: str, console: str) -> None:
    allowed = SUPPORTED_CONSOLE_MODES_BY_TYPE.get(node_type, {"telnet"})
    if console not in allowed:
        allowed_list = ", ".join(sorted(allowed))
        raise ValueError(
            f"Console mode {console!r} is not supported for {node_type} nodes; "
            f"supported modes: {allowed_list}."
        )


class NodeInterfaceRuntime(BaseModel):
    """US-204b: per-interface runtime state used as the freshness oracle
    for stale-rollback detection.

    ``current_attach_generation`` is bumped on every successful attach to
    this interface. ``link_service.delete_link`` compares the link's
    captured ``attach_generation`` with the node interface's
    ``current_attach_generation``; if equal the link's attach is still
    authoritative and detach proceeds, if different a newer attach has
    happened and the detach is logged + no-ops.
    """

    current_attach_generation: int = 0


class NodeInterface(BaseModel):
    """v2 node interface entry.

    The ``network_id`` from v1 is gone — that relationship now lives in the
    top-level ``links[]`` array. ``planned_mac`` is populated by the MAC
    registry (Wave 1 work); for now it can be ``None``.
    """

    index: int = Field(ge=0)
    name: str
    planned_mac: Optional[str] = None
    port_position: Optional[PortPosition] = None
    runtime: NodeInterfaceRuntime = Field(default_factory=NodeInterfaceRuntime)


class NodeBase(BaseModel):
    id: int
    name: str = Field(default="Node")
    type: Literal["qemu", "docker", "iol", "dynamips"] = "qemu"
    template: str = Field(default="")
    image: str = Field(default="")
    # ``serial`` is a paired-template-only console type (Juniper VCP/VFP).
    # Single-node create flows still default to ``telnet``; the value only
    # appears on lab.json entries that came from /from-paired-template.
    console: Literal["telnet", "vnc", "rdp", "serial"] = "telnet"
    status: Literal[0, 2] = 0
    delay: int = 0
    cpu: int = 1
    ram: int = 1024
    ethernet: int = 1
    cpulimit: int = 1
    uuid: Optional[UUID] = None
    firstmac: Optional[str] = None
    left: int = 0
    top: int = 0
    icon: str = Field(default="Router.png")
    width: str = Field(default="0")
    config: bool = False
    config_list: List[str] = Field(default_factory=list)
    sat: int = 0
    computed_sat: int = 0
    interfaces: List[NodeInterface] = Field(default_factory=list)
    extras: Dict[str, Any] = Field(default_factory=dict)
    interface_naming_scheme: Optional[str] = None
    # US-301: pinned QEMU machine type. ``None`` means "inherit from
    # ``template.capabilities.machine``"; pre-Wave-7 nodes are stamped
    # with ``'pc'`` by ``scripts/migrate_runtime_network.py`` so the q35
    # default never silently changes their PCI topology.
    machine_override: Optional[Literal["pc", "q35"]] = None

    @model_validator(mode='after')
    def _check_iface_naming_scheme(self):
        if self.type == 'docker' and self.interface_naming_scheme not in (None, 'eth{n}'):
            raise ValueError("Docker nodes use eth{n} naming; field is system-fixed")
        return self


class NodeRead(NodeBase):
    url: Optional[str] = Field(default="")
    cpu_usage: Optional[int] = None
    ram_usage: Optional[int] = None
    disk_usage: Optional[str] = None

    class Config:
        from_attributes = True


class NodeCreate(BaseModel):
    name: str
    type: Literal["qemu", "docker", "iol", "dynamips"] = "qemu"
    template: str
    image: str
    console: ConsoleMode = "telnet"
    cpu: int = 1
    ram: int = 1024
    ethernet: int = 1
    delay: int = 0
    left: int = 0
    top: int = 0
    icon: Optional[str] = None
    extras: Dict[str, Any] = Field(default_factory=dict)
    interface_naming_scheme: Optional[str] = None

    @model_validator(mode='after')
    def _check_iface_naming_scheme(self):
        if self.type == 'docker' and self.interface_naming_scheme not in (None, 'eth{n}'):
            raise ValueError("Docker nodes use eth{n} naming; field is system-fixed")
        validate_console_mode_for_type(self.type, self.console)
        return self


class NodeFromTemplate(BaseModel):
    """Request body for ``POST /api/labs/{lab_path:path}/nodes/from-template`` (#185).

    Operator selects a template from the existing
    ``GET /api/list/templates/{template_type}`` listing and asks the server to
    instantiate a node with the template's defaults pre-applied. The server
    rejects paired-node templates (kind="paired") with HTTP 400 until the
    multi-node picker UI follow-up lands (#185 + #188 + R-OOB-3).
    """

    template_type: Literal["qemu", "docker", "iol", "dynamips"]
    template_key: str
    name: str
    image: str
    console: Optional[ConsoleMode] = None
    cpu: Optional[int] = None
    ram: Optional[int] = None
    ethernet: Optional[int] = None
    left: int = 0
    top: int = 0
    extras: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode='after')
    def _check_console_mode(self):
        if self.console is not None:
            validate_console_mode_for_type(self.template_type, self.console)
        return self


class NodeFromPairedTemplate(BaseModel):
    """Request body for ``POST /api/labs/{lab_path:path}/nodes/from-paired-template`` (#202).

    Operator selects a paired-node template (``kind="paired"``) from the catalog
    (which exposes ``paired_templates:[...]`` alongside the regular templates list)
    and asks the server to instantiate ALL child nodes plus the auto-links between
    them in a single atomic call. On partial failure the server rolls back lab.json
    so no orphan nodes are left behind.

    The base position is the drop-point on the canvas; child nodes are laid out in
    a row with a 180-px horizontal offset relative to that anchor. ``name_overrides``
    maps the paired template's child id (e.g. ``"vcp"``, ``"vfp"``) to a user-chosen
    node name; missing entries use ``f"{template_key}-{child_id}"``.
    """

    template_key: str
    base_left: int = 0
    base_top: int = 0
    name_overrides: Dict[str, str] = Field(default_factory=dict)


class NodeBatchCreate(BaseModel):
    name_prefix: str = Field(default="Node")
    count: int = Field(default=1, ge=1, le=24)
    placement: Literal["grid", "row"] = "grid"
    type: Literal["qemu", "docker", "iol", "dynamips"] = "qemu"
    template: str
    image: str
    console: ConsoleMode = "telnet"
    cpu: int = 1
    ram: int = 1024
    ethernet: int = 1
    delay: int = 0
    left: int = 0
    top: int = 0
    icon: Optional[str] = None
    extras: Dict[str, Any] = Field(default_factory=dict)
    interface_naming_scheme: Optional[str] = None

    @model_validator(mode='after')
    def _check_iface_naming_scheme(self):
        if self.type == 'docker' and self.interface_naming_scheme not in (None, 'eth{n}'):
            raise ValueError("Docker nodes use eth{n} naming; field is system-fixed")
        validate_console_mode_for_type(self.type, self.console)
        return self


class NodeUpdate(BaseModel):
    name: Optional[str] = None
    cpu: Optional[int] = None
    ram: Optional[int] = None
    ethernet: Optional[int] = None
    image: Optional[str] = None
    # ``serial`` accepted so paired-template children (Juniper VCP/VFP) can
    # round-trip through the edit modal — the modal always re-submits
    # ``console`` and the existing value is whatever the paired template
    # declared. Without ``serial`` here, saving an unchanged paired child
    # would 422 on schema validation (#206 codex-iter3).
    console: Optional[Literal["telnet", "vnc", "rdp", "serial"]] = None
    delay: Optional[int] = None
    left: Optional[int] = None
    top: Optional[int] = None
    icon: Optional[str] = None
    config: Optional[str] = None
    extras: Optional[Dict[str, Any]] = None
    interface_naming_scheme: Optional[str] = None
