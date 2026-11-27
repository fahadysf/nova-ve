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

import { get, writable } from 'svelte/store';
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
  /** When omitted, keep the current layout defaults. */
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

/**
 * Server nodes carry the second console as ``second_console`` (snake_case
 * JSON) while the UI reads ``NodeData.secondConsole`` (#268). Normalize on
 * ingest — both directions, camelCase wins — so every consumer (node
 * context menu, config modal) sees a consistent pair regardless of which
 * side produced the record. Returns the same object when nothing changes.
 */
function normalizeNode(node: NodeData): NodeData {
  const snake = (node as NodeData & { second_console?: string | null }).second_console;
  let out = node;
  if (snake !== undefined && snake !== null && !node.secondConsole) {
    out = { ...out, secondConsole: snake as NodeData['secondConsole'] };
  }
  if (node.secondConsole && node.secondConsole !== snake) {
    out = { ...out, second_console: node.secondConsole } as NodeData;
  }
  return out;
}

function normalizeNodes(nodes: Record<string, NodeData>): Record<string, NodeData> {
  let changed = false;
  const out: Record<string, NodeData> = {};
  for (const [key, node] of Object.entries(nodes)) {
    const normalized = normalizeNode(node);
    if (normalized !== node) changed = true;
    out[key] = normalized;
  }
  return changed ? out : nodes;
}

/** Identity of one end of a link, for comparing links by their endpoints. */
function endpointKey(endpoint: Link['from'] | Link['to'] | null | undefined): string {
  if (!endpoint) return '';
  return `${endpoint.node_id ?? ''}:${endpoint.interface_index ?? ''}:${endpoint.network_id ?? ''}`;
}

/** True when two links join the same endpoints in the same direction. */
function joinSameEndpoints(a: Link, b: Link): boolean {
  return endpointKey(a.from) === endpointKey(b.from) && endpointKey(a.to) === endpointKey(b.to);
}

/** ``links`` with ``link`` in place of the entry that has the same id, or appended. */
function withLink(links: Link[], link: Link): Link[] {
  const id = String(link.id);
  const index = links.findIndex((entry) => String(entry.id) === id);
  if (index === -1) return [...links, link];
  const next = links.slice();
  next[index] = link;
  return next;
}

/**
 * Mirror what the server did to the interfaces a link attaches: the
 * node-side interface of a node-to-network link is attached to that network
 * (link deletion already reverses this on its side). Returns the same object
 * when there is nothing to change.
 */
function withAttachedInterfaces(
  nodes: Record<string, NodeData>,
  link: Link
): Record<string, NodeData> {
  let next = nodes;
  for (const [endpoint, other] of [
    [link.from, link.to],
    [link.to, link.from]
  ] as const) {
    const nodeId = endpoint?.node_id;
    const interfaceIndex = endpoint?.interface_index;
    const networkId = other?.network_id;
    if (typeof nodeId !== 'number' || typeof interfaceIndex !== 'number') continue;
    if (typeof networkId !== 'number') continue;
    const node = next[String(nodeId)];
    const attached = node?.interfaces?.[interfaceIndex];
    if (!node || !attached || attached.network_id === networkId) continue;
    next = {
      ...next,
      [String(nodeId)]: {
        ...node,
        interfaces: node.interfaces.map(
          (entry, index) => (index === interfaceIndex ? { ...entry, network_id: networkId } : entry)
        )
      }
    };
  }
  return next;
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
      store.update((state) => ({ ...state, nodes: normalizeNodes(nodes) }));
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
        nodes: { ...state.nodes, [String(nodeId)]: normalizeNode(node) },
      }));
    },

    /**
     * Adopt one link: the entry with the same id is replaced, otherwise the
     * link is appended.
     *
     * Adopting a real link also retires an optimistic placeholder
     * (``tmp_*``) between the same endpoints (#306). The canvas inserts one
     * while POST /links is in flight, and the backend announces the created
     * link over the WebSocket — sometimes before the response arrives.
     */
    upsertLink(link: Link): void {
      store.update((state) => {
        const id = String(link.id);
        const real = !id.startsWith('tmp_');
        let links = real
          ? state.links.filter(
              (entry) => !(String(entry.id).startsWith('tmp_') && joinSameEndpoints(entry, link))
            )
          : state.links;
        links = withLink(links, link);
        return { ...state, links, nodes: withAttachedInterfaces(state.nodes, link) };
      });
    },

    /**
     * Adopt the result of POST /links (#306): the created link, the network
     * it created if any (a node-to-node link joins the two nodes through an
     * implicit bridge), and the interface attachments it made.
     * ``placeholderId`` is the optimistic link the request replaced.
     */
    applyCreatedLink(
      link: Link,
      network: NetworkData | null | undefined,
      placeholderId?: string
    ): void {
      store.update((state) => {
        const links = placeholderId
          ? state.links.filter((entry) => String(entry.id) !== placeholderId)
          : state.links;
        return {
          ...state,
          links: withLink(links, link),
          nodes: withAttachedInterfaces(state.nodes, link),
          networks: network ? { ...state.networks, [String(network.id)]: network } : state.networks
        };
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
        nodes: preservePositions(normalizeNodes(snapshot.nodes), state.nodes, preserve.nodeIds),
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
      const state = get(store);
      const bucket = kind === 'node' ? 'nodes' : 'networks';
      const current = state[bucket][key];
      if (!current || (current.left === left && current.top === top)) return false;
      store.set({
        ...state,
        [bucket]: { ...state[bucket], [key]: { ...current, left, top } },
      });
      return true;
    },

    reset(): void {
      store.set(emptyState());
    },
  };
}

/** Module-level singleton: one lab editor is mounted at a time. */
export const labState = createLabState();
