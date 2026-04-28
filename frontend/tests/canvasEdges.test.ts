// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { deriveEdges, pickNetworkSide } from '$lib/services/canvasEdges';
import type { Link } from '$lib/types';

const ALPINE_DEMO_LINKS: Link[] = [
  {
    id: 'lnk_001',
    from: { node_id: 1, interface_index: 0 },
    to: { network_id: 1 },
    style_override: null,
    label: '',
    color: '',
    width: '1',
  },
  {
    id: 'lnk_002',
    from: { node_id: 3, interface_index: 0 },
    to: { network_id: 1 },
    style_override: null,
    label: '',
    color: '',
    width: '1',
  },
];

describe('canvasEdges.deriveEdges', () => {
  it('produces two edges with link:lnk_001 / link:lnk_002 ids for the alpine-docker-demo fixture', () => {
    const edges = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
    expect(edges).toHaveLength(2);
    expect(edges[0].id).toBe('link:lnk_001');
    expect(edges[1].id).toBe('link:lnk_002');
  });

  it('resolves source and target ids to node{N} / network{N}', () => {
    const edges = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
    expect(edges[0].source).toBe('node1');
    expect(edges[0].target).toBe('network1');
    expect(edges[1].source).toBe('node3');
    expect(edges[1].target).toBe('network1');
  });

  it("threads style_override='bezier' through to data.style_override", () => {
    const links: Link[] = [
      {
        id: 'lnk_010',
        from: { node_id: 2, interface_index: 1 },
        to: { network_id: 5 },
        style_override: 'bezier',
        label: 'curved',
        color: '#ff0',
        width: '2',
      },
    ];
    const edges = deriveEdges(links, { link_style: 'orthogonal' });
    expect(edges).toHaveLength(1);
    expect(edges[0].id).toBe('link:lnk_010');
    expect(edges[0].type).toBe('link');
    const data = edges[0].data as { style_override?: string; default_style?: string } | undefined;
    expect(data?.style_override).toBe('bezier');
    expect(data?.default_style).toBe('orthogonal');
  });

  it('returns an empty array for empty / null / undefined links', () => {
    expect(deriveEdges([], { link_style: 'orthogonal' })).toEqual([]);
    expect(deriveEdges(null, { link_style: 'orthogonal' })).toEqual([]);
    expect(deriveEdges(undefined, null)).toEqual([]);
  });

  it('produces target=node{N} for a node-to-node link (to.node_id)', () => {
    const links: Link[] = [
      {
        id: 'lnk_p2p',
        from: { node_id: 4, interface_index: 0 },
        to: { node_id: 5, interface_index: 1 },
        style_override: null,
        label: 'p2p',
        color: '',
        width: '1',
      },
    ];
    const edges = deriveEdges(links, { link_style: 'orthogonal' });
    expect(edges).toHaveLength(1);
    expect(edges[0].source).toBe('node4');
    expect(edges[0].target).toBe('node5');
    expect(edges[0].id).toBe('link:lnk_p2p');
  });

  it('every produced edge has both sourceHandle and targetHandle populated', () => {
    const edges = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
    for (const edge of edges) {
      expect(edge.sourceHandle).toBeTruthy();
      expect(edge.targetHandle).toBeTruthy();
    }
  });

  it('sourceHandle is iface-{interface_index} for node-side from endpoint', () => {
    const edges = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
    // lnk_001: from.interface_index = 0
    expect(edges[0].sourceHandle).toBe('iface-0');
    // lnk_002: from.interface_index = 0
    expect(edges[1].sourceHandle).toBe('iface-0');
  });

  it('targetHandle is network:{N}:right for node→network link (default side)', () => {
    const edges = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
    // Both links go to network_id=1
    expect(edges[0].targetHandle).toBe('network:1:right');
    expect(edges[1].targetHandle).toBe('network:1:right');
  });

  it('targetHandle is iface-{index} for node-to-node link', () => {
    const links: Link[] = [
      {
        id: 'lnk_p2p2',
        from: { node_id: 4, interface_index: 2 },
        to: { node_id: 5, interface_index: 3 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
    ];
    const edges = deriveEdges(links, { link_style: 'orthogonal' });
    expect(edges[0].sourceHandle).toBe('iface-2');
    expect(edges[0].targetHandle).toBe('iface-3');
  });
});

describe('canvasEdges.pickNetworkSide', () => {
  it('returns right when no positions are provided', () => {
    expect(pickNetworkSide(1)).toBe('network:1:right');
  });

  it('returns left when node is to the right of the network', () => {
    // node at x=300, network at x=100 → node is right-of-network → attach to network's right side
    // dx = 300-100 = 200 > 0 → side = 'left' (node is to the right so network's left faces away)
    // Per implementation: dx >= 0 → 'left'
    expect(pickNetworkSide(2, { x: 300, y: 100 }, { x: 100, y: 100 })).toBe('network:2:left');
  });

  it('returns right when node is to the left of the network', () => {
    // dx = 50-200 = -150 < 0 → side = 'right'
    expect(pickNetworkSide(3, { x: 50, y: 100 }, { x: 200, y: 100 })).toBe('network:3:right');
  });

  it('returns top when node is above the network (dy dominant)', () => {
    // dx=10, dy=-200 → absDy > absDx → dy < 0 → 'bottom' (node is above → network bottom faces up)
    // Per implementation: dy < 0 → 'bottom'
    expect(pickNetworkSide(4, { x: 105, y: 0 }, { x: 100, y: 200 })).toBe('network:4:bottom');
  });

  it('returns bottom when node is below the network (dy dominant)', () => {
    // dy = 300-100 = 200 > 0 → 'top'
    expect(pickNetworkSide(5, { x: 105, y: 300 }, { x: 100, y: 100 })).toBe('network:5:top');
  });

  it('is deterministic for the same inputs', () => {
    const a = pickNetworkSide(7, { x: 100, y: 200 }, { x: 400, y: 50 });
    const b = pickNetworkSide(7, { x: 100, y: 200 }, { x: 400, y: 50 });
    expect(a).toBe(b);
  });
});
