from pathlib import Path

from app.services import runtime_pids
from app.services.runtime.iol import IolLauncher


def test_iol_application_id_is_deterministic_and_avoids_active_ids():
    first = IolLauncher.application_id("lab-a", 7, set())
    assert first == IolLauncher.application_id("lab-a", 7, set())

    second = IolLauncher.application_id("lab-a", 7, {first})
    assert 1 <= second <= 512
    assert second != first


def test_iol_command_uses_adapter_counts_and_application_id(tmp_path: Path):
    image = tmp_path / "i86bi-linux-l3.bin"
    image.write_text("fake")
    node = {
        "id": 7,
        "type": "iol",
        "ethernet": 9,
        "ram": 768,
        "extras": {"nvram": 512, "serial_adapters": 0},
    }

    command = IolLauncher.build_command(image, node, 123)

    assert command == [
        str(image),
        "-e",
        "3",
        "-s",
        "0",
        "-n",
        "512",
        "-m",
        "768",
        "123",
    ]


def test_iol_netmap_maps_unique_bridge_id_to_application_id(tmp_path: Path):
    netmap = tmp_path / "NETMAP"

    IolLauncher.write_netmap(netmap, application_id=123, bridge_id=635)

    lines = netmap.read_text().splitlines()
    assert len(lines) == 64
    assert lines[0] == "635:0/0  123:0/0"
    assert lines[-1] == "635:15/3  123:15/3"


def test_iol_image_resolution_supports_importer_directory_layout(tmp_path: Path):
    root = tmp_path / "images" / "iol"
    image_dir = root / "i86bi-linux-l3"
    image_dir.mkdir(parents=True)
    image = image_dir / "i86bi-linux-l3.bin"
    image.write_text("fake")

    resolved = IolLauncher.resolve_image({"image": "i86bi-linux-l3"}, root)

    assert resolved == image


def test_runtime_pid_registry_accepts_iol_kind(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(tmp_path / "pids.json"))

    runtime_pids.register(1234, "iol", "lab-iol", 7)

    assert runtime_pids.lookup(1234)["kind"] == "iol"
