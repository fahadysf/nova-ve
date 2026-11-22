# Node Add/Edit Workflow Architecture

!!! note "Status: implemented"
    Everything in this design record has shipped: the node catalog
    (`GET /api/labs/{lab}/node-catalog`), the batch-create route
    (`POST /api/labs/{lab}/nodes/batch`), the shared `NodeConfigModal.svelte`
    (add + edit), the `Edit Node` context-menu action, server-side
    runtime-aware editability, and per-node advanced extras (`qemu_nic`,
    `qemu_version`, `architecture`, `nested_virt`, `boot_order`,
    `qemu_options`, `firstmac`). The "Original state" and "Proposed"
    sections below are kept as the historical design record.

## Purpose

Define a durable architecture for replacing the original inline node picker with a centered, capability-driven add-node workflow and a shared edit-node workflow.

This work was broader than a visual polish pass: the starting UI only exposed a thin template/image picker, while the target experience needed richer frontend forms plus backend contracts that can validate, persist, and enforce runtime-aware editability.

## Original state (historical, pre-implementation)

### Frontend

- `frontend/src/lib/components/canvas/TopologyCanvas.svelte`
  - `+` opened a first step: `Node` vs `Network`.
  - Choosing `Node` opened an inline scrollable list anchored to the bottom-left canvas control area.
  - Node creation posted a single node to `POST /api/labs/{lab}/nodes` with only `name` / `type` / `template` / `image` / `left` / `top`.
  - The node context menu exposed `Start`, `Stop`, `Wipe`, `Console`, and `Delete Node`, but no `Edit Node`.

### Backend

- `backend/app/schemas/node.py`
  - `NodeCreate` supported only `name` / `type` / `template` / `image` / `console` / `cpu` / `ram` / `ethernet` / `left` / `top`.
  - `NodeUpdate` supported only `name` / `cpu` / `ram` / `ethernet` / `left` / `top` / `icon` / `config`.
- `backend/app/routers/labs.py`
  - `create_node()` applied template defaults and wrote one node at a time.
  - No batch-create route, no server-side editability policy based on running state.
- `backend/app/services/template_service.py`
  - exposed template defaults and available images, but no node-catalog/form-schema payload and no icon catalog.
- `backend/app/services/node_runtime_service.py`
  - honored only `name` / `image` / `console` / `cpu` / `ram` / `ethernet` / `uuid` / `firstmac` at start time; no per-node overrides for advanced template fields.

## Field model (neutral checklist)

The node authoring surface covers these fields, grouped by role:

- **Identity**: template, image, name prefix, count, icon, UUID.
- **Resources**: CPU (+ CPU limit), RAM, ethernet count, delay.
- **Console**: primary console type, optional secondary console.
- **Placement**: left/top anchor, grid/row multi-add placement.
- **Advanced extras (per-type, stopped-only unless marked runtime)**: `qemu_nic` (NIC model), `qemu_version` (side-by-side QEMU build), `architecture`, `nested_virt`, `boot_order`, `qemu_options` (custom args), `firstmac`.

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

#### Field order (canonical)

The first decision the user makes in the modal is **node type**. Everything else
is downstream of that choice.

1. **Node type tabs** — a top-of-modal row of four icon tabs: `qemu`, `docker`,
   `iol`, `dynamips`. The currently-selected type is highlighted; types with
   no available templates render as disabled. In edit mode the row is rendered
   but locked, since type is immutable after create.
2. **Template** — a dropdown filtered to the templates of the active type. The
   dropdown must NOT mix templates of different types in a single flat list.
3. **Image / Icon / Console**, **CPU / RAM / Ethernets**, **Delay /
   Placement**, then per-type extras.

Behavior:

- user clicks `+`
- user picks `Node`
- a centered overlay modal opens
- the modal loads the node catalog/capabilities
- the user picks the node type from the top tab row
- the template dropdown reflows to show only that type's templates and
  auto-selects the first one
- the modal supports:
  - template selection (filtered by chosen type)
  - image selection
  - node name prefix
  - number of nodes
  - icon selection
  - CPU / RAM / ethernet / console / delay
  - per-type extras as declared by the catalog's `extras_schema`
  - preview of runtime-locked fields in edit mode
- edit mode is launched from a new `Edit Node` context-menu action

### 6. Placement Strategy for Multi-Add

When `count > 1`:

- capture the current viewport center when the modal opens
- place nodes in a deterministic compact grid starting from that anchor
- generate names from `name_prefix` plus an incrementing suffix

This keeps the behavior predictable and avoids stacking all new nodes at the exact same coordinates.

### 7. Advanced Template-Specific Fields

Now implemented (see the status banner): the runtime honors per-node `qemu_nic`, `qemu_version`, `architecture` (`qemu_arch` was renamed during implementation), `nested_virt`, `boot_order`, arbitrary `qemu_options`, and `firstmac` — all exposed through the catalog's `extras_schema` with `stoppedOnly` / `runtime` flags. HDD sizing/management remains future work.

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
