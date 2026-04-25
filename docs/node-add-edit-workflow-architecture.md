# Node Add/Edit Workflow Architecture

## Purpose

Define a durable architecture for replacing the current inline node picker with a centered, capability-driven add-node workflow and a shared edit-node workflow.

This work is broader than a visual polish pass. The current UI only exposes a thin template/image picker, while the requested experience needs richer frontend forms plus backend contracts that can validate, persist, and enforce runtime-aware editability.

## Current State

### Frontend

- `frontend/src/lib/components/canvas/TopologyCanvas.svelte`
  - `+` already opens the correct first step: `Node` vs `Network`.
  - Choosing `Node` currently opens an inline scrollable list anchored to the bottom-left canvas control area.
  - `createNodeAt()` posts a single node to `POST /api/labs/{lab}/nodes` with only:
    - `name`
    - `type`
    - `template`
    - `image`
    - `left`
    - `top`
  - The node context menu exposes `Start`, `Stop`, `Wipe`, `Console`, and `Delete Node`, but no `Edit Node`.

### Backend

- `backend/app/schemas/node.py`
  - `NodeCreate` supports:
    - `name`
    - `type`
    - `template`
    - `image`
    - `console`
    - `cpu`
    - `ram`
    - `ethernet`
    - `left`
    - `top`
  - `NodeUpdate` supports:
    - `name`
    - `cpu`
    - `ram`
    - `ethernet`
    - `left`
    - `top`
    - `icon`
    - `config`
- `backend/app/routers/labs.py`
  - `create_node()` applies template defaults and writes one node at a time.
  - `update_node()` writes the provided fields directly and resizes interfaces.
  - There is no batch-create route.
  - There is no server-side editability policy based on running state.
- `backend/app/services/template_service.py`
  - exposes template defaults and available images
  - does not expose a single node-catalog or form-schema payload
  - does not expose an icon catalog
- `backend/app/services/node_runtime_service.py`
  - current runtime actually honors only a subset of node fields at start time:
    - `name`
    - `image`
    - `console`
    - `cpu`
    - `ram`
    - `ethernet`
    - `uuid`
    - `firstmac`
  - it does not currently honor per-node overrides for advanced template fields like `qemu_version`, `qemu_arch`, `qemu_nic`, or arbitrary `qemu_options`

## External Reference Summary

### EVE-NG

Reference material reviewed:

- EVE-NG API docs: `https://www.eve-ng.net/index.php/how-to-eve-ng-api/`
- EVE cookbook snippets surfaced from the published cookbook PDFs

Observed common add-node fields:

- template
- number of nodes to add
- image
- name/prefix
- icon
- UUID
- CPU limit
- CPU
- RAM
- ethernets
- delay
- console
- left/top

Observed advanced fields in the cookbook UI:

- qemu version
- qemu arch
- qemu nic
- qemu custom options
- startup configuration

### PNETLab

Reference material reviewed:

- `https://github.com/pnetlab/pnetlab_main`
- `https://pnetlab.com/pages/documentation`

Observed patterns:

- templates carry defaults like `icon`, `cpu`, `ram`, `ethernet`, and `console`
- the platform treats node resource controls and icon choice as first-class concerns
- the UI model is template-driven rather than hard-coded per one frontend list shape

## Design Decision

Adopt a capability-driven node configuration system rather than adding more hard-coded fields to the current canvas menu.

That means:

1. one backend contract for node templates, defaults, images, icon choices, numeric limits, and editability rules
2. one centered node configuration modal reused for both add and edit
3. one server-side policy that decides which fields are mutable while a node is running

## Proposed Architecture

### 1. Node Catalog / Capability Contract

Add a backend response dedicated to node authoring, for example:

- `GET /api/labs/{lab_path:path}/node-catalog`

Recommended payload shape:

- template groups by node type
- display label per template
- template defaults:
  - `icon`
  - `cpu`
  - `ram`
  - `ethernet`
  - `console`
  - `delay`
- available images for the template
- icon options
- validation constraints:
  - numeric min/max where known
  - whether a field is required
- capability flags:
  - visible on create
  - visible on edit
  - editable while running
  - editable only while stopped
  - immutable after create

Why this is the durable path:

- the frontend stops synthesizing forms from ad hoc endpoint loops
- add and edit can use the same field metadata
- future template-specific fields can be added without reworking the modal architecture

