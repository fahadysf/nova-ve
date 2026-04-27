// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-067 crossings-reduction pass:
 *   1. Single greedy sweep, no fixpoint loop (deterministic iteration count).
 *   2. Crossings are non-increasing.
 *   3. Idempotent: running twice in succession produces no further reduction.
 */

import { describe, it, expect } from 'vitest';
import {
  __test as ortho,
  reduceCrossings,
  countCrossings,
  type Segment,
} from '$lib/services/router/orthogonal';

function seededRand(seed: number): () => number {
  let s = seed >>> 0;
  return () => {
    s = (s * 1664525 + 1013904223) >>> 0;
    return s / 0xffffffff;
  };
}

/** Build a deterministic set of orthogonal segments with intentional crossings. */
function buildSegments(seed: number, n: number): Segment[] {
  const rand = seededRand(seed);
  const segs: Segment[] = [];
  for (let i = 0; i < n; i++) {
    const isHoriz = rand() < 0.5;
    if (isHoriz) {
      const y = Math.round(rand() * 200);
      const x1 = Math.round(rand() * 200);
      const x2 = x1 + 100 + Math.round(rand() * 200);
      segs.push({ x1, y1: y, x2, y2: y });
    } else {
      const x = Math.round(rand() * 400);
      const y1 = Math.round(rand() * 100);
      const y2 = y1 + 50 + Math.round(rand() * 200);
      segs.push({ x1: x, y1, x2: x, y2 });
    }
  }
  return segs;
}

describe('US-067 crossings-reduction pass', () => {
  it('runs a single sweep — never iterates to a fixpoint', () => {
    // The reference impl is `reduceCrossings`. Run it; then run it again on the
    // result. The second pass must produce a result identical to the first
    // (i.e. no further improvement is attempted by an outer loop).
    const segs = buildSegments(1234, 24);
    const once = reduceCrossings(segs);
    const twice = reduceCrossings(once);
    expect(JSON.stringify(twice)).toBe(JSON.stringify(once));
  });

  it('crossings are non-increasing after the sweep', () => {
    for (const seed of [11, 22, 33, 44, 55]) {
      const segs = buildSegments(seed, 30);
      const before = countCrossings(segs);
      const after = countCrossings(reduceCrossings(segs));
      expect(after).toBeLessThanOrEqual(before);
    }
  });

  it('is idempotent on its own output', () => {
    for (const seed of [101, 202, 303]) {
      const segs = buildSegments(seed, 18);
      const once = reduceCrossings(segs);
      const onceCount = countCrossings(once);
      const twice = reduceCrossings(once);
      const twiceCount = countCrossings(twice);
      expect(twiceCount).toBe(onceCount);
    }
  });

  it('no-op for empty / single-segment input', () => {
    expect(reduceCrossings([])).toEqual([]);
    const single: Segment[] = [{ x1: 0, y1: 0, x2: 100, y2: 0 }];
    expect(reduceCrossings(single)).toEqual(single);
  });

  it('countCrossings detects orthogonal crossings (sanity)', () => {
    const segs: Segment[] = [
      { x1: 0, y1: 50, x2: 100, y2: 50 }, // horizontal
      { x1: 50, y1: 0, x2: 50, y2: 100 }, // vertical, crosses
      { x1: 0, y1: 200, x2: 100, y2: 200 }, // horizontal, no cross
    ];
    expect(countCrossings(segs)).toBe(1);
  });

  it('exposes Grid + segmentsCross helpers for downstream use', () => {
    expect(typeof ortho.Grid).toBe('function');
    expect(typeof ortho.segmentsCross).toBe('function');
    expect(typeof ortho.reduceCrossings).toBe('function');
  });
});
