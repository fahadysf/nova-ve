// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
  deriveEdges,
  parseIfaceInterfaceIndex,
  pickNetworkSide,
  type DiscoveredLink,
} from '$lib/services/canvasEdges';
import type { Link, LinkReconciliation } from '$lib/types';

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

// ──────────────────────────────────────────────────────────────────────────
// US-403 / US-404 — overlay branches.
//
// Overlay state is no longer carried on Link fields (removed in the
// unification refactor).  Instead:
//   - divergent entries arrive via the 4th arg ``divergentByLinkId``.
//   - discovered entries arrive via the 3rd arg ``DiscoveredLink[]``.
// ──────────────────────────────────────────────────────────────────────────

describe('canvasEdges.deriveEdges — overlay branches (US-403 / US-404)', () => {
  const BASE_LINK: Link = {
    id: 'lnk_base',
    from: { node_id: 1, interface_index: 0 },
    to: { network_id: 1 },
    style_override: null,
    label: '',
    color: '',
    width: '1',
  };

  const DIVERGENT_RECON: LinkReconciliation = {
    kind: 'divergent',
    key: 'link:lnk_base',
    link_id: 'lnk_base',
    last_checked: '2026-04-28T12:34:56+00:00',
    reason: 'no veth found',
  };

  it('renders a divergent link with the red dashed overlay when divergentByLinkId is set', () => {
    const edges = deriveEdges(
      [BASE_LINK],
      { link_style: 'orthogonal' },
      null,
      { [DIVERGENT_RECON.link_id!]: DIVERGENT_RECON }
    );
    expect(edges).toHaveLength(1);
    const edge = edges[0];
    expect(edge.style).toContain('stroke: #f87171');
    expect(edge.style).toContain('stroke-dasharray: 2 2');
    expect(edge.style).toContain('opacity: 0.7');
    const data = edge.data as { divergent?: boolean; last_checked?: string | null };
    expect(data.divergent).toBe(true);
    expect(data.last_checked).toBe('2026-04-28T12:34:56+00:00');
  });

  it('plain links (no divergent entry) keep the default stroke', () => {
    const edges = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
    for (const edge of edges) {
      expect(edge.style).toContain('stroke: #9ca3af');
      expect(edge.style).not.toContain('stroke-dasharray');
    }
  });

  it('threads last_checked through edge.data for the LinkEdge tooltip', () => {
    const edges = deriveEdges(
      [BASE_LINK],
      { link_style: 'orthogonal' },
      null,
      { [DIVERGENT_RECON.link_id!]: DIVERGENT_RECON }
    );
    const data = edges[0].data as { last_checked?: string | null };
    expect(data.last_checked).toBe('2026-04-28T12:34:56+00:00');
  });

  it('divergent and discovered render with visually distinct strokes', () => {
    // divergent: declared link with divergentByLinkId entry → red dashed
    const declaredLink: Link = { ...BASE_LINK, id: 'lnk_div' };
    // discovered: kernel-only iface → amber dashed via 3rd arg
    const discoveredEntry: DiscoveredLink = {
      iface: 'nve12abd1i0h',
      bridge_name: 'noveXn1',
      network_id: 1,
      peer_node_id: 2,
      peer_interface_index: 0,
    };
    const edges = deriveEdges(
      [declaredLink],
      { link_style: 'orthogonal' },
      [discoveredEntry],
      { lnk_div: { ...DIVERGENT_RECON, link_id: 'lnk_div', key: 'link:lnk_div' } }
    );
    expect(edges).toHaveLength(2);
    // Divergent edge (declared link)
    expect(edges[0].id).toBe('link:lnk_div');
    expect(edges[0].style).toContain('#f87171');
    expect(edges[0].style).toContain('stroke-dasharray: 2 2');
    // Discovered edge (iface overlay)
    expect(edges[1].id).toBe('discovered:nve12abd1i0h');
    expect(edges[1].style).toContain('#fbbf24');
    expect(edges[1].style).toContain('stroke-dasharray: 6 4');
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

// ──────────────────────────────────────────────────────────────────────────
// US-403 — discovered (kernel-only) link overlay edges.
//
// NOTE: US-404 will append a parallel divergent-link fixture and the file
// is shared between the two stories.  Keep the discovered-overlay describe
// block self-contained so the next merge can append cleanly.
// ──────────────────────────────────────────────────────────────────────────

describe('canvasEdges.deriveEdges (discovered overlay)', () => {
  const DISCOVERED: DiscoveredLink[] = [
    {
      iface: 'nve12abd7i2h',
      bridge_name: 'noveXn5',
      network_id: 5,
      peer_node_id: 7,
      peer_interface_index: 2,
    },
  ];

  it('produces a discovered:{iface} edge with dashed amber styling', () => {
    const edges = deriveEdges([], { link_style: 'orthogonal' }, DISCOVERED);
    expect(edges).toHaveLength(1);
    expect(edges[0].id).toBe('discovered:nve12abd7i2h');
    expect(edges[0].source).toBe('node7');
    expect(edges[0].target).toBe('network5');
    expect(edges[0].style).toBe('stroke: #fbbf24; stroke-dasharray: 6 4;');
    const data = edges[0].data as { discovered?: boolean; discovered_iface?: string } | undefined;
    expect(data?.discovered).toBe(true);
    expect(data?.discovered_iface).toBe('nve12abd7i2h');
  });

  it('threads decoded interface_index into sourceHandle (iface-{N})', () => {
    const edges = deriveEdges([], { link_style: 'orthogonal' }, DISCOVERED);
    expect(edges[0].sourceHandle).toBe('iface-2');
  });

  it('falls back to the default sourceHandle when interface_index is unknown', () => {
    const edges = deriveEdges(
      [],
      { link_style: 'orthogonal' },
      [{ ...DISCOVERED[0], peer_interface_index: null }],
    );
    expect(edges[0].sourceHandle).toBe('default');
  });

  it('skips entries with no peer_node_id', () => {
    const edges = deriveEdges(
      [],
      { link_style: 'orthogonal' },
      [{ ...DISCOVERED[0], peer_node_id: null }],
    );
    expect(edges).toHaveLength(0);
  });

  it('renders alongside declared links without disturbing them', () => {
    const declared: Link[] = [
      {
        id: 'lnk_001',
        from: { node_id: 1, interface_index: 0 },
        to: { network_id: 5 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
    ];
    const edges = deriveEdges(declared, { link_style: 'orthogonal' }, DISCOVERED);
    expect(edges).toHaveLength(2);
    expect(edges[0].id).toBe('link:lnk_001');
    expect(edges[0].style).toBe('stroke: #9ca3af; stroke-width: 1px;');
    expect(edges[1].id).toBe('discovered:nve12abd7i2h');
    expect(edges[1].style).toBe('stroke: #fbbf24; stroke-dasharray: 6 4;');
  });

  it('returns [] when both inputs are empty', () => {
    expect(deriveEdges([], { link_style: 'orthogonal' }, [])).toEqual([]);
    expect(deriveEdges(null, null, null)).toEqual([]);
  });
});

describe('canvasEdges.parseIfaceInterfaceIndex', () => {
  it('decodes the interface index from nve{hash}d{node}i{iface}h', () => {
    expect(parseIfaceInterfaceIndex('nve12abd7i2h')).toBe(2);
  });

  it('decodes multi-digit interface indices', () => {
    expect(parseIfaceInterfaceIndex('nve12abd7i12p')).toBe(12);
  });

  it('returns null for non-nova-ve iface names', () => {
    expect(parseIfaceInterfaceIndex('eth0')).toBeNull();
    expect(parseIfaceInterfaceIndex('veth9876')).toBeNull();
    expect(parseIfaceInterfaceIndex('')).toBeNull();
  });

  it('returns null when the iface form is malformed', () => {
    expect(parseIfaceInterfaceIndex('nve12abXX')).toBeNull();
    expect(parseIfaceInterfaceIndex('nveXi3')).toBeNull();
  });
});
