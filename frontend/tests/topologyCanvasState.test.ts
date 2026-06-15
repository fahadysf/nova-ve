// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { syncTopologyCanvasLocalState } from '$lib/services/topologyCanvasState';
import type { LabDefaults, Link, NetworkData, NodeData } from '$lib/types';

function node(id: number, left: number, top: number): NodeData {
  return {
    id,
    name: `node-${id}`,
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

describe('syncTopologyCanvasLocalState', () => {
  it('preserves dirty local layout when only links props change', () => {
    const staleNodes = { '1': node(1, 100, 120) };
    const staleNetworks = { '9': network(9, 360, 120) };
    const localNodes = { '1': node(1, 275, 315) };
    const localNetworks = { '9': network(9, 500, 425) };
    const defaults: LabDefaults = { link_style: 'orthogonal' };
    const topology = [];
    const links: Link[] = [];
    const nextLinks: Link[] = [
      {
        id: 'lnk_race',
        from: { node_id: 1, interface_index: 0 },
        to: { network_id: 9 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
    ];

    const result = syncTopologyCanvasLocalState(
      {
        nodes: localNodes,
        networks: localNetworks,
        topology,
        links,
        defaults,
      },
      {
        nodes: staleNodes,
        networks: staleNetworks,
        topology,
        links,
        defaults,
      },
      {
        nodes: staleNodes,
        networks: staleNetworks,
        topology,
        links: nextLinks,
        defaults,
      }
    );

    expect(result.changed).toBe(true);
    expect(result.local.links).toEqual(nextLinks);
    expect(result.local.nodes['1'].left).toBe(275);
    expect(result.local.nodes['1'].top).toBe(315);
    expect(result.local.networks['9'].left).toBe(500);
    expect(result.local.networks['9'].top).toBe(425);
  });

  it('accepts authoritative node and network prop refreshes', () => {
    const previousNodes = { '1': node(1, 100, 120) };
    const previousNetworks = { '9': network(9, 360, 120) };
    const refreshedNodes = { '1': node(1, 640, 720) };
    const refreshedNetworks = { '9': network(9, 740, 820) };
    const defaults: LabDefaults = { link_style: 'orthogonal' };
    const topology = [];
    const links: Link[] = [];

    const result = syncTopologyCanvasLocalState(
      {
        nodes: { '1': node(1, 275, 315) },
        networks: { '9': network(9, 500, 425) },
        topology,
        links,
        defaults,
      },
      {
        nodes: previousNodes,
        networks: previousNetworks,
        topology,
        links,
        defaults,
      },
      {
        nodes: refreshedNodes,
        networks: refreshedNetworks,
        topology,
        links,
        defaults,
      }
    );

    expect(result.local.nodes['1'].left).toBe(640);
    expect(result.local.nodes['1'].top).toBe(720);
    expect(result.local.networks['9'].left).toBe(740);
    expect(result.local.networks['9'].top).toBe(820);
  });
});
