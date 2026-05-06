"""Result-shape schema for adapter ``convert()`` output (#188).

Two kinds of node templates exist:

- **Single-node** (``kind`` in {``qemu``, ``iol``, ``dynamips``, ``docker``}) —
  the shape used by every adapter except paired-VM Juniper templates.
- **Paired-node** (``kind == "paired"``) — introduced by #188 for vMX
  (VCP+VFP) and vQFX (RE+PFE). Adapter emits two nodes plus an internal
  link between them, all in a single template object.

Schema is *additive*: single-node templates remain valid (they are missing
the ``nodes``/``links`` siblings; the discriminator is ``kind``). No
modification to ``backend/app/schemas/lab.py`` is required — the on-disk
lab JSON already uses ``"nodes": {}`` (Dict, see
``backend/app/services/lab_service.py:83``); paired templates instantiate
two nodes via the existing per-node creation path. Per-node fields land in
``extras: Dict[str, Any]`` at ``backend/app/schemas/node.py:63,101,126,148``,
which is already untyped.
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = 1

_SINGLE_NODE_KINDS = frozenset({"qemu", "iol", "dynamips", "docker"})
_PAIRED_KIND = "paired"


class TemplateSchemaError(ValueError):
    """Raised when a template result does not conform to the v1 schema."""


def validate_template(template: dict[str, Any]) -> None:
    """Validate a node template result-shape (single-node OR paired-node).

    Single-node: requires ``schema``, ``kind`` (in the allowed set), and
    ``image``. ``id``, ``name``, and ``extras`` are recommended but not
    enforced here (presence-only contract belongs to the adapter's
    REQUIRED_FIELDS).

    Paired-node: requires ``kind == "paired"`` plus ``nodes`` (list of >=2
    single-node objects) and ``links`` (list, possibly empty if internal
    wiring is implicit).
    """
    schema = template.get("schema")
    if schema != SCHEMA_VERSION:
        raise TemplateSchemaError(
            f"unsupported template schema version: {schema!r} (expected {SCHEMA_VERSION})"
        )

    kind = template.get("kind")
    if kind == _PAIRED_KIND:
        nodes = template.get("nodes")
        if not isinstance(nodes, list) or len(nodes) < 2:
            raise TemplateSchemaError(
                f"paired template must have 'nodes' as a list of >=2 entries; got {nodes!r}"
            )
        for entry in nodes:
            if not isinstance(entry, dict) or "id" not in entry or "kind" not in entry:
                raise TemplateSchemaError(
                    "every entry in paired.nodes must be a dict with 'id' and 'kind'"
                )
        links = template.get("links")
        if not isinstance(links, list):
            raise TemplateSchemaError("paired template must have 'links' as a list (possibly empty)")
    elif kind in _SINGLE_NODE_KINDS:
        if "image" not in template:
            raise TemplateSchemaError("single-node template missing 'image' field")
    else:
        raise TemplateSchemaError(
            f"unknown template kind: {kind!r} (expected paired or one of {sorted(_SINGLE_NODE_KINDS)})"
        )


def is_paired(template: dict[str, Any]) -> bool:
    """Return True iff ``template`` is a paired-node template."""
    return template.get("kind") == _PAIRED_KIND
