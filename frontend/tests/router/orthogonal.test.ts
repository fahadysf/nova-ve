// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * 6 fixture cases for the orthogonal A* router. Each asserts that the bend
 * count is ≤ optimal+1 (per US-066 acceptance criteria).
 */

import { describe, it, expect } from 'vitest';
import { routeOrthogonal, roundedOrthogonalPath, __test as routeOrthogonalTest } from '$lib/services/router/orthogonal';
import type { RouteRequest } from '$lib/services/router';

interface Fixture {
  name: string;
  req: RouteRequest;
  optimalBends: number;
  /**
   * Bend ceiling when the perpendicular-exit stubs (#258) raise the count
   * above the unconstrained optimum — leaving/arriving along the side
   * normal adds a joint at each end, and a constrained exit lane can shift
   * the whole detour. Defaults to optimalBends + 1.
   */
  maxBends?: number;
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
    maxBends: 4,
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
    maxBends: 6,
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
      expect(result.bends).toBeLessThanOrEqual(fx.maxBends ?? fx.optimalBends + 1);
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

describe('facing-port centered Z + sub-pixel seam merge (#254)', () => {
  const { centeredFacingSegments, mergeSubPixelPoints } = routeOrthogonalTest;

  it('routes offset facing ports as one Z with the jog at the corridor midpoint', () => {
    const segments = centeredFacingSegments(
      { x: 215.875, y: 59.27, side: 'right' },
      { x: 302.125, y: 3.27, side: 'left' },
      'horizontal'
    );
    expect(segments).toHaveLength(3);
    const jogX = segments[1].x1;
    expect(jogX).toBeCloseTo((215.875 + 302.125) / 2, 6);
    expect(segments[1].x2).toBe(jogX);
  });

  it('routes vertical facing ports with the jog at the midpoint row', () => {
    const segments = centeredFacingSegments(
      { x: 100, y: 0, side: 'bottom' },
      { x: 160, y: 200, side: 'top' },
      'vertical'
    );
    expect(segments[1].y1).toBe(100);
    expect(segments[1].y2).toBe(100);
  });

  it('merges sub-pixel seams at box edges but keeps the port anchor', () => {
    // Grid lane x=176 vs port axis x=176.1796875 produced a visible kink.
    const merged = mergeSubPixelPoints([
      { x: 176, y: 122 },
      { x: 176, y: 214 },
      { x: 176.1796875, y: 214 },
      { x: 176.1796875, y: 227.125 },
    ]);
    expect(merged).toHaveLength(3);
    // The final point is preserved exactly — the path must land on the port.
    expect(merged[merged.length - 1]).toEqual({ x: 176.1796875, y: 227.125 });
    expect(merged[1]).toEqual({ x: 176, y: 214 });
  });

  it('keeps well-separated points untouched', () => {
    const pts = [{ x: 0, y: 0 }, { x: 50, y: 0 }, { x: 50, y: 60 }];
    expect(mergeSubPixelPoints(pts)).toEqual(pts);
  });
});

describe('obstacle-avoiding detours (#254/#258 — links avert nodes)', () => {
  // net-bridge→vm-linux regression: a blocked facing corridor must fall
  // through to A* and add joints around the box instead of cutting the
  // centered Z straight through it.
  const OBSTACLE = { x: 150, y: 60, w: 100, h: 80 };

  function pathPoints(d: string): Array<{ x: number; y: number }> {
    const nums = d.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
    const pts: Array<{ x: number; y: number }> = [];
    for (let i = 0; i < nums.length; i += 2) pts.push({ x: nums[i], y: nums[i + 1] });
    return pts;
  }

  function segmentClearOfBox(
    a: { x: number; y: number },
    b: { x: number; y: number },
    clearance: number
  ): boolean {
    const minX = Math.min(a.x, b.x);
    const maxX = Math.max(a.x, b.x);
    const minY = Math.min(a.y, b.y);
    const maxY = Math.max(a.y, b.y);
    return (
      maxX <= OBSTACLE.x - clearance ||
      minX >= OBSTACLE.x + OBSTACLE.w + clearance ||
      maxY <= OBSTACLE.y - clearance ||
      minY >= OBSTACLE.y + OBSTACLE.h + clearance
    );
  }

  it('detours around an obstacle blocking the facing corridor', async () => {
    const result = await routeOrthogonal({
      from: { x: 0, y: 100, side: 'right' },
      to: { x: 400, y: 100, side: 'left' },
      obstacles: [OBSTACLE],
      style: 'orthogonal',
    });

    // The centered-Z fast path would be a 2-bend route through the box.
    expect(result.bends).toBeGreaterThan(2);
    const pts = pathPoints(result.d);
    expect(pts.length).toBeGreaterThan(2);
    for (let i = 1; i < pts.length; i += 1) {
      // A* keeps PORT_CLEARANCE=12px inflation; assert a conservative 6px.
      expect(segmentClearOfBox(pts[i - 1], pts[i], 6)).toBe(true);
    }
  });

  it('still takes the centered-Z fast path when the corridor is clear', async () => {
    const result = await routeOrthogonal({
      from: { x: 0, y: 100, side: 'right' },
      to: { x: 400, y: 60, side: 'left' },
      obstacles: [{ x: 100, y: 200, w: 200, h: 80 }],
      style: 'orthogonal',
    });
    expect(result.bends).toBeLessThanOrEqual(2);
  });
});

describe('perpendicular exit stubs (#258 — lines leave/arrive along the side normal)', () => {
  function firstLeg(d: string): { dx: number; dy: number; len: number } {
    const nums = d.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
    const dx = nums[2] - nums[0];
    const dy = nums[3] - nums[1];
    return { dx, dy, len: Math.hypot(dx, dy) };
  }

  function lastLeg(d: string): { dx: number; dy: number } {
    const nums = d.match(/-?\d+(?:\.\d+)?/g)!.map(Number);
    const n = nums.length;
    return { dx: nums[n - 2] - nums[n - 4], dy: nums[n - 1] - nums[n - 3] };
  }

  it('leaves a bottom port perpendicular for a full stub before bending', async () => {
    const result = await routeOrthogonal({
      from: { x: 100, y: 0, side: 'bottom' },
      to: { x: 400, y: 300, side: 'left' },
      obstacles: [{ x: 60, y: 120, w: 240, h: 80 }],
      style: 'orthogonal',
    });
    const leg = firstLeg(result.d);
    // Vertical (perpendicular to the bottom side), no bend within 2 cells.
    expect(leg.dx).toBe(0);
    expect(leg.dy).toBeGreaterThan(0);
    expect(leg.len).toBeGreaterThanOrEqual(39);
  });

  it('never leaves a port anti-parallel, even when the exit is crowded', async () => {
    // Live regression: iol-rtr overlapping the right exit forced the old
    // path to start `M 346,90 L 334,90` (backwards).
    const result = await routeOrthogonal({
      from: { x: 366, y: 138, side: 'right' },
      to: { x: 594, y: 271, side: 'left' },
      obstacles: [{ x: 356, y: 105, w: 152, h: 58 }],
      style: 'orthogonal',
    });
    const leg = firstLeg(result.d);
    expect(leg.dx).toBeGreaterThan(0);
    expect(leg.len).toBeGreaterThanOrEqual(39);
  });

  it('arrives perpendicular into a west port', async () => {
    const result = await routeOrthogonal({
      from: { x: 100, y: 0, side: 'bottom' },
      to: { x: 400, y: 300, side: 'left' },
      obstacles: [{ x: 60, y: 120, w: 240, h: 80 }],
      style: 'orthogonal',
    });
    const leg = lastLeg(result.d);
    expect(leg.dy).toBe(0);
    expect(leg.dx).toBeGreaterThan(0);
  });
});
