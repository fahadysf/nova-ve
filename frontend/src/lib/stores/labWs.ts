// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * labWs — US-073. Svelte stores derived from a {@link WsClient}, tracking
 * granular live state surfaced via WS events:
 *
 *  - ``liveMacs``     keyed by ``${nodeId}:${interfaceIndex}`` from
 *                      ``interface_live_mac`` events.
 *  - ``linkStates``   keyed by ``link_id`` (numeric) from ``link_state`` events.
 *  - ``nodeStates``   keyed by ``node_id`` (numeric) from ``node_state`` events.
 *  - ``connected``    reflects ``client.isOpen``; updated on every event.
 *
 * On ``lab_topology`` snapshots all three derived stores are cleared — the
 * snapshot resets the world; subsequent granular events repopulate.
 */

import { readable, writable, type Readable } from 'svelte/store';

import type { WsClient, WsMessage } from '$lib/services/wsClient';
import type { LiveMacState } from '$lib/types';

export type LabWsStores = {
  liveMacs: Readable<Record<string, LiveMacState>>;
  linkStates: Readable<Record<number, string>>;
  nodeStates: Readable<Record<number, string>>;
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

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}

export function createLabWsStores(client: WsClient): LabWsStores {
  const liveMacs = writable<Record<string, LiveMacState>>({});
  const linkStates = writable<Record<number, string>>({});
  const nodeStates = writable<Record<number, string>>({});

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
    linkStates.update((current) => ({ ...current, [payload.link_id as number]: payload.state as string }));
  });

  client.on('node_state', (msg: WsMessage) => {
    if (!isObject(msg.payload)) return;
    const payload = msg.payload as Partial<NodeStatePayload>;
    if (typeof payload.node_id !== 'number' || typeof payload.state !== 'string') return;
    nodeStates.update((current) => ({ ...current, [payload.node_id as number]: payload.state as string }));
  });

  client.on('lab_topology', () => {
    liveMacs.set({});
    linkStates.set({});
    nodeStates.set({});
  });

  return {
    liveMacs: { subscribe: liveMacs.subscribe },
    linkStates: { subscribe: linkStates.subscribe },
    nodeStates: { subscribe: nodeStates.subscribe },
    connected,
  };
}
