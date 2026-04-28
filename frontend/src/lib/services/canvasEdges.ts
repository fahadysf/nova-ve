// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { Edge } from '@xyflow/svelte';
import type { Link, LinkStyle } from '$lib/types';

export interface DeriveEdgesDefaults {
  link_style?: LinkStyle;
}

/**
 * Pick which side handle of a NetworkNode to connect to.
 *
 * When node and network positions are available the caller can supply them;
 * otherwise we fall back to the deterministic default `'right'`.
 *
 * The network node exposes four handles: `network:{N}:top`, `network:{N}:right`,
 * `network:{N}:bottom`, `network:{N}:left`.  We choose the side whose midpoint
 * is geometrically closest to the connecting node.  When positions are absent
 * the default side is `'right'` (deterministic, stable across rerenders).
 */
export function pickNetworkSide(
  networkId: number,
  nodePos?: { x: number; y: number } | null,
  networkPos?: { x: number; y: number } | null
): string {
  if (nodePos && networkPos) {
    const dx = nodePos.x - networkPos.x;
    const dy = nodePos.y - networkPos.y;
    const absDx = Math.abs(dx);
    const absDy = Math.abs(dy);
    let side: string;
    if (absDx >= absDy) {
      side = dx >= 0 ? 'left' : 'right';
    } else {
      side = dy >= 0 ? 'top' : 'bottom';
    }
    return `network:${networkId}:${side}`;
  }
  return `network:${networkId}:right`;
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

    const fromInterfaceIndex = link.from?.interface_index;

    let target: string | null = null;
    let targetHandle: string | null = null;

    if (typeof link.to?.network_id === 'number') {
      target = `network${link.to.network_id}`;
      targetHandle = pickNetworkSide(link.to.network_id);
    } else if (typeof link.to?.node_id === 'number') {
      target = `node${link.to.node_id}`;
      const toIfaceIndex = link.to.interface_index;
      targetHandle =
        typeof toIfaceIndex === 'number' ? `iface-${toIfaceIndex}` : 'default';
    }
    if (!target) continue;

    const sourceHandle =
      typeof fromInterfaceIndex === 'number' ? `iface-${fromInterfaceIndex}` : 'default';

    if (!sourceHandle || !targetHandle) {
      throw new Error(
        `[canvasEdges] link "${link.id}" produced a missing handle: ` +
          `sourceHandle=${JSON.stringify(sourceHandle)} targetHandle=${JSON.stringify(targetHandle)}`
      );
    }

    const widthValue = parseInt(link.width ?? '1', 10);
    const strokeWidth = Number.isFinite(widthValue) && widthValue > 0 ? widthValue : 1;
    const strokeColor = link.color && link.color.length > 0 ? link.color : '#9ca3af';

    const edge: Edge = {
      id: `link:${link.id}`,
      source: `node${fromNodeId}`,
      target,
      sourceHandle,
      targetHandle,
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
