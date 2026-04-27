// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { Edge } from '@xyflow/svelte';
import type { Link, LinkStyle } from '$lib/types';

export interface DeriveEdgesDefaults {
  link_style?: LinkStyle;
}

export function deriveEdges(
  links: Link[] | null | undefined,
  defaults: DeriveEdgesDefaults | null | undefined = null
): Edge[] {
  if (!links || links.length === 0) {
    return [];
  }

  const defaultStyle: LinkStyle = defaults?.link_style ?? 'orthogonal';
  const edges: Edge[] = [];

  for (const link of links) {
    if (!link || !link.id) continue;

    const fromNodeId = link.from?.node_id;
    if (typeof fromNodeId !== 'number') continue;

    let target: string | null = null;
    if (typeof link.to?.network_id === 'number') {
      target = `network${link.to.network_id}`;
    } else if (typeof link.to?.node_id === 'number') {
      target = `node${link.to.node_id}`;
    }
    if (!target) continue;

    const widthValue = parseInt(link.width ?? '1', 10);
    const strokeWidth = Number.isFinite(widthValue) && widthValue > 0 ? widthValue : 1;
    const strokeColor = link.color && link.color.length > 0 ? link.color : '#9ca3af';

    const edge: Edge = {
      id: `link:${link.id}`,
      source: `node${fromNodeId}`,
      target,
      type: 'link',
      animated: false,
      label: link.label && link.label.length > 0 ? link.label : undefined,
      style: `stroke: ${strokeColor}; stroke-width: ${strokeWidth}px;`,
      labelStyle: 'fill: #d1d5db; font-size: 10px; font-family: var(--font-mono);',
      data: {
        style_override: link.style_override ?? null,
        default_style: defaultStyle,
        label: link.label ?? '',
        color: link.color ?? '',
        width: link.width ?? '1',
      },
    };

    edges.push(edge);
  }

  return edges;
}
