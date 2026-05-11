# Frontend

SvelteKit 2 app on Svelte 5 (runes-friendly), bundled with Vite, styled with Tailwind CSS, with [@xyflow/svelte](https://github.com/xyflow/xyflow) (Svelte Flow) powering the lab canvas.

## Module layout

```
frontend/
├── src/
│   ├── routes/
│   │   └── labs/[...id]/+page.svelte    # The lab editor — top bar, canvas, console workspace
│   └── lib/
│       ├── api.ts                       # Typed wrappers over the backend REST API
│       ├── components/
│       │   ├── ToastStack.svelte        # Top-level toast renderer
│       │   └── canvas/                  # Every editor component (see below)
│       ├── stores/                      # Svelte writables (auth, ws, info panels, drag state, toasts)
│       ├── services/                    # Pure-TS helpers (port layout, edge routing, naming, etc.)
│       └── types/                       # Hand-rolled TS types mirroring backend Pydantic models
├── tests/                               # Vitest unit tests for components + stores
└── package.json
```

## Canvas components

The canvas lives entirely under `src/lib/components/canvas/`. Key files:

| File | What it does |
|---|---|
| `TopologyCanvas.svelte` | Top-level canvas orchestrator. Owns the Svelte Flow viewport, applies layout, drives edges from links. |
| `CustomNode.svelte` | One node card (Docker / QEMU / IOL). Renders status badge, template, console handle, and the bottom-left info `(i)` button. |
| `NetworkNode.svelte` | One network pill. Renders count badge and the `(i)` button next to it. |
| `Port.svelte` | One port handle on a node card. Owns hover tooltip, click-vs-drag detection, and the pinned label for connected ports. |
| `PortLayer.svelte` | Positions ports around a node card's perimeter and dispatches port events to the canvas. |
| `NetworkPort.svelte` | One connection slot on a network pill (N + 1 slots: linked + always-open). |
| `LinkEdge.svelte` | One edge between two endpoints. Owns hover boldening + wide invisible hit-area. |
| `LinkPreview.svelte` | The temporary line that follows the cursor during a link drag. |
| `LinkConfirmModal.svelte` | Modal that surfaces metric / style / label config for a link. |
| `PortTooltip.svelte` | Floating hover tooltip with planned + live MAC. |
| `PortInfoPopover.svelte` | Per-port info popover (single-click on a port). |
| `NodeConfigModal.svelte` | Add / edit a node — template picker, image picker, NIC count, interface naming. |
| `InfoPanel.svelte` | One floating draggable info panel. Pointer-capture drag + viewport clamp. |
| `InfoPanelStack.svelte` | Renders the up-to-4 open info panels in a stack. |

## Stores

| Store | Lives in | Purpose |
|---|---|---|
| `auth` | `src/lib/stores/auth.ts` | Current-user + role state. |
| `session` | `src/lib/stores/session.ts` | Per-session UI prefs (console layout, etc.). |
| `labWs` | `src/lib/stores/labWs.ts` | WebSocket subscription to the backend runtime feed. |
| `dragLink` | `src/lib/stores/dragLink.ts` | Active link-drag state machine. |
| `portInfo` | `src/lib/stores/portInfo.ts` | Per-port popover state. |
| `infoPanels` | `src/lib/stores/infoPanels.ts` | Open info panel list (max 4, LRU drop, drag positions). |
| `toasts` | `src/lib/stores/toasts.ts` | Toast notification queue. |

## Building

```bash
# Dev server with hot reload (proxies API + WS to a local backend on :8000)
cd frontend && npm run dev

# Production build (used by install.sh)
npm run build
# Output: frontend/build/ — gets rsync'd into /var/lib/nova-ve/www/
```

## Type checking & tests

```bash
npx svelte-check --tsconfig ./tsconfig.json --fail-on-warnings
npx vitest run
```

Vitest tests render components with `@testing-library/svelte` against the JSDOM environment. End-to-end tests under `tests/e2e/` run with Playwright and need a live backend.

## Design system

A Tailwind-based component set with a small token palette. See [Design system adoption](design-system.md) for the architectural notes around how it was wired in.
