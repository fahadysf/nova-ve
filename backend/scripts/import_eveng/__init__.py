"""nova-ve EVE-NG / UNetLab / PNETLab importer (umbrella #182).

This package walks an EVE-NG install tree (default ``/opt/unetlab``), copies the
images and templates into the nova-ve layout, and writes a manifest. See the
GH issues #183-#190 for the per-PR contract; #183 ships only the CLI scaffold,
manifest schema, sha256 helper, and idempotency primitives.

Importable as ``from scripts.import_eveng import main`` (no top-level side
effects). The bash wrapper at ``deploy/scripts/import-eveng-templates.sh`` is
the operator-facing entry point on a deployed host.
"""

from .cli import main

__all__ = ["main"]
