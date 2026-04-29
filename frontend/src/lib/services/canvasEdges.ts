// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { Edge } from '@xyflow/svelte';
import type { Link, LinkReconciliation, LinkStyle } from '$lib/types';

export interface DeriveEdgesDefaults {
  link_style?: LinkStyle;
}

/**
 * US-403: an inferred (kernel-only) link discovered by the backend
 * ``_discovery_loop`` and surfaced via the ``discovered_link`` WS event.
 * Carries enough context to render a dashed-amber overlay edge and, on
 * promotion, POST a real link to ``/labs/{labId}/links``.
 *
 * This is the shape passed as the 3rd arg to ``deriveEdges``; it is derived
 * from the ``linkReconciliation`` store entries with ``kind === 'discovered'``.
 */
export interface DiscoveredLink {
  /** Stable key — the kernel iface name (``nve{hash}d{node}i{iface}[h|p]``). */
  iface: string;
  /** Bridge backing the network the iface is attached to. */
  bridge_name: string;
  /** Network id the iface is bridged into. */
  network_id: number;
  /** Node id decoded from the iface name (``null`` when unparseable). */
  peer_node_id: number | null;
  /** Interface index decoded from the iface name (``null`` when unparseable). */
  peer_interface_index?: number | null;
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

/**
 * Derive SvelteFlow edges from:
 *  1. ``links``           — declared links from ``lab.json`` (source of truth).
 *  2. ``discoveredLinks`` — kernel-only ifaces not in ``links[]`` (US-403);
 *                           rendered as dashed amber overlay edges.
 *  3. ``divergentByLinkId`` — map of link_id → {@link LinkReconciliation} for
 *                           links declared in ``links[]`` but absent in the
 *                           kernel (US-404); rendered as red dashed overlay.
 *
 * The divergent and discovered branches are visually distinct and independent.
 * Overlay state is observation-only runtime data — it is never attached to the
 * ``Link`` type itself (see ``LinkReconciliation`` in types/index.ts).
 */
export function deriveEdges(
  links: Link[] | null | undefined,
  defaults: DeriveEdgesDefaults | null | undefined = null,
  discoveredLinks: DiscoveredLink[] | null | undefined = null,
  divergentByLinkId: Record<string, LinkReconciliation> | null | undefined = null
): Edge[] {
  const hasLinks = !!(links && links.length > 0);
  const hasDiscovered = !!(discoveredLinks && discoveredLinks.length > 0);
  if (!hasLinks && !hasDiscovered) {
    return [];
  }

  const defaultStyle: LinkStyle = defaults?.link_style ?? 'orthogonal';
  const edges: Edge[] = [];

  if (hasLinks) for (const link of links!) {
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

    // US-404: divergent overlay — declared but kernel has no matching veth/TAP.
    // Decoration comes from the divergentByLinkId param (never from Link itself).
    const divEntry = divergentByLinkId?.[link.id] ?? null;
    const isDivergent = divEntry !== null;
    const edgeStyle = isDivergent
      ? 'stroke: #f87171; stroke-dasharray: 2 2; opacity: 0.7;'
      : `stroke: ${strokeColor}; stroke-width: ${strokeWidth}px;`;

    edges.push({
      id: `link:${link.id}`,
      source: `node${fromNodeId}`,
      target,
      sourceHandle,
      targetHandle,
      type: 'link',
      animated: false,
      label: link.label && link.label.length > 0 ? link.label : undefined,
      style: edgeStyle,
      labelStyle: 'fill: #d1d5db; font-size: 10px; font-family: var(--font-mono);',
      data: {
        style_override: link.style_override ?? null,
        default_style: defaultStyle,
        label: link.label ?? '',
        color: link.color ?? '',
        width: link.width ?? '1',
        divergent: isDivergent,
        last_checked: divEntry?.last_checked ?? null,
      },
    });
  }

  // US-403: discovered (kernel-only) overlay edges. Rendered with a dashed
  // amber stroke. Right-click → "Promote to declared link" lifts these into
  // real ``links[]`` entries via POST /labs/{id}/links.
  if (hasDiscovered) for (const discovered of discoveredLinks!) {
    if (!discovered || !discovered.iface) continue;
    if (typeof discovered.peer_node_id !== 'number') continue;
    if (typeof discovered.network_id !== 'number') continue;

    const fromNodeId = discovered.peer_node_id;
    const networkId = discovered.network_id;
    const ifaceIndex = discovered.peer_interface_index;
    const sourceHandle =
      typeof ifaceIndex === 'number' ? `iface-${ifaceIndex}` : 'default';
    const targetHandle = pickNetworkSide(networkId);

    edges.push({
      id: `discovered:${discovered.iface}`,
      source: `node${fromNodeId}`,
      target: `network${networkId}`,
      sourceHandle,
      targetHandle,
      type: 'link',
      animated: false,
      style: 'stroke: #fbbf24; stroke-dasharray: 6 4;',
      labelStyle: 'fill: #fbbf24; font-size: 10px; font-family: var(--font-mono);',
      data: {
        style_override: null,
        default_style: defaultStyle,
        label: '',
        color: '#fbbf24',
        width: '1',
        discovered: true,
        discovered_iface: discovered.iface,
        discovered_bridge: discovered.bridge_name,
        discovered_network_id: networkId,
        discovered_peer_node_id: fromNodeId,
        discovered_peer_interface_index:
          typeof ifaceIndex === 'number' ? ifaceIndex : null,
      },
    });
  }

  return edges;
}

/**
 * Decode the ``interface_index`` from a nova-ve host iface name like
 * ``nve{4hex}d{node}i{iface}[h|p]``.  Returns ``null`` if the name does not
 * follow the scheme — caller should fall back to the ``default`` handle.
 */
export function parseIfaceInterfaceIndex(iface: string): number | null {
  if (!iface || !iface.startsWith('nve')) return null;
  const dIdx = iface.indexOf('d');
  if (dIdx < 0) return null;
  const after_d = iface.slice(dIdx + 1);
  const iIdx = after_d.indexOf('i');
  if (iIdx < 0) return null;
  const after_i = after_d.slice(iIdx + 1);
  // Trailing suffix may be ``h`` (host) or ``p`` (peer); strip non-digits.
  let digits = '';
  for (const ch of after_i) {
    if (ch >= '0' && ch <= '9') digits += ch;
    else break;
  }
  if (digits.length === 0) return null;
  const parsed = parseInt(digits, 10);
  return Number.isFinite(parsed) ? parsed : null;
}
