from typing import Literal

from pydantic import BaseModel, Field


class PortPosition(BaseModel):
    """Anchor on the perimeter of a node icon where an interface attaches.

    The canvas treats the node as a rectangle. ``side`` selects which edge,
    and ``offset`` is the normalised position along that edge from 0.0 (top
    or left corner) to 1.0 (bottom or right corner).
    """

    side: Literal["top", "right", "bottom", "left"]
    offset: float = Field(ge=0.0, le=1.0)
