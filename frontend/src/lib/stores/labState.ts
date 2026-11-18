// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * labState — the single owner of lab editor state (#245).
 *
 * The page and TopologyCanvas previously each held private copies of
 * nodes/networks/topology/links/defaults reconciled by reference-identity
 * prop syncing and 'changed'-event payload round-trips. That split caused
 * the #241 reset loop: a racing loadLab() snapshot wholesale-replaced the
 * canvas clones mid-edit and the stale copy was echoed back to the server.
 *
 * Both consumers now subscribe to this store. Incoming server snapshots go
 * through ``applySnapshot``, which merges around positions that are still
 * queued/in-flight to PUT /layout so a racing refresh cannot clobber them.
 * Mutators replace whole fields (mirroring the old ``localX = next``
 * assignments) or patch single entries; all of them notify synchronously.
 */

import { writable } from 'svelte/store';
import type { LabDefaults, LabMeta, Link, NetworkData, NodeData, TopologyLink } from '$lib/types';

export interface LabEditorState {
  labId: string | null;
  meta: LabMeta | null;
  nodes: Record<string, NodeData>;
  networks: Record<string, NetworkData>;
  topology: TopologyLink[];
  links: Link[];
  defaults: LabDefaults;
  /**
   * #245: machine-owned revision guarding PUT /layout and PUT /topology
   * via If-Match. Mirrors meta.revision but survives when meta is absent
   * (e.g. before the first loadLab); updated directly from PUT responses.
   */
  revision: number | null;
}

/** Server snapshot to adopt wholesale (from loadLab's parallel GETs). */
export interface LabSnapshot {
  meta?: LabMeta | null;
  nodes: Record<string, NodeData>;
  networks: Record<string, NetworkData>;
  topology: TopologyLink[];
  links: Link[];
  /**
   * Optional: loadLab's parallel GETs do not fetch defaults — the canvas
   * owns default link style updates via PUT /layout. When omitted, the
   * current value is kept instead of resetting to a constant.
   */
  defaults?: LabDefaults;
}

export interface LabStatePositionPreserve {
  /** Node ids whose positions are queued/in-flight to PUT /layout. */
  nodeIds?: ReadonlySet<number>;
  /** Network ids whose positions are queued/in-flight to PUT /layout. */
  networkIds?: ReadonlySet<number>;
}

function emptyState(): LabEditorState {
  return {
    labId: null,
    meta: null,
    nodes: {},
    networks: {},
    topology: [],
    links: [],
    defaults: { link_style: 'orthogonal' },
    revision: null,
  };
}

/**
 * Overlay the current left/top for the given ids onto the incoming
 * snapshot. A snapshot racing the debounced layout PUT would otherwise
 * adopt (and later echo back) pre-drag coordinates for exactly those
 * elements. Only positions are preserved — every other field is incoming.
 */
function preservePositions<T extends { left?: number; top?: number }>(
  incoming: Record<string, T>,
  current: Record<string, T>,
  ids: ReadonlySet<number> | undefined
): Record<string, T> {
  if (!ids || ids.size === 0) {
    return incoming;
  }
  let patched = false;
  const next: Record<string, T> = { ...incoming };
  for (const id of ids) {
    const key = String(id);
    const incomingEntry = next[key];
    const currentEntry = current[key];
    if (incomingEntry && currentEntry) {
      next[key] = { ...incomingEntry, left: currentEntry.left, top: currentEntry.top };
      patched = true;
    }
  }
  return patched ? next : incoming;
}

function createLabState() {
  const store = writable<LabEditorState>(emptyState());

  return {
    subscribe: store.subscribe,

    /** Bind the store to a lab (page mount / route change). */
    setLabId(labId: string): void {
      store.update((state) => (state.labId === labId ? state : { ...state, labId }));
    },

    setMeta(meta: LabMeta | null): void {
      store.update((state) => ({ ...state, meta }));
    },

    setNodes(nodes: Record<string, NodeData>): void {
      store.update((state) => ({ ...state, nodes }));
    },

    setNetworks(networks: Record<string, NetworkData>): void {
      store.update((state) => ({ ...state, networks }));
    },

    setTopology(topology: TopologyLink[]): void {
      store.update((state) => ({ ...state, topology }));
    },

    setLinks(links: Link[]): void {
      store.update((state) => ({ ...state, links }));
    },

    setDefaults(defaults: LabDefaults): void {
      store.update((state) => ({ ...state, defaults }));
    },

    upsertNode(nodeId: number, node: NodeData): void {
      store.update((state) => ({
        ...state,
        nodes: { ...state.nodes, [String(nodeId)]: node },
      }));
    },

    upsertLink(link: Link): void {
      store.update((state) => {
        const id = String(link.id);
        const index = state.links.findIndex((entry) => String(entry.id) === id);
        if (index === -1) {
          return { ...state, links: [...state.links, link] };
        }
        const links = state.links.slice();
        links[index] = link;
        return { ...state, links };
      });
    },

    removeLink(linkId: string): void {
      store.update((state) => ({
        ...state,
        links: state.links.filter((entry) => String(entry.id) !== linkId),
      }));
    },

    /**
     * Adopt a server snapshot (loadLab result, WS-driven refresh). Positions
     * of elements with a pending layout PUT are carried over from the
     * current state; everything else is incoming-authoritative.
     */
    applySnapshot(snapshot: LabSnapshot, preserve: LabStatePositionPreserve = {}): void {
      store.update((state) => ({
        ...state,
        meta: snapshot.meta !== undefined ? snapshot.meta : state.meta,
        revision:
          snapshot.meta && typeof snapshot.meta.revision === 'number'
            ? snapshot.meta.revision
            : state.revision,
        nodes: preservePositions(snapshot.nodes, state.nodes, preserve.nodeIds),
        networks: preservePositions(snapshot.networks, state.networks, preserve.networkIds),
        topology: snapshot.topology,
        links: snapshot.links,
        defaults: snapshot.defaults !== undefined ? snapshot.defaults : state.defaults,
      }));
    },

    /** Refresh the concurrency token after a successful guarded write (#245). */
    setRevision(revision: number): void {
      store.update((state) =>
        state.revision === revision
          ? state
          : {
              ...state,
              revision,
              meta: state.meta ? { ...state.meta, revision } : state.meta,
            }
      );
    },

    /**
     * Apply a canvas drag-stop position for one node/network. Returns true
     * when the element exists and the position was adopted. Centralised so
     * components never assign through `$labState.x.y = v` — Svelte 5 legacy
     * compilation turns that into `store.set($store, …)`, which the
     * labState facade does not implement (`e.set is not a function`).
     */
    applyDragStop(kind: 'node' | 'network', id: number, left: number, top: number): boolean {
      const key = String(id);
      let applied = false;
      store.update((state) => {
        if (kind === 'node') {
          const current = state.nodes[key];
          if (!current) return state;
          applied = true;
          return {
            ...state,
            nodes: { ...state.nodes, [key]: { ...current, left, top } },
          };
        }
        const current = state.networks[key];
        if (!current) return state;
        applied = true;
        return {
          ...state,
          networks: { ...state.networks, [key]: { ...current, left, top } },
        };
      });
      return applied;
    },

    reset(): void {
      store.set(emptyState());
    },
  };
}

/** Module-level singleton: one lab editor is mounted at a time. */
export const labState = createLabState();
