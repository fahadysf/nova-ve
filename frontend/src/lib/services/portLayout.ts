// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * portLayout — angular-resolution port placement (Formann 1993).
 *
 * For ``n`` interfaces, parameter ``t_i = (i + 0.5) / n`` is the perimeter
 * position in [0, 1]. The unit perimeter is split into four quadrants of
 * length 0.25 mapping to the four sides:
 *   t ∈ [0.00, 0.25)  → top
 *   t ∈ [0.25, 0.50)  → right
 *   t ∈ [0.50, 0.75)  → bottom
 *   t ∈ [0.75, 1.00]  → left
 *
 * Within a quadrant the offset along the side is ``(t - quadStart) / 0.25``,
 * giving an even angular distribution that minimises symbol clustering on
 * any single edge.
 */

import type { PortPosition, PortSide } from '$lib/types';

export interface PointLike {
  x: number;
  y: number;
}

export interface NodeBox {
  x: number;
  y: number;
  w: number;
  h: number;
}

const SIDES: readonly PortSide[] = ['top', 'right', 'bottom', 'left'];

function clamp01(value: number): number {
  if (Number.isNaN(value)) return 0;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

/**
 * Map a perimeter parameter ``t ∈ [0, 1]`` to a (side, offset) tuple.
 *
 * The mapping splits the unit perimeter into four equal quadrants. The closed
 * upper boundary (t === 1) is anchored to the last side ("left", offset 1) to
 * avoid wrap-around when callers feed sentinel values.
 */
export function tToPortPosition(t: number): PortPosition {
  const clamped = clamp01(t);
  if (clamped >= 1) {
    return { side: 'left', offset: 1 };
  }
  const quadrant = Math.min(3, Math.floor(clamped / 0.25));
  const quadStart = quadrant * 0.25;
  const offset = clamp01((clamped - quadStart) / 0.25);
  return { side: SIDES[quadrant], offset };
}

/**
 * Place ``n`` interfaces around a node's perimeter using angular resolution.
 *
 * For n = 0 returns an empty array. For n >= 1 returns an array of length n
 * where index ``i`` carries the ``PortPosition`` for the i-th interface.
 */
export function placeInterfaces(n: number): PortPosition[] {
  if (!Number.isFinite(n) || n <= 0) {
    return [];
  }
  const count = Math.floor(n);
  const result: PortPosition[] = new Array(count);
  for (let i = 0; i < count; i += 1) {
    const t = (i + 0.5) / count;
    result[i] = tToPortPosition(t);
  }
  return result;
}

/**
 * Convert a (side, offset) tuple back to absolute coordinates relative to a
 * node's bounding box. Useful for rendering ports and for snapping a dragged
 * cursor to its closest perimeter anchor.
 */
export function portPositionToPoint(position: PortPosition, nodeBox: NodeBox): PointLike {
  const offset = clamp01(position.offset);
  switch (position.side) {
    case 'top':
      return { x: nodeBox.x + nodeBox.w * offset, y: nodeBox.y };
    case 'right':
      return { x: nodeBox.x + nodeBox.w, y: nodeBox.y + nodeBox.h * offset };
    case 'bottom':
      return { x: nodeBox.x + nodeBox.w * offset, y: nodeBox.y + nodeBox.h };
    case 'left':
    default:
      return { x: nodeBox.x, y: nodeBox.y + nodeBox.h * offset };
  }
}

/**
 * Clamp an arbitrary point to the nearest perimeter slot of ``nodeBox`` and
 * return the equivalent ``PortPosition`` (side + normalised offset).
 *
 * The clamp uses the unsigned distance to each of the four edges and selects
 * the closest one; the offset along the chosen edge is computed by projecting
 * the point onto that edge and normalising into [0, 1].
 */
export function clampToPerimeter(point: PointLike, nodeBox: NodeBox): PortPosition {
  const w = nodeBox.w > 0 ? nodeBox.w : 1;
  const h = nodeBox.h > 0 ? nodeBox.h : 1;
  const dxLeft = Math.abs(point.x - nodeBox.x);
  const dxRight = Math.abs(nodeBox.x + w - point.x);
  const dyTop = Math.abs(point.y - nodeBox.y);
  const dyBottom = Math.abs(nodeBox.y + h - point.y);

  let bestSide: PortSide = 'top';
  let bestDist = dyTop;
  if (dxRight < bestDist) {
    bestSide = 'right';
    bestDist = dxRight;
  }
  if (dyBottom < bestDist) {
    bestSide = 'bottom';
    bestDist = dyBottom;
  }
  if (dxLeft < bestDist) {
    bestSide = 'left';
    bestDist = dxLeft;
  }

  if (bestSide === 'top' || bestSide === 'bottom') {
    return { side: bestSide, offset: clamp01((point.x - nodeBox.x) / w) };
  }
  return { side: bestSide, offset: clamp01((point.y - nodeBox.y) / h) };
}
