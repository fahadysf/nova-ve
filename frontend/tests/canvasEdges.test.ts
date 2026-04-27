// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { deriveEdges } from '$lib/services/canvasEdges';
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
});
