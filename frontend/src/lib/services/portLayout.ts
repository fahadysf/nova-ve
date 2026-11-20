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
import { PORT_CLEARANCE } from './router/orthogonal';

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

function moveAwayFromCorner(
  position: PortPosition,
  corner: 'top-left' | 'top-right' | 'bottom-right' | 'bottom-left',
  deltaPx: number,
  nodeBox: NodeBox
): PortPosition {
  const sideLength = sideLengthPx(position.side, nodeBox);
  const delta = deltaPx / sideLength;
  if (delta <= 0) return position;

  if (
    (corner === 'top-left' && (position.side === 'top' || position.side === 'left')) ||
    (corner === 'bottom-left' && position.side === 'bottom')
  ) {
    return { ...position, offset: clamp01(position.offset + delta) };
  }

  if (corner === 'top-right' && position.side === 'right') {
    return { ...position, offset: clamp01(position.offset + delta) };
  }

  return { ...position, offset: clamp01(position.offset - delta) };
}

function cornerPairs(
  positions: readonly PortPosition[],
  corner: 'top-left' | 'top-right' | 'bottom-right' | 'bottom-left'
): Array<[number, number]> {
  const sidePair: readonly [PortSide, PortSide] =
    corner === 'top-left'
      ? ['top', 'left']
      : corner === 'top-right'
        ? ['top', 'right']
        : corner === 'bottom-right'
          ? ['bottom', 'right']
          : ['bottom', 'left'];
  const a = positions
    .map((position, index) => ({ position, index }))
    .filter((entry) => entry.position.side === sidePair[0]);
  const b = positions
    .map((position, index) => ({ position, index }))
    .filter((entry) => entry.position.side === sidePair[1]);
  return a.flatMap((left) => b.map((right) => [left.index, right.index] as [number, number]));
}

function separateCornerCollisions(
  positions: PortPosition[],
  nodeBox: NodeBox,
  minCenterGapPx: number
): PortPosition[] {
  const result = positions.map((position) => ({ ...position }));
  const corners = ['top-left', 'top-right', 'bottom-right', 'bottom-left'] as const;
  const maxPasses = 6;

  for (let pass = 0; pass < maxPasses; pass += 1) {
    let changed = false;
    for (const corner of corners) {
      for (const [aIndex, bIndex] of cornerPairs(result, corner)) {
        const a = portPositionToPoint(result[aIndex], nodeBox);
        const b = portPositionToPoint(result[bIndex], nodeBox);
        const distance = Math.hypot(a.x - b.x, a.y - b.y);
        if (distance + 1e-6 >= minCenterGapPx) continue;

        const pushPx = minCenterGapPx - distance + 1e-3;
        result[aIndex] = moveAwayFromCorner(result[aIndex], corner, pushPx, nodeBox);
        result[bIndex] = moveAwayFromCorner(result[bIndex], corner, pushPx, nodeBox);
        changed = true;
      }
    }
    if (!changed) break;
  }

  return result;
}

/**
 * Spread ports that share one box side far enough that their circular handles
 * keep at least ``minCenterGapPx`` between centers, placed symmetrically
 * around the CENTER of the side (#258 — NetBird-style center bias): a lone
 * port sits exactly at the side's midpoint and a group fans out evenly about
 * it instead of drifting toward a corner. Incoming order (by projected
 * offset) is preserved so link stubs don't cross. For the default 10px
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

    if (entries.length === 0) continue;

    const minDelta = (minCenterGapPx + 1e-6) / sideLengthPx(side, nodeBox);
    const effectiveDelta =
      entries.length > 1 && minDelta * (entries.length - 1) > 1
        ? 1 / (entries.length - 1)
        : minDelta;

    for (const [i, entry] of entries.entries()) {
      const offset =
        entries.length === 1
          ? 0.5
          : 0.5 + (i - (entries.length - 1) / 2) * effectiveDelta;
      result[entry.index] = { side, offset: clamp01(offset) };
    }
  }

  return separateCornerCollisions(result, nodeBox, minCenterGapPx);
}

/**
 * Nudge a standalone slot (the dotted "new connection" handle) away from
 * already-placed ports with the same aversion rules the placed ports obey
 * among themselves (#258). The slot keeps its canonical position unless a
 * port on the same side sits within ``minCenterGapPx``, in which case it
 * shifts to the nearest free stretch of that side — placed ports never move.
 * A corner backstop then pushes it clear of perpendicular occupants near a
 * shared corner, mirroring ``separateCornerCollisions``.
 */
