// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { Link, NodeData, PortPosition } from '$lib/types';
import type { NodeBox } from './portLayout';

export const FRAME_WIDTH = 210;
export const FRAME_HEADER_HEIGHT = 56;
export const FRAME_ROW_HEIGHT = 28;
export const FRAME_ROW_LIMIT = 6;

export interface NetworkFrameRow {
  linkId: string;
  label: string;
  peerX: number | null;
}

/** A display projection of links: no device is reparented or moved. */
export function networkFrameRows(networkId: number, links: readonly Link[], nodes: Readonly<Record<string, NodeData>>): NetworkFrameRow[] {
  return links.flatMap<NetworkFrameRow>((link) => {
    // Match the set of declared edges rendered by deriveEdges. Other endpoint
    // shapes do not currently have a canvas handle and must not get fake rows.
    if (link.to?.network_id !== networkId || typeof link.from?.node_id !== 'number') return [];
    const peer = link.from;
    if (typeof peer?.node_id === 'number') {
      const node = nodes[String(peer.node_id)];
      const idx = peer.interface_index ?? 0;
      const iface = node?.interfaces?.find((item) => item.index === idx)?.name ?? `eth${idx}`;
      return [{ linkId: link.id, label: `${node?.name ?? `Node ${peer.node_id}`} · ${iface}`, peerX: node?.left ?? null }];
    }
    return [];
  }).sort((a, b) => a.linkId.localeCompare(b.linkId, undefined, { numeric: true }));
}

export function networkFrameHeight(rowCount: number): number {
  return FRAME_HEADER_HEIGHT + 1 + FRAME_ROW_HEIGHT * (Math.min(rowCount, FRAME_ROW_LIMIT) + (rowCount > FRAME_ROW_LIMIT ? 1 : 0));
}

/** Coordinates are relative to the unchanged saved network origin. */
export function frameFits(box: NodeBox, others: readonly NodeBox[]): boolean {
  return others.every((other) => box.x + box.w <= other.x || other.x + other.w <= box.x || box.y + box.h <= other.y || other.y + other.h <= box.y);
}

export function frameRowPosition(index: number, height: number, side: 'left' | 'right'): PortPosition {
  return { side, offset: (FRAME_HEADER_HEIGHT + FRAME_ROW_HEIGHT * (index + 0.5)) / height };
}
