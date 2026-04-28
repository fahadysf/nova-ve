from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


NetworkType = Literal[
    "linux_bridge",
    "ovs_bridge",
    "nat",
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


class NetworkRuntime(BaseModel):
    """Runtime/host-side state for a network record.

    Persisted under ``networks[i].runtime`` in lab.json. Owned by
    ``network_service`` (bridge name from US-202; IPAM free-list from
    US-204c). The IPAM free-list is intentionally NOT a monotonic
    counter — see plan §US-204c "IPAM data model (free-list, NOT a
    counter)" for why.
    """

    bridge_name: Optional[str] = None
    driver: Optional[str] = None
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


class NetworkUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[NetworkType] = None
    left: Optional[int] = None
    top: Optional[int] = None
    icon: Optional[str] = None
    visibility: Optional[bool] = None
    config: Optional[Dict[str, Any]] = None
