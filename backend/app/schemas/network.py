from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator


NetworkType = Literal[
    "linux_bridge",
    "ovs_bridge",
    "nat",
    "nat_cloud",
    "bridge_cloud",
    "cloud",
    "management",
    "pnet0", "pnet1", "pnet2", "pnet3", "pnet4",
    "pnet5", "pnet6", "pnet7", "pnet8", "pnet9",
    "internal", "internal2", "internal3",
    "private", "private2", "private3",
    "nat0",
]


class NetworkConfig(BaseModel):
    """User-supplied network configuration.

    US-204c: the optional ``cidr`` enables L3 IPAM from a free-list of
    used IPs (``runtime.used_ips``). When ``cidr`` is unset the network is
    L2-only. IPv6 CIDRs are rejected at create-network time per the
    deferred-IPv6 §5 plan entry.
    """

    cidr: Optional[str] = None
    gateway: Optional[str] = None
    dhcp: bool = False
    dhcp_start: Optional[str] = None
    dhcp_end: Optional[str] = None
    egress_interface: Optional[str] = None
    # Bridge-Cloud: name of the host-owned bridge (``br-eth*``) this lab
    # network attaches to.  Required when ``type == "bridge_cloud"``;
    # validated at the service layer against ``^br-eth[0-9]+$`` and a
    # ``host_net.bridge_exists`` probe.
    host_bridge: Optional[str] = None


class NetworkRuntime(BaseModel):
    """Runtime/host-side state for a network record.

    Persisted under ``networks[i].runtime`` in lab.json. Owned by
    ``network_service`` (bridge name from US-202; IPAM free-list from
    US-204c; ``driver``/``created_at`` reconciliation metadata from
    US-401). The IPAM free-list is intentionally NOT a monotonic
    counter — see plan §US-204c "IPAM data model (free-list, NOT a
    counter)" for why.
    """

    bridge_name: Optional[str] = None
    # US-401: ``driver`` records which provisioning backend created the
    # bridge (currently always ``"linux_bridge"``). ``created_at`` is the
    # ISO-8601 UTC timestamp of provisioning. Both feed reconciliation in
    # US-402.
    driver: Optional[str] = None
    created_at: Optional[datetime] = None
    used_ips: List[str] = Field(default_factory=list)
    # First host offset usable for allocation (skips network address and
    # the conventional .1 gateway reservation). Operators may bump this
    # if they need additional reserved low addresses.
    first_offset: int = 2


class NetworkBase(BaseModel):
    """Persisted network record (v2).

    The ``count`` field has been removed from the persisted shape — it must be
    derived from ``links[]`` on read. ``visibility`` is a boolean; ``implicit``
    flags networks that the loader synthesises (e.g. node-to-node link bridges).
    """

    id: int
    name: str = Field(default="Net")
    type: NetworkType = "linux_bridge"
    left: int = 0
    top: int = 0
    icon: str = Field(default="01-Cloud-Default.svg")
    width: int = 0
    style: str = Field(default="Solid")
    linkstyle: str = Field(default="Straight")
    color: str = Field(default="")
    label: str = Field(default="")
    visibility: bool = True
    implicit: bool = False
    smart: int = -1
    config: Dict[str, Any] = Field(default_factory=dict)
    runtime: Dict[str, Any] = Field(default_factory=dict)


class NetworkRead(NetworkBase):
    """Network with the live, links-derived ``count`` exposed to the API."""

    count: int = 0

    class Config:
        from_attributes = True


class NetworkCreate(BaseModel):
    name: str
    type: NetworkType = "linux_bridge"
    left: int = 0
    top: int = 0
    config: Optional[NetworkConfig] = None

    @model_validator(mode="after")
    def _require_host_bridge_for_bridge_cloud(self) -> "NetworkCreate":
        """Bridge-Cloud payloads MUST carry ``config.host_bridge``.

        AC7 in .omc/plans/bridge-cloud-feature.md §3: Pydantic accepts a
        ``bridge_cloud`` payload only when ``config.host_bridge`` is a
        non-empty string.  Without this, the malformed payload reaches
        the service layer and is rejected with a less helpful 400.
        """
        if self.type == "bridge_cloud":
            host_bridge = (
                self.config.host_bridge if self.config is not None else None
            )
            if not isinstance(host_bridge, str) or not host_bridge.strip():
                raise ValueError(
                    "config.host_bridge is required when type == 'bridge_cloud'"
                )
        return self


class NetworkUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[NetworkType] = None
    left: Optional[int] = None
    top: Optional[int] = None
    icon: Optional[str] = None
    visibility: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
