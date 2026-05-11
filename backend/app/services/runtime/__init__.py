"""Runtime-backend subpackage.

Each module here implements a single emulator backend (qemu, docker,
dynamips, iol). The dispatch surface lives in
``app.services.node_runtime_service.NodeRuntimeService.start_node``,
which selects a backend based on ``node["type"]``.
"""
