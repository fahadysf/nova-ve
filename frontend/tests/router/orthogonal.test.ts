// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * 6 fixture cases for the orthogonal A* router. Each asserts that the bend
 * count is ≤ optimal+1 (per US-066 acceptance criteria).
 */

import { describe, it, expect } from 'vitest';
import { routeOrthogonal } from '$lib/services/router/orthogonal';
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
