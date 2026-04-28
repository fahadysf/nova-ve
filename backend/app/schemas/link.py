from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag

LinkStyleOverride = Optional[Literal["orthogonal", "bezier", "straight"]]


class NodeEndpoint(BaseModel):
    """Link endpoint that terminates on a specific node interface."""

    node_id: int
    interface_index: int = Field(ge=0)


class NetworkEndpoint(BaseModel):
    """Link endpoint that terminates on a typed network."""

    network_id: int


def _endpoint_discriminator(value) -> str:
    if isinstance(value, dict):
        if "network_id" in value:
            return "network"
        if "node_id" in value:
            return "node"
    else:
        if hasattr(value, "network_id"):
            return "network"
        if hasattr(value, "node_id"):
            return "node"
    raise ValueError("Link endpoint must contain either 'node_id' or 'network_id'.")


LinkEndpoint = Annotated[
    Union[
        Annotated[NodeEndpoint, Tag("node")],
        Annotated[NetworkEndpoint, Tag("network")],
    ],
    Discriminator(_endpoint_discriminator),
]


class LinkMetrics(BaseModel):
    delay_ms: int = 0
    loss_pct: int = 0
    bandwidth_kbps: int = 0
    jitter_ms: int = 0


class LinkRuntime(BaseModel):
    """US-204b: per-link runtime state stamped at hot-attach time.

    ``attach_generation`` is the canonical generation value for THIS
    link's specific attach. Stamped on the link record at the moment
    ``attach_*_interface`` succeeds (atomic with the ``used_ips`` write
    under ``lab_lock``). ``link_service.delete_link`` reads this value
    and passes it as ``expected_generation=N`` to ``detach_*_interface``
    so a stale rollback never undoes a newer attach.
    """

    attach_generation: int = 0


class Link(BaseModel):
    """Topology link between two endpoints in the v2 schema."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    from_: LinkEndpoint = Field(alias="from")
    to: LinkEndpoint
    style_override: LinkStyleOverride = None
    label: str = ""
    color: str = ""
    width: str = "1"
    metrics: LinkMetrics = Field(default_factory=LinkMetrics)
    runtime: LinkRuntime = Field(default_factory=LinkRuntime)
