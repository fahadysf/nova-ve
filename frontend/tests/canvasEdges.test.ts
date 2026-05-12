// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
  assignNetworkSlots,
  buildNetworkSlotMap,
  deriveEdges,
  parseIfaceInterfaceIndex,
  slotHandleId,
  slotPosition,
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

  it('targetHandle is network:{N}:slot:{k} unique per link into the same network', () => {
    const edges = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
    // Both links go to network_id=1 — each gets its own slot, sorted by id.
    // lnk_001 < lnk_002 → lnk_001 → slot 0, lnk_002 → slot 1.
    expect(edges[0].targetHandle).toBe('network:1:slot:0');
    expect(edges[1].targetHandle).toBe('network:1:slot:1');
    // Critically: the two links no longer collapse onto the same handle id.
    expect(edges[0].targetHandle).not.toBe(edges[1].targetHandle);
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

describe('canvasEdges slot helpers', () => {
  describe('slotHandleId', () => {
    it('formats as network:{N}:slot:{k}', () => {
      expect(slotHandleId(1, 0)).toBe('network:1:slot:0');
      expect(slotHandleId(42, 5)).toBe('network:42:slot:5');
    });
  });

  describe('slotPosition', () => {
    it('places the only slot on the right at offset 0.5', () => {
      expect(slotPosition(0, 1)).toEqual({ side: 'right', offset: 0.5 });
    });

    it('round-robins across right → bottom → left → top for low slot counts', () => {
      expect(slotPosition(0, 4)).toEqual({ side: 'right', offset: 0.5 });
      expect(slotPosition(1, 4)).toEqual({ side: 'bottom', offset: 0.5 });
      expect(slotPosition(2, 4)).toEqual({ side: 'left', offset: 0.5 });
      expect(slotPosition(3, 4)).toEqual({ side: 'top', offset: 0.5 });
    });

    it('packs extra slots evenly along the same side', () => {
      // 5 slots → right gets 2, others get 1 each.
      const p0 = slotPosition(0, 5); // right, slot-on-side 0 of 2
      const p4 = slotPosition(4, 5); // right, slot-on-side 1 of 2
      expect(p0.side).toBe('right');
      expect(p4.side).toBe('right');
      expect(p0.offset).toBeCloseTo(1 / 3, 5);
      expect(p4.offset).toBeCloseTo(2 / 3, 5);
    });

    it('falls back to right/0.5 for out-of-range slot indices', () => {
      expect(slotPosition(0, 0)).toEqual({ side: 'right', offset: 0.5 });
      expect(slotPosition(-1, 4)).toEqual({ side: 'right', offset: 0.5 });
      expect(slotPosition(10, 4)).toEqual({ side: 'right', offset: 0.5 });
    });
  });

  describe('assignNetworkSlots', () => {
    it('assigns sequential slot indices to ids sorted lexicographically', () => {
      const map = assignNetworkSlots(['lnk_002', 'lnk_001', 'lnk_003']);
      expect(map.get('lnk_001')).toBe(0);
      expect(map.get('lnk_002')).toBe(1);
      expect(map.get('lnk_003')).toBe(2);
    });

    it('keeps optimistic tmp_* ids after real ids so a freshly-created link does not bump existing slots', () => {
      const map = assignNetworkSlots(['tmp_abc', 'lnk_001', 'lnk_002']);
      expect(map.get('lnk_001')).toBe(0);
      expect(map.get('lnk_002')).toBe(1);
      expect(map.get('tmp_abc')).toBe(2);
    });

    it('returns an empty map for no inputs', () => {
      expect(assignNetworkSlots([]).size).toBe(0);
    });
  });

  describe('buildNetworkSlotMap', () => {
    it('groups links by to.network_id and assigns slots per network', () => {
      const links: Link[] = [
        { id: 'a', from: { node_id: 1, interface_index: 0 }, to: { network_id: 1 }, style_override: null, label: '', color: '', width: '1' },
        { id: 'b', from: { node_id: 2, interface_index: 0 }, to: { network_id: 1 }, style_override: null, label: '', color: '', width: '1' },
        { id: 'c', from: { node_id: 3, interface_index: 0 }, to: { network_id: 2 }, style_override: null, label: '', color: '', width: '1' },
      ];
      const slotMap = buildNetworkSlotMap(links);
      expect(slotMap.get(1)?.get('a')).toBe(0);
      expect(slotMap.get(1)?.get('b')).toBe(1);
      expect(slotMap.get(2)?.get('c')).toBe(0);
    });

    it('ignores p2p links (no to.network_id)', () => {
      const links: Link[] = [
        { id: 'p2p', from: { node_id: 1, interface_index: 0 }, to: { node_id: 2, interface_index: 0 }, style_override: null, label: '', color: '', width: '1' },
      ];
      expect(buildNetworkSlotMap(links).size).toBe(0);
    });

    it('returns an empty map for null/undefined input', () => {
      expect(buildNetworkSlotMap(null).size).toBe(0);
      expect(buildNetworkSlotMap(undefined).size).toBe(0);
    });
  });

  describe('deriveEdges slot resolution', () => {
    it('survives re-derivation with the same links — slot ids are stable', () => {
      const a = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
      const b = deriveEdges(ALPINE_DEMO_LINKS, { link_style: 'orthogonal' });
      expect(a.map((e) => e.targetHandle)).toEqual(b.map((e) => e.targetHandle));
    });

    it('discovered overlay targets the open slot of the receiving network', () => {
      const declared: Link[] = [
        { id: 'lnk_only', from: { node_id: 1, interface_index: 0 }, to: { network_id: 9 }, style_override: null, label: '', color: '', width: '1' },
      ];
      const discovered: DiscoveredLink[] = [
        { iface: 'nve00aad2i0h', bridge_name: 'noveXn9', network_id: 9, peer_node_id: 2, peer_interface_index: 0 },
      ];
      const edges = deriveEdges(declared, { link_style: 'orthogonal' }, discovered);
      // declared link → slot 0; discovered → open slot 1.
      const declaredEdge = edges.find((e) => e.id === 'link:lnk_only');
      const discoveredEdge = edges.find((e) => e.id === 'discovered:nve00aad2i0h');
      expect(declaredEdge?.targetHandle).toBe('network:9:slot:0');
      expect(discoveredEdge?.targetHandle).toBe('network:9:slot:1');
    });
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

describe('canvasEdges.deriveEdges — implicit-bridge collapse and orphan', () => {
  function implicitNetwork(id: number, overrides: Partial<import('$lib/types').Network> = {}) {
    return {
      id,
      name: '',
      type: 'linux_bridge' as const,
      left: 0,
      top: 0,
      icon: '01-Cloud-Default.svg',
      width: 0,
      style: 'Solid',
      linkstyle: 'Straight',
      color: '',
      label: '',
      visibility: false,
      implicit: true,
      smart: -1,
      config: {},
      ...overrides,
    };
  }

  it('collapses a healthy 2-half implicit bridge into a single node-to-node edge', () => {
    const links: Link[] = [
      {
        id: 'lnk_pair_a',
        from: { node_id: 3, interface_index: 1 },
        to: { network_id: 7 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
      {
        id: 'lnk_pair_b',
        from: { node_id: 6, interface_index: 0 },
        to: { network_id: 7 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
    ];
    const networks = { '7': implicitNetwork(7) };
    const edges = deriveEdges(links, { link_style: 'orthogonal' }, null, null, networks);
    expect(edges).toHaveLength(1);
    expect(edges[0].id).toBe('pair:net7');
    // Lower (node_id, iface) wins source side — node 3 / iface 1 vs node 6 / iface 0.
    expect(edges[0].source).toBe('node3');
    expect(edges[0].target).toBe('node6');
    expect(edges[0].sourceHandle).toBe('iface-1');
    expect(edges[0].targetHandle).toBe('iface-0');
    const data = edges[0].data as { implicit_bridge_network_id?: number; implicit_bridge_link_ids?: string[] };
    expect(data.implicit_bridge_network_id).toBe(7);
    expect(data.implicit_bridge_link_ids?.sort()).toEqual(['lnk_pair_a', 'lnk_pair_b']);
  });

  it('renders an orphan implicit-bridge half as an amber dashed edge to the (force-shown) network', () => {
    const links: Link[] = [
      {
        id: 'lnk_orphan',
        from: { node_id: 3, interface_index: 1 },
        to: { network_id: 7 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
    ];
    const networks = { '7': implicitNetwork(7) };
    const edges = deriveEdges(links, { link_style: 'orthogonal' }, null, null, networks);
    expect(edges).toHaveLength(1);
    expect(edges[0].id).toBe('link:lnk_orphan');
    expect(edges[0].target).toBe('network7');
    expect(edges[0].style).toContain('#fbbf24');
    expect(edges[0].style).toContain('stroke-dasharray');
    const data = edges[0].data as { orphan_implicit?: boolean };
    expect(data.orphan_implicit).toBe(true);
  });

  it('does not collapse when the network is not implicit (regular paired-via-explicit-bridge stays as two edges)', () => {
    const links: Link[] = [
      {
        id: 'lnk_a',
        from: { node_id: 3, interface_index: 1 },
        to: { network_id: 7 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
      {
        id: 'lnk_b',
        from: { node_id: 6, interface_index: 0 },
        to: { network_id: 7 },
        style_override: null,
        label: '',
        color: '',
        width: '1',
      },
    ];
    const networks = { '7': implicitNetwork(7, { implicit: false, visibility: true }) };
    const edges = deriveEdges(links, { link_style: 'orthogonal' }, null, null, networks);
    expect(edges).toHaveLength(2);
    expect(edges.map((e) => e.id).sort()).toEqual(['link:lnk_a', 'link:lnk_b']);
    for (const edge of edges) {
      expect(edge.target).toBe('network7');
    }
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