export function nudgeAwayFromPorts(
  slot: PortPosition,
  occupied: readonly PortPosition[],
  nodeBox: NodeBox,
  minCenterGapPx = PORT_HANDLE_MIN_CENTER_GAP_PX
): PortPosition {
  const result: PortPosition = { side: slot.side, offset: clamp01(slot.offset) };

  const sameSideOffsets = occupied
    .filter((position) => position.side === result.side)
    .map((position) => clamp01(position.offset))
    .sort((a, b) => a - b);
  const minDelta = (minCenterGapPx + 1e-6) / sideLengthPx(result.side, nodeBox);
  const clearOf = (offset: number): boolean =>
    sameSideOffsets.every((o) => Math.abs(offset - o) + 1e-9 >= minDelta);

  if (sameSideOffsets.length > 0 && !clearOf(result.offset)) {
    // Feasible stretches between/around the placed ports; pick the one whose
    // clamped point is closest to the slot's canonical offset.
    const intervals: Array<[number, number]> = [];
    let cursor = 0;
    for (const o of sameSideOffsets) {
      const hi = o - minDelta;
      if (hi >= cursor) intervals.push([cursor, hi]);
      cursor = o + minDelta;
    }
    if (cursor <= 1) intervals.push([cursor, 1]);

    let best: number | null = null;
    let bestDist = Infinity;
    for (const [lo, hi] of intervals) {
      const candidate = Math.min(Math.max(result.offset, lo), hi);
      const dist = Math.abs(candidate - result.offset);
      if (dist < bestDist) {
        best = candidate;
        bestDist = dist;
      }
    }
    if (best === null) {
      // Side saturated with ports: hug the less-crowded edge.
      const mid = (sameSideOffsets[0] + sameSideOffsets[sameSideOffsets.length - 1]) / 2;
      best = result.offset <= mid ? 0 : 1;
    }
    result.offset = clamp01(best);
  }

  // Corner backstop: perpendicular occupants near a shared corner. Push along
  // our own side in whichever direction increases the Euclidean gap.
  for (let pass = 0; pass < 6; pass += 1) {
    let changed = false;
    for (const position of occupied) {
      if (position.side === result.side) continue;
      const point = portPositionToPoint(result, nodeBox);
      const other = portPositionToPoint(position, nodeBox);
      const distance = Math.hypot(point.x - other.x, point.y - other.y);
      if (distance + 1e-6 >= minCenterGapPx) continue;

      const probe = portPositionToPoint(
        { side: result.side, offset: clamp01(result.offset + 0.001) },
        nodeBox
      );
      const sign = Math.hypot(probe.x - other.x, probe.y - other.y) >= distance ? 1 : -1;
      // 1.2× overshoot: only the slot moves, so a plain step converges
      // asymptotically from below and never crosses the threshold.
      const delta =
        (1.2 * (minCenterGapPx - distance) + 1e-3) / sideLengthPx(result.side, nodeBox);
      result.offset = clamp01(result.offset + sign * delta);
      changed = true;
    }
    if (!changed) break;
  }

  return result;
}

// ---------- route-aware side selection (#258) --------------------------------

/**
 * Depth of the "exit strip" probed in front of a candidate port. Twice the
 * router's obstacle clearance, so a blocked strip means the free exit lane
 * is narrower than the router would ever leave open.
 */
const EXIT_STRIP_DEPTH_PX = PORT_CLEARANCE * 2;
/** Half-width of the probed band across the port handle. */
const EXIT_STRIP_HALF_WIDTH_PX = PORT_HANDLE_MIN_CENTER_GAP_PX / 2 + 1;

/**
 * True when the area immediately in front of ``position`` is free of
 * inflated obstacles — i.e. the link can leave the port moving forward
 * instead of being forced to bend back behind the origin card.
 */
function exitStripIsClear(
  position: PortPosition,
  nodeBox: NodeBox,
  obstacles: readonly NodeBox[],
  clearance: number
): boolean {
  const point = portPositionToPoint(position, nodeBox);
  let minX: number;
  let maxX: number;
  let minY: number;
  let maxY: number;
  switch (position.side) {
    case 'right':
      minX = nodeBox.x + nodeBox.w;
      maxX = minX + EXIT_STRIP_DEPTH_PX;
      minY = point.y - EXIT_STRIP_HALF_WIDTH_PX;
      maxY = point.y + EXIT_STRIP_HALF_WIDTH_PX;
      break;
    case 'left':
      maxX = nodeBox.x;
      minX = maxX - EXIT_STRIP_DEPTH_PX;
      minY = point.y - EXIT_STRIP_HALF_WIDTH_PX;
      maxY = point.y + EXIT_STRIP_HALF_WIDTH_PX;
      break;
    case 'bottom':
      minY = nodeBox.y + nodeBox.h;
      maxY = minY + EXIT_STRIP_DEPTH_PX;
      minX = point.x - EXIT_STRIP_HALF_WIDTH_PX;
      maxX = point.x + EXIT_STRIP_HALF_WIDTH_PX;
      break;
    case 'top':
    default:
      maxY = nodeBox.y;
      minY = maxY - EXIT_STRIP_DEPTH_PX;
      minX = point.x - EXIT_STRIP_HALF_WIDTH_PX;
      maxX = point.x + EXIT_STRIP_HALF_WIDTH_PX;
      break;
  }
  return !obstacles.some((obstacle) => {
    const oMinX = obstacle.x - clearance;
    const oMaxX = obstacle.x + obstacle.w + clearance;
    const oMinY = obstacle.y - clearance;
    const oMaxY = obstacle.y + obstacle.h + clearance;
    return oMinX < maxX && oMaxX > minX && oMinY < maxY && oMaxY > minY;
  });
}

