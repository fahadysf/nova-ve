// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * 6 fixture cases for the orthogonal A* router. Each asserts that the bend
 * count is ≤ optimal+1 (per US-066 acceptance criteria).
 */

import { describe, it, expect } from 'vitest';
import { routeOrthogonal, roundedOrthogonalPath } from '$lib/services/router/orthogonal';
import type { RouteRequest } from '$lib/services/router';

interface Fixture {
  name: string;
  req: RouteRequest;
  optimalBends: number;
}

const FIXTURES: Fixture[] = [
  {
    name: 'horizontal aligned ports, no obstacles',
    req: {
      from: { x: 0, y: 100, side: 'right' },
      to: { x: 200, y: 100, side: 'left' },
      obstacles: [],
      style: 'orthogonal',
    },
    optimalBends: 0,
  },
  {
    name: 'vertical aligned ports, no obstacles',
    req: {
      from: { x: 100, y: 0, side: 'bottom' },
      to: { x: 100, y: 200, side: 'top' },
      obstacles: [],
      style: 'orthogonal',
    },
    optimalBends: 0,
  },
  {
    name: 'right-angle L-route, no obstacles',
    req: {
      from: { x: 0, y: 0, side: 'right' },
      to: { x: 200, y: 200, side: 'top' },
      obstacles: [],
      style: 'orthogonal',
    },
    optimalBends: 1,
  },
  {
    name: 'simple obstacle requires Z-detour',
    req: {
      from: { x: 0, y: 100, side: 'right' },
      to: { x: 400, y: 100, side: 'left' },
      obstacles: [{ x: 150, y: 60, w: 100, h: 80 }],
      style: 'orthogonal',
    },
    optimalBends: 2,
  },
  {
    name: 'wide gap with multiple obstacles',
    req: {
      from: { x: 0, y: 50, side: 'right' },
      to: { x: 600, y: 250, side: 'left' },
      obstacles: [
        { x: 100, y: 0, w: 80, h: 120 },
        { x: 300, y: 180, w: 80, h: 120 },
      ],
      style: 'orthogonal',
    },
    optimalBends: 3,
  },
  {
    name: 'opposite-side ports facing same direction',
    req: {
      from: { x: 0, y: 100, side: 'left' },
      to: { x: 200, y: 100, side: 'right' },
      obstacles: [],
      style: 'orthogonal',
    },
    optimalBends: 2,
  },
];

describe('orthogonal router fixtures', () => {
  for (const fx of FIXTURES) {
    it(`${fx.name}: bends ≤ optimal(${fx.optimalBends})+1`, async () => {
      const result = await routeOrthogonal(fx.req);
      expect(result.d).toMatch(/^M /);
      expect(result.bends).toBeLessThanOrEqual(fx.optimalBends + 1);
      expect(result.lengthPx).toBeGreaterThan(0);
    });
  }

  it('produces a deterministic path for the same input', async () => {
    const req = FIXTURES[3].req;
    const a = await routeOrthogonal(req);
    const b = await routeOrthogonal(req);
    expect(a.d).toBe(b.d);
    expect(a.bends).toBe(b.bends);
  });

  it('honors AbortSignal at A* level', async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    await expect(
      routeOrthogonal(FIXTURES[3].req, ctrl.signal),
    ).rejects.toMatchObject({ name: 'AbortError' });
  });
});

describe('roundedOrthogonalPath (#254 NetBird-style corners)', () => {
  const R = 8;

  it('keeps a straight two-point path as a bare line', () => {
    const d = roundedOrthogonalPath(
      [{ x: 0, y: 100 }, { x: 200, y: 100 }],
      R,
    );
    expect(d).toBe('M 0,100 L 200,100');
    expect(d).not.toContain('Q');
  });

  it('rounds an L-corner with a quadratic curve clamped to the radius', () => {
    // 100px horizontal run then 100px vertical drop; radius 8 fits both legs.
    const d = roundedOrthogonalPath(
      [{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }],
      R,
    );
    // Cut-in at (100-8, 0), control at the corner, cut-out at (100, 8).
    expect(d).toBe('M 0,0 L 92,0 Q 100,0 100,8 L 100,100');
  });

  it('clamps the radius to half the shortest adjacent leg', () => {
    // 6px vertical leg before the corner: radius clamps to 3.
    const d = roundedOrthogonalPath(
      [{ x: 0, y: 0 }, { x: 50, y: 0 }, { x: 50, y: 6 }, { x: 100, y: 6 }],
      R,
    );
    // First corner: min(8, 50/2, 6/2) = 3 cuts BOTH legs — cut-in at 47, cut-out
    // at (50,3); second corner: min(8, 3, 50/2) = 3 on both sides.
    expect(d).toBe('M 0,0 L 47,0 Q 50,0 50,3 L 50,3 Q 50,6 53,6 L 100,6');
  });

  it('keeps degenerate corners sharp instead of emitting collapsing curves', () => {
    const d = roundedOrthogonalPath(
      [{ x: 0, y: 0 }, { x: 20, y: 0 }, { x: 20, y: 0.2 }, { x: 60, y: 0.2 }],
      R,
    );
    expect(d).toContain('L 20,0');
    // The sub-pixel corner never rounds (radius clamps to 0.1 < 0.5).
    expect(d.split('Q').length - 1).toBe(0);
  });

  it('preserves endpoints exactly so the path lands on the port handles', () => {
    const d = roundedOrthogonalPath(
      [{ x: 12, y: 34 }, { x: 200, y: 34 }, { x: 200, y: 300 }, { x: 500, y: 300 }],
      R,
    );
    expect(d.startsWith('M 12,34')).toBe(true);
    expect(d.endsWith('L 500,300')).toBe(true);
  });
});
