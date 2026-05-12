// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { Edge } from '@xyflow/svelte';
import type { Link, LinkReconciliation, LinkStyle } from '$lib/types';

export interface DeriveEdgesDefaults {
  link_style?: LinkStyle;
}

/**
 * Structural shape ``deriveEdges`` needs to tell an implicit bridge from a
 * promoted/explicit one. Stays compatible with both the v2 ``Network`` and
 * the v1 ``NetworkData`` types used by the canvas — we only need
 * ``implicit`` (truthy when the bridge is paired-create scaffolding) and
 * ``visibility`` (falsy when the bridge is hidden on the canvas).
 */
export interface NetworkShape {
  implicit?: boolean;
  visibility: boolean | number;
}

/**
 * Networks that are flagged ``implicit: true`` are kept invisible on the
 * canvas (the paired-create UX hides the bridge node and renders the two
 * link halves as a single visual edge between the two endpoints). When the
 * data is healthy this is exactly two declared links to the bridge; we
 * collapse them into one edge. When the data is broken — orphan half left
 * over after a peer was repointed — we still need to surface the link, so
 * the orphan half renders with an amber warning stroke to whichever target
 * the regular branch produces. ``TopologyCanvas`` force-shows broken
 * implicit networks so the orphan has a node to point at.
 */
