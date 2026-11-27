# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
import shutil
import stat
import subprocess


def test_publish_frontend_normalizes_public_modes_without_changing_build(tmp_path):
    repo = tmp_path / "checkout"
    frontend = repo / "frontend"
    frontend.mkdir(parents=True)
    scripts = repo / "deploy/scripts"
    scripts.mkdir(parents=True)
    origin = Path(__file__).resolve().parents[2] / "deploy/scripts/build-frontend.sh"
    script = scripts / origin.name
    shutil.copyfile(origin, script)

    private = repo / "private-fixture"
    private.write_text("fixture")
    private.chmod(0o600)
    npm = tmp_path / "npm"
    npm.write_text('''#!/bin/sh
set -eu
if [ "$1" = run ] && [ "$2" = build ]; then
    mkdir -p build/_app/immutable
    printf 'fixture\n' > build/index.html
    printf 'fixture\n' > build/_app/immutable/test.js
    ln -s "$PWD/../private-fixture" build/external-reference
fi
''')
    npm.chmod(0o755)

    published = tmp_path / "published"
    result = subprocess.run(
        ["bash", "-c", 'umask 077; exec bash "$1"', "fixture", str(script)],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}",
             "NOVA_VE_FRONTEND_ROOT": str(published)},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(published.stat().st_mode) == 0o755
    assert stat.S_IMODE((published / "_app/immutable").stat().st_mode) == 0o755
    assert stat.S_IMODE((published / "index.html").stat().st_mode) == 0o644
    assert stat.S_IMODE((published / "_app/immutable/test.js").stat().st_mode) == 0o644
    assert stat.S_IMODE((frontend / "build/index.html").stat().st_mode) == 0o600
    assert (published / "external-reference").is_symlink()
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
