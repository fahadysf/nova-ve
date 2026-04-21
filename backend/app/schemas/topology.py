from pydantic import BaseModel, Field
from typing import Literal


class TopologyLink(BaseModel):
    type: Literal["ethernet"] = "ethernet"
    source: str
    source_node_name: str = ""
    source_type: Literal["node", "network"] = "node"
    source_label: str = ""
    source_interfaceId: int = 0
    source_suspend: int = 0
    destination: str
    destination_type: Literal["node", "network"] = "network"
    destination_node_name: str = ""
    destination_label: str = ""
    destination_interfaceId: int | str = "network"
    destination_suspend: int = 0
    network_id: int = 0
    style: str = ""
    linkstyle: str = ""
    label: str = ""
    labelpos: str = "0.5"
    color: str = ""
    stub: str = "0"
    width: str = "1"
    curviness: str = "10"
    beziercurviness: str = "150"
    round: str = "0"
    midpoint: str = "0.5"
    srcpos: str = "0.15"
    dstpos: str = "0.85"
    source_delay: int = 0
    source_loss: int = 0
    source_bandwidth: int = 0
    source_jitter: int = 0
    destination_delay: int = 0
    destination_loss: int = 0
    destination_bandwidth: int = 0
    destination_jitter: int = 0
