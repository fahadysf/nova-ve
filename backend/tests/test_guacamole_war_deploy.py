# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "deploy/scripts/run-guacamole.sh"


@pytest.fixture
def guacamole_deploy(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    def executable(name, script):
        path = bin_dir / name
        path.write_text(script)
        path.chmod(0o755)

    executable("docker", f"#!{sys.executable}\n" + '''
import os
import shutil
import stat
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["GUAC_FIXTURE_STATE"])
war = state / "fixture.war"
if args[0] == "compose" and "ps" in args:
    print(args[-1])
elif args[0] == "compose" and "port" in args:
    print(os.environ["GUAC_FIXTURE_PUBLISHED"])
elif args[0] == "compose" and "restart" in args:
    (state / "restart").touch()
elif args[0] == "exec" and "psql" in args:
    print("guacamole_entity")
elif args[0] == "exec" and "test" in args:
    raise SystemExit(0 if war.stat().st_mode & stat.S_IROTH else 1)
elif args[0] == "exec" and "chmod" in args:
    war.chmod(int(args[-2], 8))
elif args[0] == "cp" and args[1].startswith("guacamole:"):
    shutil.copyfile(war, args[2])
elif args[0] == "cp" and args[1].endswith("guacamole-patched.war"):
    source = Path(args[1])
    (state / "private-dir-mode").write_text(oct(stat.S_IMODE(source.parent.stat().st_mode)))
    shutil.copy(source, war)
''')
    executable("curl", '#!/bin/sh\nfor final in "$@"; do :; done\nprintf "%s\\n" "$final" >> "$GUAC_FIXTURE_STATE/probes"\nexit "$GUAC_FIXTURE_CURL_EXIT"\n')
    executable("sleep", "#!/bin/sh\nexit 0\n")
    environment_file = tmp_path / "backend.env"
    environment_file.write_text("GUACAMOLE_DB_PASSWORD=local-fixture\n")

    def run(*, patched=False, ready=True, published="127.0.0.1:8081"):
        war = state / "fixture.war"
        with zipfile.ZipFile(war, "w") as archive:
            tunnel = "guacamole-common-js/all.min.js"
            source = tunnel + ("?v=nova-ve-websocket" if patched else "")
            patch = '<script src="nova-ve-guac-patch.js"></script>' if patched else ""
            archive.writestr("index.html", f'<body><script src="{source}"></script>{patch}</body>')
            socket = 'new WebSocket(b+"?"+f)' if patched else 'new WebSocket(b+"?"+f,"guacamole")'
            archive.writestr(tunnel, socket)
            if patched:
                archive.writestr("nova-ve-guac-patch.js", "// fixture")
        war.chmod(0o600)
        result = subprocess.run(
            ["bash", "-c", 'umask 077; exec bash "$1"', "fixture", str(SCRIPT)],
            env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                 "NOVA_VE_ENV_FILE": str(environment_file),
                 "GUACAMOLE_STATE_DIR": str(state / "runtime"),
                 "GUAC_FIXTURE_STATE": str(state), "GUAC_FIXTURE_CURL_EXIT": "0" if ready else "22",
                 "GUAC_FIXTURE_PUBLISHED": published},
            text=True, capture_output=True, timeout=20,
        )
        return result, state, war

    return run


@pytest.mark.parametrize(("published", "endpoint"), [
    ("127.0.0.1:8081", "http://127.0.0.1:8081/html5/"),
    ("0.0.0.0:18081", "http://127.0.0.1:18081/html5/"),
])
def test_fresh_war_is_readable_with_restrictive_umask(guacamole_deploy, published, endpoint):
    result, state, war = guacamole_deploy(published=published)
    assert result.returncode == 0, result.stderr
    assert war.stat().st_mode & 0o777 == 0o644
    assert (state / "private-dir-mode").read_text() == "0o700"
    assert (state / "restart").exists()
    assert (state / "probes").read_text().splitlines() == [endpoint]


def test_already_patched_unreadable_war_is_repaired(guacamole_deploy):
    result, state, war = guacamole_deploy(patched=True)
    assert result.returncode == 0, result.stderr
    assert war.stat().st_mode & 0o777 == 0o644
    assert (state / "restart").exists()


def test_guacamole_readiness_failure_is_reported(guacamole_deploy):
    result, _, _ = guacamole_deploy(ready=False)
    assert result.returncode == 1
    assert "Guacamole did not become ready" in result.stderr
