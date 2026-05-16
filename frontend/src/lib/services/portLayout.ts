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
export const PORT_HANDLE_DIAMETER_PX = 10;
export const PORT_HANDLE_MIN_EDGE_GAP_PX = 1;
export const PORT_HANDLE_MIN_CENTER_GAP_PX =
  PORT_HANDLE_DIAMETER_PX + PORT_HANDLE_MIN_EDGE_GAP_PX;

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

/**
 * Return the perimeter position on ``sourceBox`` closest to ``targetBox``.
 *
 * This gives connected endpoints their shortest default path by choosing the
 * facing side from the gap between the two boxes, then projecting the
 * counterpart's center onto that side.
 */
export function closestPortPositionToBox(sourceBox: NodeBox, targetBox: NodeBox): PortPosition {
  const w = sourceBox.w > 0 ? sourceBox.w : 1;
  const h = sourceBox.h > 0 ? sourceBox.h : 1;
  const sourceRight = sourceBox.x + w;
  const sourceBottom = sourceBox.y + h;
  const targetRight = targetBox.x + Math.max(targetBox.w, 1);
  const targetBottom = targetBox.y + Math.max(targetBox.h, 1);
  const targetCenter = {
    x: targetBox.x + Math.max(targetBox.w, 1) / 2,
    y: targetBox.y + Math.max(targetBox.h, 1) / 2,
  };

  const gapRight = Math.max(0, targetBox.x - sourceRight);
  const gapLeft = Math.max(0, sourceBox.x - targetRight);
  const gapBottom = Math.max(0, targetBox.y - sourceBottom);
  const gapTop = Math.max(0, sourceBox.y - targetBottom);
  const horizontalGap = Math.max(gapRight, gapLeft);
  const verticalGap = Math.max(gapBottom, gapTop);

  if (horizontalGap === 0 && verticalGap === 0) {
    return clampToPerimeter(targetCenter, sourceBox);
  }

  if (horizontalGap >= verticalGap) {
    return {
      side: gapLeft > gapRight ? 'left' : 'right',
      offset: clamp01((targetCenter.y - sourceBox.y) / h),
    };
  }

  return {
    side: gapTop > gapBottom ? 'top' : 'bottom',
    offset: clamp01((targetCenter.x - sourceBox.x) / w),
  };
}

function sideLengthPx(side: PortSide, nodeBox: NodeBox): number {
  if (side === 'top' || side === 'bottom') {
    return nodeBox.w > 0 ? nodeBox.w : 1;
  }
  return nodeBox.h > 0 ? nodeBox.h : 1;
}

/**
 * Spread ports that share one box side far enough that their circular handles
 * keep at least ``minCenterGapPx`` between centers. For the default 10px
 * handles this preserves a 1px visual gap between handle edges.
 */
export function separatePortPositions(
  positions: readonly PortPosition[],
  nodeBox: NodeBox,
  minCenterGapPx = PORT_HANDLE_MIN_CENTER_GAP_PX
): PortPosition[] {
  const result = positions.map((position) => ({
    side: position.side,
    offset: clamp01(position.offset),
  }));

  for (const side of SIDES) {
    const entries = result
      .map((position, index) => ({ position, index }))
      .filter((entry) => entry.position.side === side)
      .sort((a, b) => {
        const offsetDelta = a.position.offset - b.position.offset;
        return offsetDelta === 0 ? a.index - b.index : offsetDelta;
      });

    if (entries.length <= 1) continue;

    const minDelta = (minCenterGapPx + 1e-6) / sideLengthPx(side, nodeBox);
    const effectiveDelta =
      minDelta * (entries.length - 1) > 1 ? 1 / (entries.length - 1) : minDelta;

    const adjusted = entries.map((entry) => entry.position.offset);
    for (let index = 1; index < adjusted.length; index += 1) {
      adjusted[index] = Math.max(adjusted[index], adjusted[index - 1] + effectiveDelta);
    }

    const overflow = adjusted[adjusted.length - 1] - 1;
    if (overflow > 0) {
      for (let index = 0; index < adjusted.length; index += 1) {
        adjusted[index] -= overflow;
      }
    }

    for (let index = adjusted.length - 2; index >= 0; index -= 1) {
      adjusted[index] = Math.min(adjusted[index], adjusted[index + 1] - effectiveDelta);
    }

    const underflow = 0 - adjusted[0];
    if (underflow > 0) {
      for (let index = 0; index < adjusted.length; index += 1) {
        adjusted[index] += underflow;
      }
    }

    for (const [entryIndex, entry] of entries.entries()) {
      result[entry.index] = {
        side,
        offset: clamp01(adjusted[entryIndex]),
      };
    }
  }

  return result;
}
