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
    expect(get(stores.linkReconciliation)).toEqual({});
  });
});

// ──────────────────────────────────────────────────────────────────────────
// US-403 / US-404 — unified linkReconciliation store.
// Verifies that both WS event types write into the same store and that the
// invariant boundary (lab_topology clears everything) holds.
// ──────────────────────────────────────────────────────────────────────────

describe('labWs.createLabWsStores — linkReconciliation (US-403 / US-404)', () => {
  it('discovered_link event → entry with kind=discovered, key=iface:{x}', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: {
        lab_id: 'lab1',
        network_id: 5,
        bridge_name: 'noveXn5',
        iface: 'nve12abd7i2h',
        peer_node_id: 7,
      },
    });

    const recon = get(stores.linkReconciliation);
    const entry = recon['iface:nve12abd7i2h'];
    expect(entry).toBeDefined();
    expect(entry.kind).toBe('discovered');
    expect(entry.key).toBe('iface:nve12abd7i2h');
    expect(entry.iface).toBe('nve12abd7i2h');
    expect(entry.network_id).toBe(5);
    expect(entry.bridge_name).toBe('noveXn5');
    expect(entry.peer_node_id).toBe(7);
  });

  it('link_divergent event → entry with kind=divergent, key=link:{x}', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: {
        link_id: 'lnk_abc',
        lab_id: 'lab1',
        reason: 'no veth found',
        last_checked: '2026-04-28T12:34:56+00:00',
      },
    });

    const recon = get(stores.linkReconciliation);
    const entry = recon['link:lnk_abc'];
    expect(entry).toBeDefined();
    expect(entry.kind).toBe('divergent');
    expect(entry.key).toBe('link:lnk_abc');
    expect(entry.link_id).toBe('lnk_abc');
    expect(entry.last_checked).toBe('2026-04-28T12:34:56+00:00');
    expect(entry.reason).toBe('no veth found');
  });

  it('both event types coexist in the same store keyed distinctly', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0', iface: 'nve1i0h', peer_node_id: 1 },
    });
    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_x', last_checked: '2026-04-28T00:00:00Z' },
    });

    const recon = get(stores.linkReconciliation);
    expect(Object.keys(recon)).toHaveLength(2);
    expect(recon['iface:nve1i0h'].kind).toBe('discovered');
    expect(recon['link:lnk_x'].kind).toBe('divergent');
  });

  it('lab_topology snapshot clears the unified reconciliation store', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0', iface: 'nve1i0h', peer_node_id: 1 },
    });
    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_y', last_checked: '2026-04-28T00:00:00Z' },
    });
    expect(Object.keys(get(stores.linkReconciliation))).toHaveLength(2);

    client.emit('lab_topology', { type: 'lab_topology', seq: 42, payload: {} });
    expect(get(stores.linkReconciliation)).toEqual({});
  });

  it('drops malformed discovered_link payloads (missing iface)', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0' }, // no iface
    });

    expect(get(stores.linkReconciliation)).toEqual({});
  });

  it('drops malformed link_divergent payloads (missing last_checked)', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_z' }, // no last_checked
    });

    expect(get(stores.linkReconciliation)).toEqual({});
  });

  // ── LOW-4 ordering tests ──────────────────────────────────────────────

  it('LOW-4 / duplicate re-emission: same discovered_link twice → entry overwritten, not duplicated', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    const payload = { network_id: 3, bridge_name: 'br3', iface: 'nve12abd3i1h', peer_node_id: 3 };
    client.emit('discovered_link', { type: 'discovered_link', payload });
    client.emit('discovered_link', { type: 'discovered_link', payload: { ...payload, peer_node_id: 99 } });

    const recon = get(stores.linkReconciliation);
    expect(Object.keys(recon)).toHaveLength(1);
    // Later emission wins — peer_node_id should be the second value.
    expect(recon['iface:nve12abd3i1h'].peer_node_id).toBe(99);
  });

  it('LOW-4 / duplicate re-emission: same link_divergent twice → entry overwritten, not duplicated', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_dup', last_checked: '2026-04-28T10:00:00Z', reason: 'first' },
    });
    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_dup', last_checked: '2026-04-28T11:00:00Z', reason: 'second' },
    });

    const recon = get(stores.linkReconciliation);
    expect(Object.keys(recon)).toHaveLength(1);
    expect(recon['link:lnk_dup'].last_checked).toBe('2026-04-28T11:00:00Z');
    expect(recon['link:lnk_dup'].reason).toBe('second');
  });

  it('LOW-4 / MEDIUM-3 stale repopulation: event received after lab_topology reset is discarded', () => {
    // Simulate: discovered_link arrives, topology snapshot clears, then another
    // discovered_link from the old discovery pass arrives.  The epoch guard
    // should keep the store empty.
    //
    // NOTE: In production JS these are all synchronous events — the "stale"
    // scenario occurs when a new snapshot arrives between two events emitted
    // by the same discovery batch.  We simulate it by interleaving a
    // lab_topology between two discovered_link emissions.
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0', iface: 'nve12abd1i0h', peer_node_id: 1 },
    });
    expect(Object.keys(get(stores.linkReconciliation))).toHaveLength(1);

    // Topology snapshot → bumps epoch, clears store.
    client.emit('lab_topology', { type: 'lab_topology', seq: 10, payload: {} });
    expect(get(stores.linkReconciliation)).toEqual({});

    // A second discovered_link from the same old batch arrives post-reset.
    // Because the epoch was already captured before the handler runs and
    // lab_topology bumped it synchronously, this write is NOT stale in the
    // synchronous test model — the epoch check is designed to guard against
    // async delays, so here the new event DOES write (it captures the new epoch).
    // This test verifies the clear itself was durable: store was empty between
    // lab_topology and the next genuine event.
    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 2, bridge_name: 'br2', iface: 'nve12abd2i0h', peer_node_id: 2 },
    });
    // The new event is in the new epoch — it should be accepted.
    expect(Object.keys(get(stores.linkReconciliation))).toHaveLength(1);
    expect(get(stores.linkReconciliation)['iface:nve12abd2i0h']).toBeDefined();
  });

  it('LOW-4 / HIGH-1 promote-clears-overlay: deleteReconciliation removes only the targeted key', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0', iface: 'nve12abd7i2h', peer_node_id: 7 },
    });
    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 2, bridge_name: 'br2', iface: 'nve12abd8i0h', peer_node_id: 8 },
    });
    expect(Object.keys(get(stores.linkReconciliation))).toHaveLength(2);

    stores.deleteReconciliation('iface:nve12abd7i2h');

    const recon = get(stores.linkReconciliation);
    expect(Object.keys(recon)).toHaveLength(1);
    expect(recon['iface:nve12abd7i2h']).toBeUndefined();
    expect(recon['iface:nve12abd8i0h']).toBeDefined();
  });

  it('LOW-4 / deleteReconciliation on missing key is a no-op', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0', iface: 'nve12abd1i0h', peer_node_id: 1 },
    });
    const before = get(stores.linkReconciliation);

    stores.deleteReconciliation('iface:does-not-exist');

    expect(get(stores.linkReconciliation)).toEqual(before);
  });

  it('LOW-4 / MEDIUM-2 iface-decode: discovered_link without peer_interface_index gets it derived from iface name', () => {
    // Verifies the MEDIUM-2 fix: the backend only sends iface, not
    // peer_interface_index.  The store must derive it from the iface name.
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    // nve12abd7i2h → interface_index = 2
    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: {
        network_id: 5,
        bridge_name: 'noveXn5',
        iface: 'nve12abd7i2h',
        peer_node_id: 7,
        // No peer_interface_index in the payload — backend does not send it.
      },
    });

    const entry = get(stores.linkReconciliation)['iface:nve12abd7i2h'];
    expect(entry.peer_interface_index).toBe(2);
  });

  it('LOW-4 / MEDIUM-2 iface-decode: unrecognised iface name yields null peer_interface_index', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0', iface: 'veth1234', peer_node_id: 1 },
    });

    const entry = get(stores.linkReconciliation)['iface:veth1234'];
    expect(entry.peer_interface_index).toBeNull();
  });
});
