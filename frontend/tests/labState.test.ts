// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { labState } from '$lib/stores/labState';
import type { Link, NetworkData, NodeData } from '$lib/types';

function node(id: number, left: number, top: number, name = `node-${id}`): NodeData {
  return {
    id,
    name,
    type: 'qemu',
    template: 'vyos',
    image: 'vyos',
    console: 'telnet',
    status: 0,
    delay: 0,
    cpu: 1,
    ram: 1024,
    ethernet: 1,
    left,
    top,
    icon: 'Router.png',
    interfaces: [],
  };
}

function network(id: number, left: number, top: number): NetworkData {
  return {
    id,
    name: `net-${id}`,
    type: 'bridge',
    left,
    top,
    icon: 'Cloud.png',
    visibility: true,
    count: 0,
  };
}

function link(id: string): Link {
  return {
    id,
    from: { node_id: 1, interface_index: 0 },
    to: { network_id: 9 },
    style_override: null,
    label: '',
    color: '',
    width: '1',
  };
}

describe('labState', () => {
  beforeEach(() => {
    labState.reset();
  });

  it('normalizes server snake_case second_console to secondConsole on ingest (#268)', () => {
    const serverNode = {
      ...node(1, 0, 0),
      second_console: 'vnc',
    } as NodeData & { second_console?: string | null };
    expect(serverNode.secondConsole).toBeUndefined();

    labState.applySnapshot({
      nodes: { '1': serverNode },
      networks: {},
      topology: [],
      links: [],
    });

    const stored = get(labState).nodes['1'];
    expect(stored.secondConsole).toBe('vnc');
    // Round-trip key stays in sync so wholesale PUTs carry it too.
    expect((stored as NodeData & { second_console?: string | null }).second_console).toBe('vnc');
  });

  it('keeps camelCase secondConsole authoritative and syncs the snake key', () => {
    labState.upsertNode(2, { ...node(2, 0, 0), secondConsole: 'telnet' });
    const stored = get(labState).nodes['2'];
    expect(stored.secondConsole).toBe('telnet');
    expect((stored as NodeData & { second_console?: string | null }).second_console).toBe('telnet');
  });

  it('leaves nodes without a second console untouched (identity preserved)', () => {
    const plain = node(3, 0, 0);
    labState.setNodes({ '3': plain });
    expect(get(labState).nodes['3']).toBe(plain);
  });

  it('starts empty and reset() clears adopted state', () => {
    expect(get(labState).nodes).toEqual({});
    labState.setNodes({ '1': node(1, 0, 0) });
    expect(Object.keys(get(labState).nodes)).toHaveLength(1);
    labState.reset();
    expect(get(labState).nodes).toEqual({});
    expect(get(labState).labId).toBeNull();
  });

  it('applySnapshot adopts the incoming state authoritatively', () => {
    labState.setNodes({ '1': node(1, 10, 10) });
    labState.applySnapshot({
      nodes: { '2': node(2, 20, 20, 'fresh') },
      networks: {},
      topology: [],
      links: [],
      defaults: { link_style: 'straight' },
    });

    const state = get(labState);
    expect(Object.keys(state.nodes)).toEqual(['2']);
    expect(state.nodes['2'].name).toBe('fresh');
    expect(state.defaults.link_style).toBe('straight');
  });

  it('applySnapshot keeps current positions for ids with a pending layout PUT', () => {
    labState.setNodes({ '1': node(1, 275, 315) });
    labState.setNetworks({ '9': network(9, 500, 425) });

    // Server snapshot raced the debounced save: OLD positions, NEW name.
    const raced = node(1, 100, 120, 'renamed-elsewhere');
    labState.applySnapshot(
      {
        nodes: { '1': raced },
        networks: { '9': network(9, 360, 120) },
        topology: [],
        links: [],
        defaults: { link_style: 'orthogonal' },
      },
      { nodeIds: new Set([1]), networkIds: new Set([9]) }
    );

    const state = get(labState);
    expect(state.nodes['1'].left).toBe(275);
    expect(state.nodes['1'].top).toBe(315);
    expect(state.nodes['1'].name).toBe('renamed-elsewhere');
    expect(state.networks['9'].left).toBe(500);
    expect(state.networks['9'].top).toBe(425);
  });

  it('applySnapshot does not preserve ids missing from the snapshot', () => {
    labState.setNodes({ '1': node(1, 275, 315), '2': node(2, 5, 5) });
    labState.applySnapshot(
      {
        nodes: { '2': node(2, 20, 20) },
        networks: {},
        topology: [],
        links: [],
        defaults: { link_style: 'orthogonal' },
      },
      { nodeIds: new Set([1]) }
    );

    // Node 1 is gone from the snapshot (deleted server-side) even though it
    // had a pending position; node 2 takes incoming coordinates.
    expect(get(labState).nodes['1']).toBeUndefined();
    expect(get(labState).nodes['2'].left).toBe(20);
  });

  it('field mutators replace state and notify synchronously', () => {
    const seen: number[] = [];
    const unsub = labState.subscribe((state) => seen.push(Object.keys(state.nodes).length));
    unsub();
    // (subscribe fired once with the empty state)

    labState.setNodes({ '1': node(1, 1, 1) });
    expect(Object.keys(get(labState).nodes)).toHaveLength(1);

    labState.setTopology([
      { source: 'node1', destination: 'network1', network_id: 1 },
    ]);
    expect(get(labState).topology).toHaveLength(1);
  });

  it('upsertNode / upsertLink / removeLink patch single entries', () => {
    labState.setNodes({ '1': node(1, 1, 1) });
    labState.upsertNode(1, node(1, 9, 9, 'moved'));
    expect(get(labState).nodes['1'].name).toBe('moved');
    expect(Object.keys(get(labState).nodes)).toHaveLength(1);

    const first = link('lnk-1');
    const second = link('lnk-2');
    labState.setLinks([first]);
    labState.upsertLink(second);
    expect(get(labState).links).toHaveLength(2);

    const replacedFirst = { ...first, label: 'updated' };
    labState.upsertLink(replacedFirst);
    expect(get(labState).links).toHaveLength(2);
    expect(get(labState).links[0].label).toBe('updated');

    labState.removeLink('lnk-1');
    expect(get(labState).links.map((entry) => entry.id)).toEqual(['lnk-2']);
  });

  it('setLabId is idempotent for the same lab', () => {
    labState.setLabId('demo.json');
    const before = get(labState);
    labState.setLabId('demo.json');
    expect(get(labState)).toBe(before); // same reference — no spurious notify
    labState.setLabId('other.json');
    expect(get(labState).labId).toBe('other.json');
  });

  it('applySnapshot adopts meta.revision and setRevision updates state + meta (#245)', () => {
    labState.applySnapshot({
      meta: {
        id: 'demo',
        name: 'demo',
        filename: 'demo.json',
        path: '/demo.json',
        owner: 'admin',
        author: '',
        description: '',
        version: '4',
        grid: false,
        lock: false,
        revision: 7,
      },
      nodes: {},
      networks: {},
      topology: [],
      links: [],
    });
    expect(get(labState).revision).toBe(7);

    // Post-write revision from a PUT response lands in both slots.
    labState.setRevision(8);
    const state = get(labState);
    expect(state.revision).toBe(8);
    expect(state.meta?.revision).toBe(8);

    // Snapshots without a numeric revision keep the current token.
    labState.applySnapshot({
      nodes: {},
      networks: {},
      topology: [],
      links: [],
    });
    expect(get(labState).revision).toBe(8);
  });

  it('applyDragStop adopts rounded positions for nodes and networks', () => {
    labState.applySnapshot({
      nodes: { '1': node(1, 10, 20) },
      networks: { '9': network(9, 30, 40) },
      topology: [],
      links: [],
    });

    // #245 regression: the component previously assigned through
    // `$labState.nodes[id].left = v`, which Svelte 5 legacy compilation
    // turned into `store.set($store, …)` — throwing `e.set is not a
    // function` on every drag stop and killing all layout saves.
    expect(labState.applyDragStop('node', 1, 121, 80)).toBe(true);
    expect(labState.applyDragStop('network', 9, 10, 21)).toBe(true);

    const state = get(labState);
    expect(state.nodes['1'].left).toBe(121);
    expect(state.nodes['1'].top).toBe(80);
    expect(state.networks['9'].left).toBe(10);
    expect(state.networks['9'].top).toBe(21);
  });

  it('identical drag positions neither write state nor notify subscribers', () => {
    labState.applySnapshot({
      nodes: { '1': node(1, 10, 20) },
      networks: { '9': network(9, 30, 40) },
      topology: [],
      links: [],
    });
    const original = get(labState);
    let updates = 0;
    const unsubscribe = labState.subscribe(() => { updates += 1; });
    try {
      expect(labState.applyDragStop('node', 1, 10, 20)).toBe(false);
      expect(labState.applyDragStop('network', 9, 30, 40)).toBe(false);
      expect(get(labState)).toBe(original);
      expect(updates).toBe(1);
    } finally {
      unsubscribe();
    }
  });

  it('applyDragStop reports false for unknown elements without changing state', () => {
    labState.applySnapshot({
      nodes: { '1': node(1, 10, 20) },
      networks: {},
      topology: [],
      links: [],
    });

    expect(labState.applyDragStop('node', 99, 5, 5)).toBe(false);
    expect(labState.applyDragStop('network', 1, 5, 5)).toBe(false); // wrong kind

    const state = get(labState);
    expect(state.nodes['1'].left).toBe(10);
    expect(state.networks).toEqual({});
  });
});

