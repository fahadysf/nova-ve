# Lab lifecycle

A lab is the JSON file at `/var/lib/nova-ve/labs/<name>.json`. Its **runtime state** (which nodes are running, console ports, live MACs, host bridges) is separate from the on-disk topology and lives in the backend's runtime tracker.

## Starting a node

- Click a node → **Start** in the right-click menu, or
- Use the lab-level **Start all** action.

Starting a node:

1. Resolves the runtime (Docker / QEMU).
2. Builds + plumbs every interface (veth pair or TAP) into the right bridge.
3. Launches the process (Docker container or QEMU process).
4. Records the runtime entry: PID, console port, started-at timestamp, and per-interface live-MAC state.
5. Flips the node status badge from gray ("stopped") to amber ("starting") and then to emerald ("running").

If start fails, the node stays in the "stopped" state and the error surfaces in the backend log (`journalctl -u nova-ve-backend`).

## Stopping a node

- Click a node → **Stop**, or **Stop all** for the lab.

Stopping:

1. Sends the runtime its terminate signal (Docker: `docker rm -f <id>`; QEMU: `kill` + cleanup).
2. Removes the veth / TAP devices from the host bridges.
3. Clears the runtime tracker entry.
4. Flips the status badge to gray.

The lab JSON is unchanged — node configuration survives stops. Re-starting the node re-creates the runtime state from scratch.

## Saving

There is no explicit save button. Every change you make on the canvas — adding / removing nodes, adding / removing links, moving nodes, dragging ports — is committed to the lab JSON server-side as you make it. The frontend treats the lab JSON as the canonical source of truth, and re-fetches it whenever the live runtime view needs reconciliation.

## Deleting a lab

From the **Labs** index:

1. Hover the lab card → click the delete icon.
2. Confirm the prompt.

Deleting:

- Removes the lab JSON.
- Tears down host bridges that no other lab references. Running node processes are not stopped by the delete itself — they lose their lab binding and are reconciled away by the runtime registry.

## Moving a lab

From the **Labs** index:

1. Hover the lab card → click **Move…**.
2. Pick the target folder and confirm.

The lab keeps its file name — only its folder changes. You can move labs between your own folders and the **Shared** folder, a read-write space visible to every signed-in user (use it to publish a lab for others, or pick one up into your own folder). Moving a lab never disturbs a running node or its console: runtime state tracks the lab's internal ID, not its file path.

## Files and ownership

| Path | Owner | Purpose |
|---|---|---|
| `/var/lib/nova-ve/labs/<name>.json` | `nova-ve` service user | Lab topology |
| `/var/lib/nova-ve/labs/<name>/` | `nova-ve` service user | Per-node runtime state (qcow2 overlays, logs) |
| `/var/lib/nova-ve/labs/<name>.json.lock` | `nova-ve` service user | Cooperative write lock used by the backend |

If you need to inspect a lab JSON from the host, `sudo cat /var/lib/nova-ve/labs/<name>.json | jq` is the canonical recipe.

For backing labs up, see [Backups](../operations/backups.md).
