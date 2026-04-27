// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
  clampToPerimeter,
  placeInterfaces,
  portPositionToPoint,
  tToPortPosition,
} from '$lib/services/portLayout';

describe('portLayout.placeInterfaces', () => {
  it('returns an empty array for n = 0', () => {
    expect(placeInterfaces(0)).toEqual([]);
  });

  it('places one interface in the centre of the top edge', () => {
    // n=1 → t = 0.5 → quadrant index 2 → side "bottom", offset 0.0
    // (centre of perimeter, projected onto the bottom edge by the quadrant map).
    expect(placeInterfaces(1)).toEqual([{ side: 'bottom', offset: 0 }]);
  });

  it('places two interfaces opposite each other', () => {
    // n=2 → t = 0.25, 0.75 → side "right" (offset 0), side "left" (offset 0).
    expect(placeInterfaces(2)).toEqual([
      { side: 'right', offset: 0 },
      { side: 'left', offset: 0 },
    ]);
  });

  it('places three interfaces with one anchor per first three quadrants', () => {
    // t values 1/6, 0.5, 5/6.
    const positions = placeInterfaces(3);
    expect(positions).toHaveLength(3);
    expect(positions[0].side).toBe('top');
    expect(positions[0].offset).toBeCloseTo((1 / 6 - 0) / 0.25, 5);
    expect(positions[1]).toEqual({ side: 'bottom', offset: 0 });
    expect(positions[2].side).toBe('left');
    expect(positions[2].offset).toBeCloseTo((5 / 6 - 0.75) / 0.25, 5);
  });

  it('places four interfaces — one per side at offset 0.5', () => {
    // t values 0.125, 0.375, 0.625, 0.875 → centre of each quadrant.
    expect(placeInterfaces(4)).toEqual([
      { side: 'top', offset: 0.5 },
      { side: 'right', offset: 0.5 },
      { side: 'bottom', offset: 0.5 },
      { side: 'left', offset: 0.5 },
    ]);
  });

  it('places six interfaces with deterministic distribution', () => {
    const positions = placeInterfaces(6);
    expect(positions).toHaveLength(6);
    const sides = positions.map((p) => p.side);
    expect(sides).toEqual(['top', 'right', 'right', 'bottom', 'left', 'left']);
    // Within-quadrant offsets are evenly spaced.
    expect(positions[0].offset).toBeCloseTo(((1 / 12) - 0) / 0.25, 5);
    expect(positions[1].offset).toBeCloseTo((0.25 - 0.25) / 0.25, 5);
    expect(positions[2].offset).toBeCloseTo(((5 / 12) - 0.25) / 0.25, 5);
  });

  it('places eight interfaces — two per side', () => {
    const positions = placeInterfaces(8);
    expect(positions.map((p) => p.side)).toEqual([
      'top', 'top', 'right', 'right', 'bottom', 'bottom', 'left', 'left',
    ]);
    expect(positions[0].offset).toBeCloseTo(0.25, 5);
    expect(positions[1].offset).toBeCloseTo(0.75, 5);
    expect(positions[7].offset).toBeCloseTo(0.75, 5);
  });

  it('places twelve interfaces — three per side', () => {
    const positions = placeInterfaces(12);
    expect(positions).toHaveLength(12);
    const sideCounts = positions.reduce<Record<string, number>>((acc, p) => {
      acc[p.side] = (acc[p.side] ?? 0) + 1;
      return acc;
    }, {});
    expect(sideCounts).toEqual({ top: 3, right: 3, bottom: 3, left: 3 });
  });

  it('keeps every offset within [0, 1]', () => {
    for (const n of [1, 2, 3, 4, 6, 8, 12, 16, 24]) {
      const positions = placeInterfaces(n);
      for (const p of positions) {
        expect(p.offset).toBeGreaterThanOrEqual(0);
        expect(p.offset).toBeLessThanOrEqual(1);
      }
    }
  });
});

describe('portLayout.tToPortPosition', () => {
  it('clamps t = 0 to top edge start', () => {
    expect(tToPortPosition(0)).toEqual({ side: 'top', offset: 0 });
  });

  it('clamps t = 1 to left edge end', () => {
    expect(tToPortPosition(1)).toEqual({ side: 'left', offset: 1 });
  });

  it('returns top edge centre for t = 0.125', () => {
    expect(tToPortPosition(0.125)).toEqual({ side: 'top', offset: 0.5 });
  });
});

describe('portLayout.clampToPerimeter', () => {
  const box = { x: 100, y: 100, w: 200, h: 100 };

  it('snaps a point near the top edge to side "top"', () => {
    const pos = clampToPerimeter({ x: 200, y: 95 }, box);
    expect(pos.side).toBe('top');
    expect(pos.offset).toBeCloseTo(0.5, 5);
  });

  it('snaps a point near the right edge to side "right"', () => {
    const pos = clampToPerimeter({ x: 305, y: 150 }, box);
    expect(pos.side).toBe('right');
    expect(pos.offset).toBeCloseTo(0.5, 5);
  });

  it('snaps a point near the bottom edge to side "bottom"', () => {
    const pos = clampToPerimeter({ x: 250, y: 205 }, box);
    expect(pos.side).toBe('bottom');
    expect(pos.offset).toBeCloseTo(0.75, 5);
  });

  it('snaps a point near the left edge to side "left"', () => {
    const pos = clampToPerimeter({ x: 95, y: 175 }, box);
    expect(pos.side).toBe('left');
    expect(pos.offset).toBeCloseTo(0.75, 5);
  });

  it('round-trips a perimeter point through portPositionToPoint', () => {
    const pos = clampToPerimeter({ x: 200, y: 100 }, box);
    const reproduced = portPositionToPoint(pos, box);
    expect(reproduced.x).toBeCloseTo(200, 5);
    expect(reproduced.y).toBeCloseTo(100, 5);
  });
});