/** Open space between the source side and the target box along ``side``. */
function sideGapPx(sourceBox: NodeBox, targetBox: NodeBox, side: PortSide): number {
  switch (side) {
    case 'right':
      return Math.max(0, targetBox.x - (sourceBox.x + sourceBox.w));
    case 'left':
      return Math.max(0, sourceBox.x - (targetBox.x + targetBox.w));
    case 'bottom':
      return Math.max(0, targetBox.y - (sourceBox.y + sourceBox.h));
    case 'top':
    default:
      return Math.max(0, sourceBox.y - (targetBox.y + targetBox.h));
  }
}

/** Perimeter position on ``sourceBox`` for ``side`` facing ``targetBox``. */
function projectedPortPosition(
  sourceBox: NodeBox,
  targetBox: NodeBox,
  side: PortSide
): PortPosition {
  const w = sourceBox.w > 0 ? sourceBox.w : 1;
  const h = sourceBox.h > 0 ? sourceBox.h : 1;
  const targetCenter = {
    x: targetBox.x + Math.max(targetBox.w, 1) / 2,
    y: targetBox.y + Math.max(targetBox.h, 1) / 2,
  };
  if (side === 'top' || side === 'bottom') {
    return { side, offset: clamp01((targetCenter.x - sourceBox.x) / w) };
  }
  return { side, offset: clamp01((targetCenter.y - sourceBox.y) / h) };
}

/**
 * Gap-facing port position, relocated when the chosen side is boxed in
 * (#258). ``closestPortPositionToBox`` only looks at the two endpoint boxes,
 * so a peer sitting on the far side of an unrelated card gets a port whose
 * exit is blocked — the router then bends the link BACK behind the origin
 * node (observed live: net-bridge → vm-linux doubled back off its right
 * edge because iol-rtr blocked the corridor). When the gap-facing side's
 * exit strip is obstructed, move the connection point to the unconstricted
 * FORWARD side that heads most directly toward the target (ties on the
 * wider gap). Sides pointing away from the peer never win — a top-side port
 * for a bottom-right target is worse than keeping the facing side and
 * letting the router detour. With a clear exit the position is exactly
 * ``closestPortPositionToBox``.
 */
export function bestRouteAwarePortPosition(
  sourceBox: NodeBox,
  targetBox: NodeBox,
  obstacles: readonly NodeBox[] = [],
  clearance = PORT_CLEARANCE
): PortPosition {
  const chosen = closestPortPositionToBox(sourceBox, targetBox);
  if (obstacles.length === 0) return chosen;
  if (exitStripIsClear(chosen, sourceBox, obstacles, clearance)) return chosen;

  const sourceCenter = {
    x: sourceBox.x + sourceBox.w / 2,
    y: sourceBox.y + sourceBox.h / 2,
  };
  const targetCenter = {
    x: targetBox.x + targetBox.w / 2,
    y: targetBox.y + targetBox.h / 2,
  };
  const toPeerX = targetCenter.x - sourceCenter.x;
  const toPeerY = targetCenter.y - sourceCenter.y;
  const toPeerLen = Math.hypot(toPeerX, toPeerY) || 1;

  let best: PortPosition | null = null;
  let bestScore = -Infinity;
  for (const side of SIDES) {
    if (side === chosen.side) continue;
    const candidate = projectedPortPosition(sourceBox, targetBox, side);
    if (!exitStripIsClear(candidate, sourceBox, obstacles, clearance)) continue;
    const towardPeer =
      side === 'top'
        ? -toPeerY
        : side === 'bottom'
          ? toPeerY
          : side === 'right'
            ? toPeerX
            : -toPeerX;
    // Never relocate to a side that points away from the peer — the link
    // would leave backwards and hug the origin box.
    if (towardPeer <= 0) continue;
    const score = (towardPeer / toPeerLen) * 1000 + sideGapPx(sourceBox, targetBox, side);
    if (score > bestScore) {
      best = candidate;
      bestScore = score;
    }
  }
  return best ?? chosen;
}
