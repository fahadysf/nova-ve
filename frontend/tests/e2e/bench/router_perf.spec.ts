// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-066 / US-071: orthogonal router perf bench.
 *
 * Note: PRD asked for a Playwright e2e bench, but the router has zero DOM
 * dependency, so this is implemented as a Vitest spec for speed and
 * simplicity. The CI workflow runs it via `npm run test -- bench/router_perf`.
 *
 * Asserts:
 *   - p95 over N=10 runs on the 200-node fixture ≤ 150ms
 *   - p95 over N=10 runs on the 500-node fixture ≤ 300ms
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { routeOrthogonal } from '$lib/services/router/orthogonal';
import type { Obstacle, RouteRequest } from '$lib/services/router';

interface Fixture {
  name: string;
  nodes: Array<{ id: number; x: number; y: number; w: number; h: number }>;
  links: Array<{
    from: { x: number; y: number; side: 'top' | 'right' | 'bottom' | 'left' };
    to: { x: number; y: number; side: 'top' | 'right' | 'bottom' | 'left' };
  }>;
}

function loadFixture(name: string): Fixture {
  const p = resolve(process.cwd(), 'tests/e2e/fixtures', `${name}.json`);
  return JSON.parse(readFileSync(p, 'utf-8'));
}

function p95(samples: number[]): number {
  const sorted = [...samples].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95));
  return sorted[idx];
}

async function timeRoute(fx: Fixture): Promise<number> {
  const obstacles: Obstacle[] = fx.nodes.map((n) => ({
    x: n.x,
    y: n.y,
    w: n.w,
    h: n.h,
  }));
  const t0 = performance.now();
  for (const link of fx.links) {
    const req: RouteRequest = {
      from: link.from,
      to: link.to,
      obstacles,
      style: 'orthogonal',
    };
    await routeOrthogonal(req);
  }
  return performance.now() - t0;
}

describe('router perf bench', () => {
  it('200-node fixture: p95 over 10 runs ≤ 150ms', async () => {
    const fx = loadFixture('router_bench_200');
    // Warm-up: JIT + heap shape stabilization.
    await timeRoute(fx);
    const samples: number[] = [];
    for (let i = 0; i < 10; i++) {
      samples.push(await timeRoute(fx));
    }
    const p = p95(samples);
    // eslint-disable-next-line no-console
    console.log(
      `[bench] router_bench_200: samples=${samples
        .map((s) => s.toFixed(1))
        .join(',')} p95=${p.toFixed(1)}ms`,
    );
    expect(p).toBeLessThanOrEqual(150);
  }, 60_000);

  it('500-node fixture: p95 over 10 runs ≤ 300ms', async () => {
    const fx = loadFixture('router_bench_500');
    await timeRoute(fx); // warm-up
    const samples: number[] = [];
    for (let i = 0; i < 10; i++) {
      samples.push(await timeRoute(fx));
    }
    const p = p95(samples);
    // eslint-disable-next-line no-console
    console.log(
      `[bench] router_bench_500: samples=${samples
        .map((s) => s.toFixed(1))
        .join(',')} p95=${p.toFixed(1)}ms`,
    );
    expect(p).toBeLessThanOrEqual(300);
  }, 120_000);
});
