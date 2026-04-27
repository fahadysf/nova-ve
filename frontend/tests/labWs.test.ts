// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import { createLabWsStores } from '$lib/stores/labWs';
import type { WsClient, WsHandler, WsMessage } from '$lib/services/wsClient';

interface FakeClient extends WsClient {
  emit(type: string, msg: WsMessage): void;
}

function makeFakeClient(): FakeClient {
  const handlers = new Map<string, Set<WsHandler>>();
  let abortController = new AbortController();
  return {
    connect() {},
    close() {},
    on(type: string, handler: WsHandler) {
      let set = handlers.get(type);
      if (!set) {
        set = new Set();
        handlers.set(type, set);
      }
      set.add(handler);
      return () => {
        handlers.get(type)?.delete(handler);
      };
    },
    get lastSeq() {
      return 0;
    },
    get abortController() {
      return abortController;
    },
    get isOpen() {
      return false;
    },
    emit(type: string, msg: WsMessage) {
      if (type === 'lab_topology') {
        abortController.abort();
        abortController = new AbortController();
      }
      const set = handlers.get(type);
      if (!set) return;
      for (const handler of Array.from(set)) handler(msg);
    },
  };
}

describe('labWs.createLabWsStores', () => {
  it('updates liveMacs from interface_live_mac events keyed by ${nodeId}:${interfaceIndex}', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('interface_live_mac', {
      type: 'interface_live_mac',
      payload: {
        node_id: 5,
        interface_index: 2,
        state: 'mismatch',
        planned_mac: 'aa:bb:cc:dd:ee:ff',
        live_mac: '11:22:33:44:55:66',
      },
    });

    const liveMacs = get(stores.liveMacs);
    expect(liveMacs['5:2']).toEqual({
      state: 'mismatch',
      planned_mac: 'aa:bb:cc:dd:ee:ff',
      live_mac: '11:22:33:44:55:66',
    });
  });

  it('updates nodeStates from node_state events', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('node_state', {
      type: 'node_state',
      payload: { node_id: 12, state: 'starting' },
    });
    client.emit('node_state', {
      type: 'node_state',
      payload: { node_id: 12, state: 'running' },
    });
    client.emit('node_state', {
      type: 'node_state',
      payload: { node_id: 13, state: 'stopped' },
    });

    const nodeStates = get(stores.nodeStates);
    expect(nodeStates).toEqual({ 12: 'running', 13: 'stopped' });
  });

  it('updates linkStates from link_state events', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('link_state', {
      type: 'link_state',
      payload: { link_id: 7, state: 'up' },
    });
    client.emit('link_state', {
      type: 'link_state',
      payload: { link_id: 8, state: 'down' },
    });

    const linkStates = get(stores.linkStates);
    expect(linkStates).toEqual({ 7: 'up', 8: 'down' });
  });

  it('clears all derived state on lab_topology snapshots', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('node_state', { type: 'node_state', payload: { node_id: 1, state: 'running' } });
    client.emit('link_state', { type: 'link_state', payload: { link_id: 2, state: 'up' } });
    client.emit('interface_live_mac', {
      type: 'interface_live_mac',
      payload: {
        node_id: 1,
        interface_index: 0,
        state: 'confirmed',
        planned_mac: 'aa:bb:cc:dd:ee:ff',
      },
    });
    expect(Object.keys(get(stores.nodeStates))).toHaveLength(1);

    client.emit('lab_topology', { type: 'lab_topology', seq: 99, payload: {} });
    expect(get(stores.nodeStates)).toEqual({});
    expect(get(stores.linkStates)).toEqual({});
    expect(get(stores.liveMacs)).toEqual({});
  });
});
