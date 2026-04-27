// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-066 / US-067 / US-071: router quality bench.
 *
 * Asserts that crossings-per-edge ≤ 0.5 after the reduction sweep on the
 * 200-node fixture.
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  routeOrthogonal,
  reduceCrossings,
  segmentsCross,
  type Segment,
} from '$lib/services/router/orthogonal';
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

/** Parse an orthogonal SVG path "M x,y L x,y L x,y …" back into Segments. */
function pathToSegments(d: string): Segment[] {
  const tokens = d.trim().split(/\s+/);
  const out: Segment[] = [];
  let cx = 0;
  let cy = 0;
  let i = 0;
  while (i < tokens.length) {
    const t = tokens[i];
    if (t === 'M' || t === 'L') {
      const [xs, ys] = tokens[i + 1].split(',');
      const nx = Number(xs);
      const ny = Number(ys);
      if (t === 'L') {
        out.push({ x1: cx, y1: cy, x2: nx, y2: ny });
      }
      cx = nx;
      cy = ny;
      i += 2;
    } else {
      i++;
    }
  }
  return out;
}

/** Count only crossings between segments belonging to different paths. */
function countCrossPathCrossings(segments: Segment[]): number {
  let n = 0;
  for (let i = 0; i < segments.length; i++) {
    for (let j = i + 1; j < segments.length; j++) {
      if (segments[i].pathId === segments[j].pathId) continue;
      if (segmentsCross(segments[i], segments[j])) n++;
    }
  }
  return n;
}

describe('router quality bench', () => {
  it('200-node fixture: crossings-per-edge ≤ 0.5', async () => {
    const fx = loadFixture('router_bench_200');
    const obstacles: Obstacle[] = fx.nodes.map((n) => ({
      x: n.x,
      y: n.y,
      w: n.w,
      h: n.h,
    }));

    const allSegments: Segment[] = [];
    for (let i = 0; i < fx.links.length; i++) {
      const link = fx.links[i];
      const req: RouteRequest = {
        from: link.from,
        to: link.to,
        obstacles,
        style: 'orthogonal',
      };
      const r = await routeOrthogonal(req);
      const segs = pathToSegments(r.d);
      for (const s of segs) {
        s.pathId = i;
        allSegments.push(s);
      }
    }

    const reduced = reduceCrossings(allSegments);
    const crossings = countCrossPathCrossings(reduced);
    const perEdge = crossings / fx.links.length;
    // eslint-disable-next-line no-console
    console.log(
      `[bench] router_bench_200 quality: links=${fx.links.length} segments=${reduced.length} cross-path-crossings=${crossings} perEdge=${perEdge.toFixed(3)}`,
    );
    expect(perEdge).toBeLessThanOrEqual(0.5);
  }, 120_000);
});
