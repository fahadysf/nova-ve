// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { PortRef, RouteRequest, RouteResult } from './index';

/**
 * Cubic bezier router. Control points placed at `0.5*|dx|` along the port
 * tangent on each side, where the tangent is determined by `port.side`:
 *   top    → ( 0,-1)
 *   right  → ( 1, 0)
 *   bottom → ( 0, 1)
 *   left   → (-1, 0)
 * The "|dx|" magnitude here is the source/target separation along the
 * tangent's dominant axis (use the absolute distance between endpoints to
 * keep the curve smooth regardless of orientation).
 */
export function routeBezier(req: RouteRequest): RouteResult {
  const { from, to } = req;
  const [tx1, ty1] = tangentFor(from);
  const [tx2, ty2] = tangentFor(to);

  const dx = Math.abs(to.x - from.x);
  const dy = Math.abs(to.y - from.y);
  // Use the dominant separation as control magnitude. This keeps horizontal
  // bezier shapes proportionate to horizontal distance and vertical to vertical.
  const mag = 0.5 * Math.max(dx, dy, 1);

  const cx1 = from.x + mag * tx1;
  const cy1 = from.y + mag * ty1;
  const cx2 = to.x + mag * tx2;
  const cy2 = to.y + mag * ty2;

  const d = `M ${from.x},${from.y} C ${cx1},${cy1} ${cx2},${cy2} ${to.x},${to.y}`;

  // Approximate length by sampling.
  const lengthPx = sampleCubicLength(
    from.x,
    from.y,
    cx1,
    cy1,
    cx2,
    cy2,
    to.x,
    to.y,
    16,
  );

  return { d, bends: 0, lengthPx };
}

/** Return the unit tangent direction for a port given its side. */
export function tangentFor(port: PortRef): [number, number] {
  switch (port.side) {
    case 'top':
      return [0, -1];
    case 'right':
      return [1, 0];
    case 'bottom':
      return [0, 1];
    case 'left':
      return [-1, 0];
  }
}

function sampleCubicLength(
  sx: number,
  sy: number,
  c1x: number,
  c1y: number,
  c2x: number,
  c2y: number,
  ex: number,
  ey: number,
  steps: number,
): number {
  let prevX = sx;
  let prevY = sy;
  let total = 0;
  for (let i = 1; i <= steps; i++) {
    const t = i / steps;
    const it = 1 - t;
    const x =
      it * it * it * sx +
      3 * it * it * t * c1x +
      3 * it * t * t * c2x +
      t * t * t * ex;
    const y =
      it * it * it * sy +
      3 * it * it * t * c1y +
      3 * it * t * t * c2y +
      t * t * t * ey;
    total += Math.hypot(x - prevX, y - prevY);
    prevX = x;
    prevY = y;
  }
  return total;
}
