# SPDX-License-Identifier: Apache-2.0

"""Refuse startup while pre-#334 dnsmasq servers remain on the old layout."""

from __future__ import annotations

import errno
import os
from pathlib import Path

from app.services import host_net
from app.services.network_info_service import PROC_ROOT


def require_secure_dnsmasq_layout() -> None:
    """Check raw process arguments without trusting the legacy directory.

    Match the old ``nat-cloud/<bridge>/dnsmasq.conf`` path suffix in argv.
    Do not resolve that path: the service-owned directory can be replaced.
    A process that exits during the scan is harmless; other read failures
    make the result incomplete and must prevent startup.
    """
    if host_net.sandbox_mode():
        return
    try:
        capability = host_net._invoke_helper("dnsmasq-layout-version")
    except (OSError, host_net.HostNetError) as exc:
        raise RuntimeError("The installed network helper lacks the protected DHCP layout; startup refused") from exc
    if (capability.stdout or "").strip() != "root-owned-dnsmasq-v1":
        raise RuntimeError("The installed network helper lacks the protected DHCP layout; startup refused")
    try:
        entries = list(PROC_ROOT.iterdir())
        legacy = []
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                argv = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ESRCH):
                    continue
                raise
            if not argv or os.path.basename(argv[0]) != b"dnsmasq":
                continue
            for arg in argv[1:]:
                if not arg.startswith(b"--conf-file="):
                    continue
                path = Path(os.fsdecode(arg[len(b"--conf-file="):]))
                parts = path.parts
                if (len(parts) >= 3
                    and parts[-3] == "nat-cloud" and parts[-1] == "dnsmasq.conf"
                    and host_net.RE_BRIDGE_NAME.fullmatch(parts[-2])):
                    legacy.append(entry.name)
                    break
    except OSError as exc:
        raise RuntimeError("Cannot inspect all processes for legacy DHCP servers; startup refused") from exc
    if legacy:
        raise RuntimeError(
            "Legacy DHCP servers are still running; stop or migrate them with the new helper "
            "before starting the backend (see the upgrade documentation)"
        )
