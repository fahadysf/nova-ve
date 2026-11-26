"""Exercise the installer checkout policy with local Git repositories only."""

import os
import re
import subprocess
from pathlib import Path

import pytest


INSTALLER = Path(__file__).resolve().parents[2] / "install.sh"
PROVISIONER = INSTALLER.parent / "deploy" / "scripts" / "provision-ubuntu-2604.sh"


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def commit(repo, content):
    (repo / "release.txt").write_text(content)
    git(repo, "add", "release.txt")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "-m", "Release fixture")
    return git(repo, "rev-parse", "HEAD")


def prepare(tmp_path, source, app, ref="main"):
    script = INSTALLER.read_text()
    function = re.search(r"^prepare_release_checkout\(\) \{\n.*?^\}", script, re.M | re.S)
    assert function is not None, "The installer has no isolated release checkout step"
    function_source = function.group()
    shell = r"""
set -eu
APP_OWNER=fixture
APP_GROUP=fixture
REPO_URL=$1
REPO_DIR=$2
REPO_REF=$3
TRUSTED_SOURCE=$4
log() { :; }
chown() { :; }
install() { mkdir -p -- "${@: -1}"; }
sudo() {
  test "$1" = -u && test "$2" = "$APP_OWNER" && test "$3" = -H || exit 99
  shift 3
  "$@"
}
""" + function_source + '\nprepare_release_checkout "$TRUSTED_SOURCE"\n'
    return subprocess.run(
        ["bash", "-c", shell, "installer-test", str(source), str(app), ref,
         str(tmp_path / "trusted")],
        text=True, capture_output=True, timeout=45,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


@pytest.fixture
def upstream(tmp_path):
    source = tmp_path / "upstream"
    source.mkdir()
    git(source, "init", "--initial-branch=main")
    staged_provisioner = source / PROVISIONER.relative_to(INSTALLER.parent)
    staged_provisioner.parent.mkdir(parents=True)
    staged_provisioner.write_text(PROVISIONER.read_text())
    staged_provisioner.chmod(0o755)
    git(source, "add", "deploy")
    return source


@pytest.mark.parametrize("ref", ["main", "release-tag", "commit-sha"])
def test_fresh_application_matches_trusted_release(tmp_path, upstream, ref):
    revision = commit(upstream, "first release")
    if ref == "release-tag":
        git(upstream, "tag", ref)
        commit(upstream, "later branch revision")
    if ref == "commit-sha":
        ref = revision
        commit(upstream, "later branch revision")
    app = tmp_path / "app"

    result = prepare(tmp_path, upstream, app, ref)

    assert result.returncode == 0, result.stderr
    assert git(app, "rev-parse", "HEAD") == revision
    assert git(tmp_path / "trusted", "rev-parse", "HEAD") == revision
    (app / "release.txt").write_text("application-owned edit")
    assert (tmp_path / "trusted" / "release.txt").read_text() == "first release"


def test_upgrade_uses_the_configured_source(tmp_path, upstream):
    commit(upstream, "first release")
    app = tmp_path / "app"
    git(tmp_path, "clone", str(upstream), str(app))
    git(app, "remote", "set-url", "origin", str(tmp_path / "nonexistent"))
    revision = commit(upstream, "next release")

    result = prepare(tmp_path, upstream, app)

    assert result.returncode == 0, result.stderr
    assert git(app, "rev-parse", "HEAD") == revision
    assert git(tmp_path / "trusted", "rev-parse", "HEAD") == revision


def test_retry_after_preparing_an_empty_checkout(tmp_path, upstream):
    revision = commit(upstream, "release")
    app = tmp_path / "app"
    app.mkdir()
    git(app, "init")
    git(app, "remote", "add", "origin", str(upstream))

    result = prepare(tmp_path, upstream, app)

    assert result.returncode == 0, result.stderr
    assert git(app, "rev-parse", "HEAD") == revision


@pytest.mark.parametrize("local_change", ["dirty", "diverged"])
def test_upgrade_preserves_checkout_when_it_cannot_fast_forward(tmp_path, upstream, local_change):
    commit(upstream, "first release")
    app = tmp_path / "app"
    git(tmp_path, "clone", str(upstream), str(app))
    commit(upstream, "next release")
    if local_change == "diverged":
        revision = commit(app, "local change")
    else:
        revision = git(app, "rev-parse", "HEAD")
        (app / "release.txt").write_text("local change")

    result = prepare(tmp_path, upstream, app)

    assert result.returncode != 0
    if local_change == "diverged":
        assert "cannot fast-forward" in result.stderr
    else:
        assert "set aside local changes" in result.stderr
    assert git(app, "rev-parse", "HEAD") == revision
    assert (app / "release.txt").read_text() == "local change"


def test_incompatible_provisioner_is_rejected_before_checkout_update(tmp_path, upstream):
    revision = commit(upstream, "current release")
    app = tmp_path / "app"
    git(tmp_path, "clone", str(upstream), str(app))
    provisioner = upstream / PROVISIONER.relative_to(INSTALLER.parent)
    provisioner.write_text("#!/usr/bin/env bash\n")
    git(upstream, "add", "deploy")
    commit(upstream, "incompatible release")

    result = prepare(tmp_path, upstream, app)

    assert result.returncode != 0
    assert "does not support separate provisioning" in result.stderr
    assert git(app, "rev-parse", "HEAD") == revision
    assert (app / "release.txt").read_text() == "current release"


def test_staged_provisioner_gets_trusted_cwd_and_separate_application(tmp_path):
    stage = tmp_path / "source"
    stage.mkdir()
    app = tmp_path / "app"
    app.mkdir()
    child = stage / "fixture.sh"
    child.write_text(r'''
printf '%s\n' "$PWD" "$NOVA_VE_REPO_DIR" "$NOVA_VE_RELEASE_COMMIT" \
  "${BASH_ENV-unset}" "${ENV-unset}" "${PYTHONPATH-unset}" "${PYTHONHOME-unset}"
''')
    function = re.search(r"^run_staged_provisioner\(\) \(\n.*?^\)", INSTALLER.read_text(), re.M | re.S)
    assert function is not None
    shell = r"""
set -eu
TRUSTED_SOURCE=$1
REPO_DIR=$2
PROVISIONER=$3
RELEASE_COMMIT=fixture-revision
APP_OWNER=fixture-user
REBOOT_SENTINEL=/unused
export BASH_ENV=/unused ENV=/unused PYTHONPATH=$REPO_DIR PYTHONHOME=$REPO_DIR
cd "$REPO_DIR"
""" + function.group() + "\nrun_staged_provisioner\n"
    result = subprocess.run(
        ["bash", "-c", shell, "installer-test", str(stage), str(app), str(child)],
        text=True, capture_output=True, check=True,
    )
    assert result.stdout.splitlines() == [
        str(stage.resolve()), str(app), "fixture-revision", "unset", "unset", "unset", "unset",
    ]


def test_provisioner_backend_commands_use_service_identity():
    script = PROVISIONER.read_text()
    subprocess.run(["bash", "-n"], text=True, input=script, check=True)
    function = re.search(r"^run_app_backend\(\) \{\n.*?^\}", script, re.M | re.S)
    assert function is not None
    shell = r"""
set -eu
APP_OWNER=fixture-user
APP_GROUP=fixture-group
APP_ROOT=/application-checkout
ENV_FILE=/protected/backend.env
run() { printf '%s\n' "$@"; }
""" + function.group() + '\nrun_app_backend /application-checkout/backend/.venv/bin/python -m alembic upgrade head\n'
    result = subprocess.run(["bash", "-c", shell], text=True, capture_output=True, check=True)
    command = result.stdout.splitlines()
    assert command[0] == "systemd-run"
    assert "--uid=fixture-user" in command
    assert "--gid=fixture-group" in command
    assert "--working-directory=/application-checkout/backend" in command
    assert "--property=EnvironmentFile=/protected/backend.env" in command
    assert "--expand-environment=no" in command
    assert command[command.index("--") + 1 :] == [
        "/application-checkout/backend/.venv/bin/python", "-m", "alembic", "upgrade", "head",
    ]


@pytest.mark.parametrize(
    "scenario,error",
    [
        ("trusted", ""),
        ("unowned", "non-root ancestor"),
        ("writable", "writable or non-root ancestor"),
        ("untrusted-asset", "Provisioning inputs must be owned"),
    ],
)
def test_provisioning_tree_owner_and_mode_checks(scenario, error):
    function = re.search(
        r"^validate_provisioning_tree\(\) \{\n.*?^\}", PROVISIONER.read_text(), re.M | re.S,
    )
    assert function is not None
    shell = r"""
set -eu
SCENARIO=$1
REPO_ROOT=/tmp/nova-ve-release-fixture/source
stat() {
  case "$SCENARIO:${@: -1}" in
    unowned:/tmp/nova-ve-release-fixture/source) echo '1000 755' ;;
    writable:/tmp/nova-ve-release-fixture) echo '0 775' ;;
    *:/tmp) echo '0 1777' ;;
    *) echo '0 755' ;;
  esac
}
find() {
  if [[ "$SCENARIO" == untrusted-asset ]]; then
    printf '%s\n' "$REPO_ROOT/deploy/fixture"
  fi
}
""" + function.group() + "\nvalidate_provisioning_tree\n"
    result = subprocess.run(
        ["bash", "-c", shell, "installer-test", scenario], text=True, capture_output=True,
    )
    if error:
        assert result.returncode != 0 and error in result.stderr
    else:
        assert result.returncode == 0, result.stderr
