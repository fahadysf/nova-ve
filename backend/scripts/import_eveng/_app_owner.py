"""Resolve the canonical app owner for the EVE-NG importer (#184).

Mirrors the resolution in ``install.sh:76``::

    APP_OWNER="${NOVA_VE_OWNER:-${SUDO_USER:-ubuntu}}"

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

    Resolution order matches ``install.sh:76``:

    1. ``NOVA_VE_OWNER`` if set and non-empty.
    2. ``SUDO_USER`` if set and non-empty.
    3. Hard default: ``"ubuntu"``.

    The resolved name is then validated via :func:`pwd.getpwnam`; if the user
    does not exist on the host, :class:`AppOwnerError` is raised with a message
    naming all three resolution sites that were tried.
    """
    env_map = env if env is not None else os.environ

    nova_owner = (env_map.get("NOVA_VE_OWNER") or "").strip()
    sudo_user = (env_map.get("SUDO_USER") or "").strip()

    if nova_owner:
        candidate, source = nova_owner, "NOVA_VE_OWNER"
    elif sudo_user:
        candidate, source = sudo_user, "SUDO_USER"
    else:
        candidate, source = "ubuntu", "default"

    try:
        pw = pwd.getpwnam(candidate)
    except KeyError as exc:
        raise AppOwnerError(
            f"APP_OWNER resolution failed: user {candidate!r} (from {source}) "
            f"does not exist on this host. Resolution sites tried in order: "
            f"NOVA_VE_OWNER (env={nova_owner!r}), SUDO_USER (env={sudo_user!r}), "
            f"default 'ubuntu'."
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
