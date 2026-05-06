"""Tests for the APP_OWNER resolver (#184)."""

from __future__ import annotations

import grp
import os
import pwd

import pytest

from scripts.import_eveng._app_owner import AppOwnerError, resolve


def _current_user() -> tuple[str, str]:
    pw = pwd.getpwuid(os.getuid())
    gr = grp.getgrgid(pw.pw_gid)
    return pw.pw_name, gr.gr_name


def test_resolves_from_nova_ve_owner_first() -> None:
    user, _ = _current_user()
    owner = resolve(env={"NOVA_VE_OWNER": user, "SUDO_USER": "different-user"})
    assert owner.name == user
    assert owner.source == "NOVA_VE_OWNER"


def test_falls_back_to_sudo_user_when_nova_ve_owner_unset() -> None:
    user, group = _current_user()
    owner = resolve(env={"NOVA_VE_OWNER": "", "SUDO_USER": user})
    assert owner.name == user
    assert owner.group == group
    assert owner.source == "SUDO_USER"


def test_falls_back_to_default_when_neither_env_set() -> None:
    """When neither env var is set, resolver tries the default 'ubuntu'."""
    try:
        pwd.getpwnam("ubuntu")
    except KeyError:
        pytest.skip("'ubuntu' user does not exist on this host")
    owner = resolve(env={})
    assert owner.name == "ubuntu"
    assert owner.source == "default"


def test_raises_loudly_when_resolved_user_does_not_exist() -> None:
    """If even the resolved name fails getpwnam, raise AppOwnerError naming all sites tried."""
    with pytest.raises(AppOwnerError) as exc_info:
        resolve(env={"NOVA_VE_OWNER": "definitely-not-a-real-user-xyz-12345"})
    msg = str(exc_info.value)
    assert "NOVA_VE_OWNER" in msg
    assert "SUDO_USER" in msg
    assert "default 'ubuntu'" in msg


def test_resolve_returns_uid_gid_home() -> None:
    user, group = _current_user()
    owner = resolve(env={"NOVA_VE_OWNER": user})
    assert owner.uid == os.getuid()
    assert owner.gid == os.getgid()
    assert owner.group == group
    assert owner.home  # non-empty
