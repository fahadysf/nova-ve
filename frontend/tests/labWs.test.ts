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
// US-404 — linkReconciliation store (divergent links).
// Verifies divergent entries are added and cleared, and that the invariant
// boundary (lab_topology clears everything) holds.
// ──────────────────────────────────────────────────────────────────────────

describe('labWs.createLabWsStores — linkReconciliation (US-404)', () => {
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

  it('link_divergence_cleared removes only that link\'s divergent entry', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    for (const id of ['lnk_a', 'lnk_b']) {
      client.emit('link_divergent', {
        type: 'link_divergent',
        payload: { link_id: id, last_checked: '2026-04-28T00:00:00Z', generation: 3 },
      });
    }

    client.emit('link_divergence_cleared', {
      type: 'link_divergence_cleared',
      payload: { link_id: 'lnk_a', lab_id: 'lab1', generation: 4 },
    });

    expect(Object.keys(get(stores.linkReconciliation))).toEqual(['link:lnk_b']);
  });

  it('ignores link_divergence_cleared from before the last snapshot', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('lab_topology', { type: 'lab_topology', seq: 1, generation: 5, payload: {} });
    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_a', last_checked: '2026-04-28T00:00:00Z', generation: 6 },
    });
    client.emit('link_divergence_cleared', {
      type: 'link_divergence_cleared',
      payload: { link_id: 'lnk_a', generation: 5 },
    });

    expect(get(stores.linkReconciliation)['link:lnk_a']).toBeDefined();
  });

  it('ignores discovered_link events (the promote-to-link suggestion was removed)', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('discovered_link', {
      type: 'discovered_link',
      payload: { network_id: 1, bridge_name: 'br0', iface: 'nve12abd1i0h', peer_node_id: 1 },
    });

    expect(get(stores.linkReconciliation)).toEqual({});
  });

  it('lab_topology snapshot clears the unified reconciliation store', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    for (const id of ['lnk_x', 'lnk_y']) {
      client.emit('link_divergent', {
        type: 'link_divergent',
        payload: { link_id: id, last_checked: '2026-04-28T00:00:00Z' },
      });
    }
    expect(Object.keys(get(stores.linkReconciliation))).toHaveLength(2);

    client.emit('lab_topology', { type: 'lab_topology', seq: 42, payload: {} });
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

  it('MEDIUM-3 / gen-token: link_divergent with gen <= topology generation is rejected', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    // lab_topology with generation=3 sets the threshold.
    client.emit('lab_topology', { type: 'lab_topology', seq: 5, generation: 3, payload: {} } as unknown as import('$lib/services/wsClient').WsMessage);

    // Stale divergent event (gen 2 <= 3) → rejected.
    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_stale', last_checked: '2026-04-28T00:00:00Z', generation: 2 },
    });
    expect(get(stores.linkReconciliation)).toEqual({});

    // Same-cycle divergent event (gen 3 <= 3) → also rejected.
    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_same', last_checked: '2026-04-28T00:00:00Z', generation: 3 },
    });
    expect(get(stores.linkReconciliation)).toEqual({});

    // Next-cycle divergent event (gen 4 > 3) → accepted.
    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_fresh', last_checked: '2026-04-28T01:00:00Z', generation: 4 },
    });
    expect(get(stores.linkReconciliation)).toHaveProperty('link:lnk_fresh');
  });

  it('MEDIUM-3 / gen-token backward-compat: link_divergent without generation field is accepted even after lab_topology', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);

    client.emit('lab_topology', { type: 'lab_topology', seq: 1, generation: 10, payload: {} } as unknown as import('$lib/services/wsClient').WsMessage);

    client.emit('link_divergent', {
      type: 'link_divergent',
      payload: { link_id: 'lnk_legacy', last_checked: '2026-04-28T00:00:00Z' },
    });
    expect(get(stores.linkReconciliation)).toHaveProperty('link:lnk_legacy');
  });

  // #153: per-link mutation events.
  it('onLinkChange fires on link_created with the full link payload', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);
    const events: unknown[] = [];
    stores.onLinkChange((event) => events.push(event));

    client.emit('link_created', {
      type: 'link_created',
      payload: { link: { id: 'lnk_001', from: { node_id: 1, interface_index: 0 }, to: { network_id: 7 } } },
    });

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({ kind: 'created', link: { id: 'lnk_001' } });
  });

  it('onLinkChange fires on link_deleted with the link_id stringified', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);
    const events: unknown[] = [];
    stores.onLinkChange((event) => events.push(event));

    // Backend may send numeric or string link_id depending on the event source;
    // the contract is that subscribers receive a string.
    client.emit('link_deleted', { type: 'link_deleted', payload: { link_id: 42 } });
    client.emit('link_deleted', { type: 'link_deleted', payload: { link_id: 'lnk_009' } });

    expect(events).toEqual([
      { kind: 'deleted', link_id: '42' },
      { kind: 'deleted', link_id: 'lnk_009' },
    ]);
  });

  it('onLinkChange delivers cascade deletes (one event per link_id, no coalescing)', () => {
    // Mirrors the orphan implicit-bridge cascade: deleting one half fires
    // link_deleted for both halves in the same WS batch.
    const client = makeFakeClient();
    const stores = createLabWsStores(client);
    const events: unknown[] = [];
    stores.onLinkChange((event) => events.push(event));

    client.emit('link_deleted', { type: 'link_deleted', payload: { link_id: 'lnk_008' } });
    client.emit('link_deleted', { type: 'link_deleted', payload: { link_id: 'lnk_009' } });

    expect(events).toEqual([
      { kind: 'deleted', link_id: 'lnk_008' },
      { kind: 'deleted', link_id: 'lnk_009' },
    ]);
  });

  it('onLinkChange unsubscribe stops further events', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);
    const events: unknown[] = [];
    const unsub = stores.onLinkChange((event) => events.push(event));

    client.emit('link_deleted', { type: 'link_deleted', payload: { link_id: '1' } });
    unsub();
    client.emit('link_deleted', { type: 'link_deleted', payload: { link_id: '2' } });

    expect(events).toEqual([{ kind: 'deleted', link_id: '1' }]);
  });

  it('onLinkChange ignores malformed payloads (missing link / link_id)', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);
    const events: unknown[] = [];
    stores.onLinkChange((event) => events.push(event));

    client.emit('link_created', { type: 'link_created', payload: {} });
    client.emit('link_deleted', { type: 'link_deleted', payload: {} });
    client.emit('link_created', { type: 'link_created', payload: { link: 'not-an-object' } });

    expect(events).toEqual([]);
  });

  it('a throwing subscriber does not break other subscribers', () => {
    const client = makeFakeClient();
    const stores = createLabWsStores(client);
    const collected: unknown[] = [];
    stores.onLinkChange(() => {
      throw new Error('boom');
    });
    stores.onLinkChange((event) => collected.push(event));

    client.emit('link_deleted', { type: 'link_deleted', payload: { link_id: 'lnk_001' } });

    expect(collected).toEqual([{ kind: 'deleted', link_id: 'lnk_001' }]);
  });
});
