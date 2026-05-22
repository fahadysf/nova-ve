# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Static regression guard for Bridge-Cloud cross-domain isolation.

After the v3 refactor, the ONLY function permitted to invoke
``host_net.link_master(`` directly is ``host_net.link_master_any`` itself
(inside ``host_net.py``).  All upper-layer call sites
(``node_runtime_service.py``, ``link_service.py``) MUST dispatch via the
driver-aware wrapper ``host_net.link_master_any(...)``.

This test parses every module under ``backend/app/services|routers`` via
the Python AST and fails if any active call to ``host_net.link_master(``
appears outside ``host_net.py``.  Using AST means string literals and
comments that happen to contain the substring "link_master" are
correctly ignored.

See ``.omc/plans/bridge-cloud-feature.md`` §4.4 — "Static guard — CI
grep test".
"""

from __future__ import annotations

import ast
import pathlib


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCAN_ROOTS = (
    REPO_ROOT / "backend" / "app" / "services",
    REPO_ROOT / "backend" / "app" / "routers",
)
ALLOWED_FILE_NAME = "host_net.py"


def _iter_python_files():
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            yield path


def _find_raw_calls(path: pathlib.Path) -> list[tuple[int, str]]:
    """Return a list of ``(line_no, source_line)`` for every literal
    ``host_net.link_master(`` call in ``path``.

    Uses Python's AST so we skip strings and comments — they cannot
    encode an actual function call that the runtime would execute.
    """
    matches: list[tuple[int, str]] = []
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return matches
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return matches
    source_lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # We're looking for: host_net.link_master(...)
        # NOT host_net.link_master_any(...) or .link_master_host(...).
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr != "link_master":
            continue
        # The dotted lhs must be the literal name ``host_net``.
        if not isinstance(func.value, ast.Name) or func.value.id != "host_net":
            continue
        line = source_lines[node.lineno - 1] if node.lineno <= len(source_lines) else ""
        matches.append((node.lineno, line.strip()))
    return matches


def test_no_raw_link_master_calls_outside_host_net() -> None:
    """Fail if any module in ``backend/app/services|routers/`` calls
    ``host_net.link_master(`` directly.  The only legitimate call is
    inside ``host_net.link_master_any`` (which lives in ``host_net.py``).
    """
    offenders: list[str] = []
    for path in _iter_python_files():
        if path.name == ALLOWED_FILE_NAME:
            continue
        for line_no, snippet in _find_raw_calls(path):
            offenders.append(f"{path}:{line_no}: {snippet}")

    assert not offenders, (
        "Raw host_net.link_master( calls outside host_net.py:\n"
        + "\n".join(f"  {o}" for o in offenders)
        + "\nDispatch via host_net.link_master_any(iface, bridge, driver=...) instead."
    )


def test_link_master_any_exists_in_host_net() -> None:
    """Anchor test — fails if the dispatcher itself disappears."""
    from app.services import host_net

    assert hasattr(host_net, "link_master_any")
    assert callable(host_net.link_master_any)
    assert hasattr(host_net, "link_master_host")
    assert callable(host_net.link_master_host)
