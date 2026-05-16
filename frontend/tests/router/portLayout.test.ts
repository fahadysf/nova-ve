// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
  clampToPerimeter,
  closestPortPositionToBox,
  PORT_HANDLE_MIN_CENTER_GAP_PX,
  placeInterfaces,
  portPositionToPoint,
  separatePortPositions,
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

describe('portLayout.closestPortPositionToBox', () => {
  it('places the endpoint on the side facing the counterpart box', () => {
    const source = { x: 100, y: 100, w: 120, h: 60 };
    const target = { x: 300, y: 110, w: 80, h: 40 };

    const pos = closestPortPositionToBox(source, target);

    expect(pos.side).toBe('right');
    expect(pos.offset).toBeCloseTo(0.5, 5);
  });

  it('uses the closest outline point when the counterpart is below the box', () => {
    const source = { x: 100, y: 100, w: 120, h: 60 };
    const target = { x: 150, y: 165, w: 40, h: 20 };

    const pos = closestPortPositionToBox(source, target);

    expect(pos.side).toBe('bottom');
    expect(pos.offset).toBeCloseTo(0.58333, 4);
  });
});

describe('portLayout.separatePortPositions', () => {
  it('keeps same-side handle edges at least 1px apart', () => {
    const box = { x: 0, y: 0, w: 100, h: 100 };

    const separated = separatePortPositions(
      [
        { side: 'right', offset: 0.5 },
        { side: 'right', offset: 0.5 },
      ],
      box
    );

    const centerGapPx = Math.abs(separated[1].offset - separated[0].offset) * box.h;
    expect(centerGapPx).toBeGreaterThanOrEqual(PORT_HANDLE_MIN_CENTER_GAP_PX);
  });

  it('does not move ports on different sides', () => {
    const box = { x: 0, y: 0, w: 100, h: 100 };
    const positions = [
      { side: 'right', offset: 0.5 },
      { side: 'left', offset: 0.5 },
    ] as const;

    expect(separatePortPositions(positions, box)).toEqual(positions);
  });

  it('spreads crowded side ports within the boundary when exact spacing is impossible', () => {
    const box = { x: 0, y: 0, w: 100, h: 20 };

    const separated = separatePortPositions(
      [
        { side: 'right', offset: 0.5 },
        { side: 'right', offset: 0.5 },
        { side: 'right', offset: 0.5 },
      ],
      box
    );

    expect(separated.map((position) => position.offset)).toEqual([0, 0.5, 1]);
  });

  it('separates handles that collide across a shared corner', () => {
    const box = { x: 0, y: 0, w: 100, h: 100 };

    const separated = separatePortPositions(
      [
        { side: 'top', offset: 0 },
        { side: 'left', offset: 0 },
      ],
      box
    );
    const points = separated.map((position) => portPositionToPoint(position, box));
    const centerGapPx = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);

    expect(centerGapPx).toBeGreaterThanOrEqual(PORT_HANDLE_MIN_CENTER_GAP_PX);
  });
});
