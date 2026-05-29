"""Resolve the canonical service owner for the EVE-NG importer (#184, #228).

Mirrors the service-account resolution used by ``install.sh``::

    APP_OWNER="${NOVA_VE_SERVICE_USER:-${NOVA_VE_OWNER:-nova-ve}}"

The user must exist on the host (``id <user>`` succeeds); otherwise the helper
raises :class:`AppOwnerError` and the CLI fails loudly. Lines 77-82 of
``install.sh`` derive ``APP_GROUP`` (primary group) and ``APP_HOME`` from the
resolved user; we mirror those derivations here using :mod:`pwd` and :mod:`grp`.
"""

from __future__ import annotations

import grp
import os
import pwd
from dataclasses import dataclass


class AppOwnerError(RuntimeError):
    """Raised when ``APP_OWNER`` cannot be resolved or does not exist on the host."""


@dataclass(frozen=True)
class AppOwner:
    """The fully resolved owner identity for chown operations."""

    name: str
    uid: int
    group: str
    gid: int
    home: str
    source: str  # which env var (or "default") supplied the name


def resolve(env: dict[str, str] | None = None) -> AppOwner:
    """Resolve ``APP_OWNER`` from the process environment (or an override map).

    Resolution order matches the installer after #228:

    1. ``NOVA_VE_SERVICE_USER`` if set and non-empty.
    2. ``NOVA_VE_OWNER`` if set and non-empty (compatibility alias).
    3. Hard default: ``"nova-ve"``.

    The resolved name is then validated via :func:`pwd.getpwnam`; if the user
    does not exist on the host, :class:`AppOwnerError` is raised with a message
    naming all three resolution sites that were tried.
    """
    env_map = env if env is not None else os.environ

    service_user = (env_map.get("NOVA_VE_SERVICE_USER") or "").strip()
    nova_owner = (env_map.get("NOVA_VE_OWNER") or "").strip()

    if service_user:
        candidate, source = service_user, "NOVA_VE_SERVICE_USER"
    elif nova_owner:
        candidate, source = nova_owner, "NOVA_VE_OWNER"
    else:
        candidate, source = "nova-ve", "default"

    try:
        pw = pwd.getpwnam(candidate)
    except KeyError as exc:
        raise AppOwnerError(
            f"APP_OWNER resolution failed: user {candidate!r} (from {source}) "
            f"does not exist on this host. Resolution sites tried in order: "
            f"NOVA_VE_SERVICE_USER (env={service_user!r}), "
            f"NOVA_VE_OWNER (env={nova_owner!r}), default 'nova-ve'."
        ) from exc

    try:
        gr = grp.getgrgid(pw.pw_gid)
        group_name = gr.gr_name
    except KeyError:
        # The user's primary GID does not have a group entry — extremely rare
        # but handle by falling back to the gid as a string. install.sh:81's
        # `id -gn` would also fail in this case, so failing loudly here matches
        # the bash wrapper's behaviour.
        raise AppOwnerError(
            f"APP_OWNER {candidate!r} primary GID {pw.pw_gid} has no group entry."
        )

    return AppOwner(
        name=pw.pw_name,
        uid=pw.pw_uid,
        group=group_name,
        gid=pw.pw_gid,
        home=pw.pw_dir,
        source=source,
    )
