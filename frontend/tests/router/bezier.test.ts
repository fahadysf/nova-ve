// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * Bezier router invariants. Verifies that control points are placed at
 * 0.5*max(|dx|,|dy|) along the port tangent on at least three port
 * orientations.
 */

import { describe, it, expect } from 'vitest';
import { routeBezier, tangentFor } from '$lib/services/router/bezier';
import type { PortRef, RouteRequest } from '$lib/services/router';

function parseCubic(d: string): {
  s: [number, number];
  c1: [number, number];
  c2: [number, number];
  e: [number, number];
} {
  const m = d.match(
    /^M (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?) C (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/,
  );
  expect(m, `bad path: ${d}`).toBeTruthy();
  const n = m!.slice(1).map(Number);
  return {
    s: [n[0], n[1]],
    c1: [n[2], n[3]],
    c2: [n[4], n[5]],
    e: [n[6], n[7]],
  };
}

describe('bezier router control-point invariant', () => {
  const cases: Array<{ from: PortRef; to: PortRef }> = [
    {
      from: { x: 0, y: 100, side: 'right' },
      to: { x: 300, y: 100, side: 'left' },
    },
    {
      from: { x: 100, y: 0, side: 'bottom' },
      to: { x: 100, y: 240, side: 'top' },
    },
    {
      from: { x: 0, y: 0, side: 'right' },
      to: { x: 200, y: 200, side: 'top' },
    },
    {
      from: { x: 50, y: 0, side: 'top' },
      to: { x: 250, y: 100, side: 'bottom' },
    },
  ];

  for (const c of cases) {
    it(`${c.from.side} → ${c.to.side}: control points lie on tangents at 0.5*max(|dx|,|dy|)`, () => {
      const req: RouteRequest = {
        from: c.from,
        to: c.to,
        obstacles: [],
        style: 'bezier',
      };
      const result = routeBezier(req);
      const { s, c1, c2, e } = parseCubic(result.d);
      expect(s).toEqual([c.from.x, c.from.y]);
      expect(e).toEqual([c.to.x, c.to.y]);

      const dx = Math.abs(c.to.x - c.from.x);
      const dy = Math.abs(c.to.y - c.from.y);
      const mag = 0.5 * Math.max(dx, dy, 1);

      const [tx1, ty1] = tangentFor(c.from);
      const [tx2, ty2] = tangentFor(c.to);

      expect(c1[0]).toBeCloseTo(c.from.x + mag * tx1, 6);
      expect(c1[1]).toBeCloseTo(c.from.y + mag * ty1, 6);
      expect(c2[0]).toBeCloseTo(c.to.x + mag * tx2, 6);
      expect(c2[1]).toBeCloseTo(c.to.y + mag * ty2, 6);

      expect(result.bends).toBe(0);
      expect(result.lengthPx).toBeGreaterThan(0);
    });
  }
});
