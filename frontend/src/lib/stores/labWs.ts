// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * labWs — US-073 / US-403 / US-404. Svelte stores derived from a
 * {@link WsClient}, tracking granular live state surfaced via WS events:
 *
 *  - ``liveMacs``           keyed by ``${nodeId}:${interfaceIndex}`` from
 *                            ``interface_live_mac`` events.
 *  - ``linkStates``         keyed by ``link_id`` (numeric) from ``link_state``
 *                            events.
 *  - ``nodeStates``         keyed by ``node_id`` (numeric) from ``node_state``
 *                            events.
 *  - ``linkReconciliation`` unified overlay store (US-403 + US-404): keyed by
 *                            ``'iface:<iface>'`` for ``discovered_link`` events
 *                            and ``'link:<link_id>'`` for ``link_divergent``
 *                            events.  Both map into {@link LinkReconciliation}
 *                            records and drive the canvas amber/red overlays.
 *  - ``connected``          reflects ``client.isOpen``; updated on every event.
 *
 * On ``lab_topology`` snapshots ALL derived stores are cleared — the snapshot
 * resets the world; subsequent granular events repopulate.
 *
 * The full backend unification to a single ``link_reconciliation`` event is
 * deferred (see codex comment on #106); this store is the frontend seam that
 * absorbs both event types without exposing the split to consumers.
 */

import { readable, writable, type Readable } from 'svelte/store';

import type { WsClient, WsMessage } from '$lib/services/wsClient';
import { parseIfaceInterfaceIndex } from '$lib/services/canvasEdges';
import type { LinkReconciliation, LiveMacState } from '$lib/types';

export type { LinkReconciliation };

export type LabWsStores = {
  liveMacs: Readable<Record<string, LiveMacState>>;
  linkStates: Readable<Record<number, string>>;
  nodeStates: Readable<Record<number, string>>;
  /** Unified reconciliation overlay: discovered (amber) + divergent (red). */
  linkReconciliation: Readable<Record<string, LinkReconciliation>>;
  /**
   * Remove a single entry from the reconciliation store by key.
   * Used by TopologyCanvas after a successful "Promote to declared link" POST
   * so the amber discovered overlay clears immediately rather than waiting for
   * the next discovery cycle (HIGH-1 fix).
   */
  deleteReconciliation: (key: string) => void;
  connected: Readable<boolean>;
};

interface LiveMacPayload {
  node_id: number;
  interface_index: number;
  state: LiveMacState['state'];
  planned_mac: string;
  live_mac?: string;
  reason?: string;
}

interface LinkStatePayload {
  link_id: number;
  state: string;
}

interface NodeStatePayload {
  node_id: number;
  state: string;
}

interface DiscoveredLinkPayload {
  lab_id?: string;
  network_id: number;
  bridge_name: string;
  iface: string;
  peer_node_id?: number | null;
}

interface LinkDivergentPayload {
  link_id: string;
  lab_id?: string;
  reason?: string;
  last_checked: string;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

export function createLabWsStores(client: WsClient): LabWsStores {
  const liveMacs = writable<Record<string, LiveMacState>>({});
  const linkStates = writable<Record<number, string>>({});
  const nodeStates = writable<Record<number, string>>({});
  const linkReconciliation = writable<Record<string, LinkReconciliation>>({});
  /**
   * MEDIUM-3: epoch counter incremented on every ``lab_topology`` snapshot.
   * Each reconciliation event handler captures the epoch at the start of the
   * call; if it has been bumped by a ``lab_topology`` before the handler
   * writes, the event is treated as stale and discarded.  This prevents a
   * ``discovered_link`` or ``link_divergent`` in-flight from the previous
   * discovery pass from repopulating the store after a reset.
   */
  let topologyEpoch = 0;

  const connected = readable<boolean>(client.isOpen, (set) => {
    const sync = () => set(client.isOpen);
    const unsubOpen = client.on('__open', sync);
    const unsubClose = client.on('__close', sync);
    sync();
    return () => {
      unsubOpen();
      unsubClose();
    };
  });

  client.on('interface_live_mac', (msg: WsMessage) => {
    if (!isObject(msg.payload)) return;
    const payload = msg.payload as Partial<LiveMacPayload>;
    if (typeof payload.node_id !== 'number' || typeof payload.interface_index !== 'number') {
      return;
    }
    if (typeof payload.state !== 'string' || typeof payload.planned_mac !== 'string') {
      return;
    }
    const key = `${payload.node_id}:${payload.interface_index}`;
    const next: LiveMacState = {
      state: payload.state,
      planned_mac: payload.planned_mac,
    };
    if (typeof payload.live_mac === 'string') next.live_mac = payload.live_mac;
    if (typeof payload.reason === 'string') next.reason = payload.reason;
    liveMacs.update((current) => ({ ...current, [key]: next }));
  });

  client.on('link_state', (msg: WsMessage) => {
    if (!isObject(msg.payload)) return;
    const payload = msg.payload as Partial<LinkStatePayload>;
    if (typeof payload.link_id !== 'number' || typeof payload.state !== 'string') return;
    linkStates.update((current) => ({
      ...current,
      [payload.link_id as number]: payload.state as string,
    }));
  });

  client.on('node_state', (msg: WsMessage) => {
    if (!isObject(msg.payload)) return;
    const payload = msg.payload as Partial<NodeStatePayload>;
    if (typeof payload.node_id !== 'number' || typeof payload.state !== 'string') return;
    nodeStates.update((current) => ({
      ...current,
      [payload.node_id as number]: payload.state as string,
    }));
  });

  // US-403: kernel-only iface not in links[] → amber dashed overlay edge.
  // MEDIUM-2: parse peer_interface_index from the iface name on ingestion.
  // MEDIUM-3: capture epoch to discard stale events from previous discovery pass.
  client.on('discovered_link', (msg: WsMessage) => {
    const myEpoch = topologyEpoch;
    if (!isObject(msg.payload)) return;
    const payload = msg.payload as Partial<DiscoveredLinkPayload>;
    if (typeof payload.iface !== 'string' || payload.iface.length === 0) return;
    if (typeof payload.network_id !== 'number') return;
    if (typeof payload.bridge_name !== 'string') return;
    if (topologyEpoch !== myEpoch) return;
    const key = `iface:${payload.iface}`;
    const next: LinkReconciliation = {
      kind: 'discovered',
      key,
      iface: payload.iface,
      network_id: payload.network_id,
      bridge_name: payload.bridge_name,
      peer_node_id: typeof payload.peer_node_id === 'number' ? payload.peer_node_id : null,
      // Derived from the iface name; null when the name scheme is unrecognised.
      peer_interface_index: parseIfaceInterfaceIndex(payload.iface),
    };
    linkReconciliation.update((current) => ({ ...current, [key]: next }));
  });

  // US-404: declared link with no matching kernel veth/TAP → red dashed overlay.
  // MEDIUM-3: same epoch guard.
  client.on('link_divergent', (msg: WsMessage) => {
    const myEpoch = topologyEpoch;
    if (!isObject(msg.payload)) return;
    const payload = msg.payload as Partial<LinkDivergentPayload>;
    if (typeof payload.link_id !== 'string' || payload.link_id.length === 0) return;
    if (typeof payload.last_checked !== 'string') return;
    if (topologyEpoch !== myEpoch) return;
    const key = `link:${payload.link_id}`;
    const next: LinkReconciliation = {
      kind: 'divergent',
      key,
      link_id: payload.link_id,
      last_checked: payload.last_checked,
      reason: typeof payload.reason === 'string' ? payload.reason : '',
    };
    linkReconciliation.update((current) => ({ ...current, [key]: next }));
  });

  // A fresh lab_topology snapshot resets the world.
  // MEDIUM-3: bump the epoch BEFORE clearing the store so any in-flight
  // discovered_link / link_divergent handlers that captured the old epoch
  // will discard their writes.
  client.on('lab_topology', () => {
    topologyEpoch += 1;
    liveMacs.set({});
    linkStates.set({});
    nodeStates.set({});
    linkReconciliation.set({});
  });

  return {
    liveMacs: { subscribe: liveMacs.subscribe },
    linkStates: { subscribe: linkStates.subscribe },
    nodeStates: { subscribe: nodeStates.subscribe },
    linkReconciliation: { subscribe: linkReconciliation.subscribe },
    deleteReconciliation(key: string) {
      linkReconciliation.update((current) => {
        if (!(key in current)) return current;
        const next = { ...current };
        delete next[key];
        return next;
      });
    },
    connected,
  };
}