function _buildImplicitNetMeta(
  links: readonly Link[],
  networks: Readonly<Record<string, NetworkShape>> | null | undefined
): {
  pairsByNet: Map<number, Link[]>;
  isImplicit: (networkId: number) => boolean;
} {
  const pairsByNet = new Map<number, Link[]>();
  const isImplicit = (networkId: number): boolean => {
    if (!networks) return false;
    const network = networks[String(networkId)];
    return !!network && network.implicit === true && !network.visibility;
  };
  for (const link of links) {
    if (!link?.id) continue;
    const networkId = link.to?.network_id;
    if (typeof networkId !== 'number') continue;
    if (!isImplicit(networkId)) continue;
    if (!pairsByNet.has(networkId)) pairsByNet.set(networkId, []);
    pairsByNet.get(networkId)!.push(link);
  }
  return { pairsByNet, isImplicit };
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

export type NetworkSide = 'top' | 'right' | 'bottom' | 'left';

export interface NetworkSlotPlacement {
  side: NetworkSide;
  offset: number;
}

const SLOT_SIDE_ORDER: readonly NetworkSide[] = ['right', 'bottom', 'left', 'top'];

/**
 * Place slot ``slotIdx`` (0-indexed) for a network rendering ``totalSlots``
 * connection points. Slots round-robin across the four sides starting at
 * ``right``; offsets along each side are evenly spaced for the slots that
 * share it.
 *
 * Examples:
 *   slotPosition(0, 1) → { side: 'right',  offset: 0.5 }
 *   slotPosition(1, 4) → { side: 'bottom', offset: 0.5 }
 *   slotPosition(0, 5) → { side: 'right',  offset: 0.333 }
 *   slotPosition(4, 5) → { side: 'right',  offset: 0.667 }
 */
export function slotPosition(slotIdx: number, totalSlots: number): NetworkSlotPlacement {
  if (totalSlots <= 0 || slotIdx < 0 || slotIdx >= totalSlots) {
    return { side: 'right', offset: 0.5 };
  }
  const sideIdx = slotIdx % 4;
  const slotOnSide = Math.floor(slotIdx / 4);
  const base = Math.floor(totalSlots / 4);
  const extra = totalSlots % 4;
  const slotsOnThisSide = sideIdx < extra ? base + 1 : base;
  const offset = (slotOnSide + 1) / (slotsOnThisSide + 1);
  return { side: SLOT_SIDE_ORDER[sideIdx], offset };
}

/**
 * Stable handle id for a per-link slot on a network node.
 */
export function slotHandleId(networkId: number, slotIdx: number): string {
  return `network:${networkId}:slot:${slotIdx}`;
}

/**
 * Sort link ids deterministically, preferring real ids over the optimistic
 * ``tmp_*`` placeholders so a freshly-created link does not yank existing
 * slots around while the POST is in flight.
 */
function sortLinkIdsForSlots(linkIds: readonly string[]): string[] {
  const real: string[] = [];
  const optimistic: string[] = [];
  for (const id of linkIds) {
    (id.startsWith('tmp_') ? optimistic : real).push(id);
  }
  real.sort();
  optimistic.sort();
  return [...real, ...optimistic];
}

/**
 * Assign each link id a stable slot index for a single network. Slot
 * ``links.length`` is the always-open slot used for new connections.
 */
export function assignNetworkSlots(linkIds: readonly string[]): Map<string, number> {
  const sorted = sortLinkIdsForSlots(linkIds);
  const out = new Map<string, number>();
  sorted.forEach((id, idx) => out.set(id, idx));
  return out;
}

/**
 * Group declared links by ``link.to.network_id`` and build per-network slot
 * assignments. Networks not present in the map have zero declared links and
 * render only the open slot at index ``0``.
 */
export function buildNetworkSlotMap(
  links: Link[] | null | undefined
): Map<number, Map<string, number>> {
  const out = new Map<number, Map<string, number>>();
  if (!links) return out;
  const grouped = new Map<number, string[]>();
  for (const link of links) {
    if (!link?.id) continue;
    const networkId = link.to?.network_id;
    if (typeof networkId !== 'number') continue;
    if (!grouped.has(networkId)) grouped.set(networkId, []);
    grouped.get(networkId)!.push(link.id);
  }
  for (const [networkId, ids] of grouped) {
    out.set(networkId, assignNetworkSlots(ids));
  }
  return out;
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
  divergentByLinkId: Record<string, LinkReconciliation> | null | undefined = null,
  networks: Readonly<Record<string, NetworkShape>> | null | undefined = null
): Edge[] {
  const hasLinks = !!(links && links.length > 0);
  const hasDiscovered = !!(discoveredLinks && discoveredLinks.length > 0);
  if (!hasLinks && !hasDiscovered) {
    return [];
  }

  const defaultStyle: LinkStyle = defaults?.link_style ?? 'orthogonal';
  const edges: Edge[] = [];
  const slotMapByNetwork = buildNetworkSlotMap(links);

  // Identify implicit-bridge networks so paired halves can collapse into a
  // single node-to-node edge. ``handledImplicitLinkIds`` tracks halves that
  // we've already emitted (as a collapsed edge or skipped as the peer of
  // one); the regular loop below skips them. Broken implicit networks
  // (refcount != 2) fall through to the regular branch with an amber
  // overlay style — TopologyCanvas force-shows their network node so the
  // orphan edge has a target.
  const implicitMeta = hasLinks
    ? _buildImplicitNetMeta(links!, networks)
    : { pairsByNet: new Map<number, Link[]>(), isImplicit: () => false };
  const handledImplicitLinkIds = new Set<string>();
  if (hasLinks) for (const [networkId, pair] of implicitMeta.pairsByNet) {
    if (pair.length !== 2) continue;
    const [a, b] = pair;
    const aNode = a.from?.node_id;
    const bNode = b.from?.node_id;
    if (typeof aNode !== 'number' || typeof bNode !== 'number') continue;
    const aIface = a.from?.interface_index;
    const bIface = b.from?.interface_index;
    handledImplicitLinkIds.add(a.id);
    handledImplicitLinkIds.add(b.id);
    // Deterministic edge id and orientation: sort by (node_id, iface_idx)
    // so the same pair produces the same edge id regardless of which half
    // was iterated first.
    const sortedHalves = [a, b].sort((l, r) => {
      const ln = l.from?.node_id ?? 0;
      const rn = r.from?.node_id ?? 0;
      if (ln !== rn) return ln - rn;
      return (l.from?.interface_index ?? 0) - (r.from?.interface_index ?? 0);
    });
    const [srcLink, dstLink] = sortedHalves;
    const srcNode = srcLink.from?.node_id ?? 0;
    const dstNode = dstLink.from?.node_id ?? 0;
    const srcIface = srcLink.from?.interface_index;
    const dstIface = dstLink.from?.interface_index;
    const widthValue = parseInt(a.width ?? '1', 10);
    const strokeWidth = Number.isFinite(widthValue) && widthValue > 0 ? widthValue : 1;
    const strokeColor = a.color && a.color.length > 0 ? a.color : '#9ca3af';
    edges.push({
      id: `pair:net${networkId}`,
      source: `node${srcNode}`,
      target: `node${dstNode}`,
      sourceHandle: typeof srcIface === 'number' ? `iface-${srcIface}` : 'default',
      targetHandle: typeof dstIface === 'number' ? `iface-${dstIface}` : 'default',
      type: 'link',
      animated: false,
      label: a.label && a.label.length > 0 ? a.label : undefined,
      style: `stroke: ${strokeColor}; stroke-width: ${strokeWidth}px;`,
      labelStyle: 'fill: #d1d5db; font-size: 10px; font-family: var(--font-mono);',
      data: {
        style_override: a.style_override ?? null,
        default_style: defaultStyle,
        label: a.label ?? '',
        color: a.color ?? '',
        width: a.width ?? '1',
        implicit_bridge_network_id: networkId,
        implicit_bridge_link_ids: [a.id, b.id],
      },
    });
  }

  if (hasLinks) for (const link of links!) {
    if (!link || !link.id) continue;
    if (handledImplicitLinkIds.has(link.id)) continue;

    const fromNodeId = link.from?.node_id;
    if (typeof fromNodeId !== 'number') continue;

    const fromInterfaceIndex = link.from?.interface_index;

    let target: string | null = null;
    let targetHandle: string | null = null;

    if (typeof link.to?.network_id === 'number') {
      target = `network${link.to.network_id}`;
      const networkSlots = slotMapByNetwork.get(link.to.network_id);
      const slotIdx = networkSlots?.get(link.id);
      const resolvedSlot = typeof slotIdx === 'number' ? slotIdx : (networkSlots?.size ?? 0);
      targetHandle = slotHandleId(link.to.network_id, resolvedSlot);
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

    // Orphan implicit-bridge half — declared link to an implicit network
    // whose paired peer is missing (refcount != 2). The bridge is hidden by
    // default; the canvas force-shows it so the orphan has a target node
    // to point at. Style it amber so the broken state is visible at a
    // glance.
    const isOrphanImplicit =
      typeof link.to?.network_id === 'number'
        && implicitMeta.isImplicit(link.to.network_id)
        && (implicitMeta.pairsByNet.get(link.to.network_id)?.length ?? 0) !== 2;

    let edgeStyle: string;
    if (isDivergent) {
      edgeStyle = 'stroke: #f87171; stroke-dasharray: 2 2; opacity: 0.7;';
    } else if (isOrphanImplicit) {
      edgeStyle = 'stroke: #fbbf24; stroke-dasharray: 4 3; opacity: 0.85;';
    } else {
      edgeStyle = `stroke: ${strokeColor}; stroke-width: ${strokeWidth}px;`;
    }

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
        orphan_implicit: isOrphanImplicit,
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
    // Discovered (kernel-only) overlays target the open slot — they are
    // observation-only so we do not allocate a dedicated slot for them.
    const declaredSlots = slotMapByNetwork.get(networkId)?.size ?? 0;
    const targetHandle = slotHandleId(networkId, declaredSlots);

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