describe('labState link adoption (#306)', () => {
  beforeEach(() => {
    labState.reset();
  });

  function nodeWithInterfaces(id: number, count: number): NodeData {
    return {
      ...node(id, 0, 0),
      interfaces: Array.from({ length: count }, (_, index) => ({
        index,
        name: `eth${index}`,
        network_id: 0,
        planned_mac: null,
        port_position: { side: 'right' as const, offset: 0.5 }
      }))
    };
  }

  function linkBetween(id: string, from: Link['from'], to: Link['to']): Link {
    return { id, from, to, style_override: null, label: '', color: '', width: '1' };
  }

  const toNetwork = { network_id: 9 };
  const bridge: NetworkData = {
    id: 21,
    name: 'net-21',
    type: 'bridge',
    left: 0,
    top: 0,
    icon: 'Cloud.png',
    visibility: false,
    implicit: true,
    count: 2
  };

  it('upsertLink replaces the entry with the same id instead of adding a second one', () => {
    labState.setLinks([linkBetween('lnk_1', { node_id: 1, interface_index: 0 }, toNetwork)]);

    labState.upsertLink(linkBetween('lnk_1', { node_id: 1, interface_index: 0 }, { network_id: 9 }));

    expect(get(labState).links.map((entry) => entry.id)).toEqual(['lnk_1']);
    expect(get(labState).links[0].to).toEqual({ network_id: 9 });
  });

  it('upsertLink retires an optimistic placeholder between the same endpoints', () => {
    const placeholder = linkBetween('tmp_key', { node_id: 1, interface_index: 1 }, toNetwork);
    const other = linkBetween('lnk_1', { node_id: 1, interface_index: 0 }, toNetwork);
    labState.setLinks([placeholder, other]);

    labState.upsertLink(linkBetween('lnk_101', { node_id: 1, interface_index: 1 }, toNetwork));

    expect(get(labState).links.map((entry) => entry.id)).toEqual(['lnk_1', 'lnk_101']);
  });

  it('upsertLink keeps a placeholder that joins different endpoints', () => {
    labState.setLinks([linkBetween('tmp_key', { node_id: 2, interface_index: 0 }, toNetwork)]);

    labState.upsertLink(linkBetween('lnk_101', { node_id: 1, interface_index: 1 }, toNetwork));

    expect(get(labState).links.map((entry) => entry.id)).toEqual(['tmp_key', 'lnk_101']);
  });

  it('upsertLink attaches the node-side interface to the network the link joins', () => {
    labState.setNodes({ '1': nodeWithInterfaces(1, 2) });

    labState.upsertLink(linkBetween('lnk_101', { node_id: 1, interface_index: 1 }, toNetwork));

    const interfaces = get(labState).nodes['1'].interfaces;
    expect(interfaces[1].network_id).toBe(9);
    expect(interfaces[0].network_id).toBe(0);
  });

  it('applyCreatedLink swaps the placeholder for the created link and adopts the bridge', () => {
    labState.setLinks([
      linkBetween('tmp_key', { node_id: 1, interface_index: 1 }, { node_id: 2, interface_index: 0 })
    ]);
    labState.setNodes({ '1': nodeWithInterfaces(1, 2) });
    const first = linkBetween('lnk_201', { node_id: 1, interface_index: 1 }, { network_id: 21 });

    labState.applyCreatedLink(first, bridge, 'tmp_key');

    const state = get(labState);
    expect(state.links.map((entry) => entry.id)).toEqual(['lnk_201']);
    expect(state.networks['21']).toEqual(bridge);
    expect(state.nodes['1'].interfaces[1].network_id).toBe(21);
  });

  it('applyCreatedLink keeps a copy the WebSocket already delivered single', () => {
    const first = linkBetween('lnk_201', { node_id: 1, interface_index: 1 }, { network_id: 21 });
    labState.setLinks([
      linkBetween('tmp_key', { node_id: 1, interface_index: 1 }, { node_id: 2, interface_index: 0 }),
      first
    ]);

    labState.applyCreatedLink(first, bridge, 'tmp_key');

    const state = get(labState);
    expect(state.links.map((entry) => entry.id)).toEqual(['lnk_201']);
    expect(state.networks['21']).toEqual(bridge);
  });

  it('applyCreatedLink without a network leaves the networks alone', () => {
    labState.setNetworks({ '9': network(9, 0, 0) });
    labState.setLinks([linkBetween('tmp_key', { node_id: 1, interface_index: 0 }, toNetwork)]);

    labState.applyCreatedLink(
      linkBetween('lnk_101', { node_id: 1, interface_index: 0 }, toNetwork),
      null,
      'tmp_key'
    );

    expect(get(labState).links.map((entry) => entry.id)).toEqual(['lnk_101']);
    expect(Object.keys(get(labState).networks)).toEqual(['9']);
  });
});