### 2. Batch Node Creation

Add a dedicated batch-create route instead of overloading the current single-create route:

- `POST /api/labs/{lab_path:path}/nodes/batch`

Recommended request shape:

- `type`
- `template`
- `image`
- `name_prefix`
- `count`
- `icon`
- `cpu`
- `ram`
- `ethernet`
- `console`
- `delay`
- anchor position:
  - `left`
  - `top`
- optional placement mode:
  - `grid`
  - `row`

Recommended response shape:

- created node list
- created ids
- resolved defaults actually applied

Why not loop the existing single-node route from the frontend:

- server-side naming and placement stay deterministic
- validation runs once against one request model
- partial failure handling is clearer
- future quota/resource checks belong on the server

### 3. Enhanced Single-Node Update

Extend `PUT /api/labs/{lab_path:path}/nodes/{node_id}` to support the common configurable fields that matter after creation:

- `name`
- `icon`
- `cpu`
- `ram`
- `ethernet`
- `console`
- `delay`
- `image`
- `left`
- `top`

Explicitly keep these immutable after create in the first implementation unless a later runtime change reopens them:

- `type`
- `template`

`uuid` should be create-time only in the first implementation. The runtime depends on it for QEMU identity and there is no current migration path for changing it safely on an existing node.

### 4. Runtime-Aware Editability Policy

Enforce field mutability on the backend, not only in the Svelte form.

Recommended matrix for the first shipped pass:

- always editable:
  - `name`
  - `icon`
  - `left`
  - `top`
- editable only while stopped:
  - `image`
  - `cpu`
  - `ram`
  - `ethernet`
  - `console`
  - `delay`
- create-only:
  - `count`
  - `name_prefix`
- immutable after create:
  - `type`
  - `template`
  - `uuid`

The modal should render stopped-only fields as disabled with clear messaging when the node is running, but the backend must still reject invalid writes.

### 5. Shared Frontend Modal

Add a reusable component, for example:

- `frontend/src/lib/components/canvas/NodeConfigModal.svelte`

Modes:

- `create`
- `edit`

Behavior:

- user clicks `+`
- user picks `Node`
- a centered overlay modal opens
- the modal loads the node catalog/capabilities
- the modal supports:
  - template selection
  - image selection
  - node name prefix
  - number of nodes
  - icon selection
  - CPU / RAM / ethernet / console / delay
  - preview of runtime-locked fields in edit mode
- edit mode is launched from a new `Edit Node` context-menu action

### 6. Placement Strategy for Multi-Add

When `count > 1`:

- capture the current viewport center when the modal opens
- place nodes in a deterministic compact grid starting from that anchor
- generate names from `name_prefix` plus an incrementing suffix

This keeps the behavior predictable and avoids stacking all new nodes at the exact same coordinates.

### 7. Advanced Template-Specific Fields

The reference products expose more fields than the current runtime can honor safely.

For this tracker:

- support the common cross-template fields listed above end-to-end
- design the node catalog payload so future per-template advanced fields can be added
- do not fake support for fields that the runtime ignores today

Explicitly out of scope for the first implementation unless backend runtime support is added in the same tracker:

- per-node `qemu_version`
- per-node `qemu_arch`
- per-node `qemu_nic`
- arbitrary per-node `qemu_options`
- HDD sizing/management

## Issue Slices

1. Tracker: capability-driven node add/edit workflow
2. Backend: node catalog, icon options, batch create, and editability enforcement
3. Frontend add flow: centered add-node modal and multi-add placement
4. Frontend edit flow: `Edit Node` context-menu action and shared modal edit mode
5. Verification/docs: backend tests, browser validation, and tracker close-out evidence

## Related Existing Issues

- `#24` and `#25` cover the earlier, narrower palette/image exposure request.
- The new tracker should treat them as related context, not as sufficient scope, because the requested add/edit workflow is broader than a simple inline palette.

## Verification Focus

Primary lab route:

- `/labs/ui-reactivity-check.json`

Core scenarios:

- add one node through the modal
- add multiple nodes through the modal
- edit a stopped node
- edit a running node and confirm runtime-locked fields are disabled and backend-enforced
- preserve existing network add flow
- preserve drag/save, console, and node start/stop flows
