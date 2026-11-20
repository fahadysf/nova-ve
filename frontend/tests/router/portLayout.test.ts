// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import {
  clampToPerimeter,
  closestPortPositionToBox,
  bestRouteAwarePortPosition,
  nudgeAwayFromPorts,
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

describe('portLayout.separatePortPositions — center bias (#258)', () => {
  const box = { x: 0, y: 0, w: 100, h: 100 };

  it('pulls a lone port to the exact center of its side', () => {
    const separated = separatePortPositions(
      [{ side: 'right', offset: 0.8 }],
      box
    );
    expect(separated).toEqual([{ side: 'right', offset: 0.5 }]);
  });

  it('fans a clustered group out symmetrically around the side center', () => {
    const separated = separatePortPositions(
      [
        { side: 'right', offset: 0.5 },
        { side: 'right', offset: 0.5 },
        { side: 'right', offset: 0.5 },
      ],
      box
    );
    const offsets = separated.map((position) => position.offset);
    // Symmetric about 0.5, evenly spaced at the minimum center gap.
    expect(offsets[1]).toBeCloseTo(0.5, 6);
    expect((offsets[0] + offsets[2]) / 2).toBeCloseTo(0.5, 6);
    expect((offsets[1] - offsets[0]) * box.h).toBeGreaterThanOrEqual(PORT_HANDLE_MIN_CENTER_GAP_PX);
    expect((offsets[2] - offsets[1]) * box.h).toBeGreaterThanOrEqual(PORT_HANDLE_MIN_CENTER_GAP_PX);
  });

  it('preserves projected order so link stubs do not cross', () => {
    const separated = separatePortPositions(
      [
        { side: 'right', offset: 0.9 },
        { side: 'right', offset: 0.1 },
      ],
      box
    );
    // The 0.9-ideal port stays the lower of the two on the side.
    expect(separated[0].offset).toBeGreaterThan(separated[1].offset);
    expect((separated[0].offset + separated[1].offset) / 2).toBeCloseTo(0.5, 6);
  });
});

describe('portLayout.nudgeAwayFromPorts — dotted open-slot aversion (#258)', () => {
  const box = { x: 0, y: 0, w: 100, h: 100 };

  it('keeps the canonical position when no occupant is within the min gap', () => {
    const nudged = nudgeAwayFromPorts(
      { side: 'right', offset: 0.5 },
      [{ side: 'right', offset: 0.95 }],
      box
    );
    expect(nudged).toEqual({ side: 'right', offset: 0.5 });
  });

  it('shifts to the nearest free stretch when a same-side port sits too close', () => {
    // Occupant at 0.48 leaves [0.59, 1] as the nearest feasible stretch;
    // 0.59 is closer to the canonical 0.5 than the 0.37 cap of [0, 0.37].
    const nudged = nudgeAwayFromPorts(
      { side: 'right', offset: 0.5 },
      [{ side: 'right', offset: 0.48 }],
      box
    );
    expect(nudged.side).toBe('right');
    expect(nudged.offset).toBeGreaterThanOrEqual(0.48 + PORT_HANDLE_MIN_CENTER_GAP_PX / box.h - 1e-6);
    expect(nudged.offset).toBeCloseTo(0.59, 6);
  });

  it('picks the other direction when the occupant sits below the canonical spot', () => {
    const nudged = nudgeAwayFromPorts(
      { side: 'right', offset: 0.5 },
      [{ side: 'right', offset: 0.55 }],
      box
    );
    expect(nudged.offset).toBeCloseTo(0.55 - PORT_HANDLE_MIN_CENTER_GAP_PX / box.h, 6);
  });

  it('never moves the placed ports themselves', () => {
    const occupant = { side: 'right', offset: 0.48 } as const;
    nudgeAwayFromPorts({ side: 'right', offset: 0.5 }, [occupant], box);
    expect(occupant.offset).toBe(0.48);
  });

  it('pushes clear of a perpendicular occupant near a shared corner', () => {
    const nudged = nudgeAwayFromPorts(
      { side: 'right', offset: 0 },
      [{ side: 'top', offset: 0.95 }],
      box
    );
    const slotPoint = portPositionToPoint(nudged, box);
    const occupantPoint = portPositionToPoint({ side: 'top', offset: 0.95 }, box);
    const distance = Math.hypot(slotPoint.x - occupantPoint.x, slotPoint.y - occupantPoint.y);
    expect(distance).toBeGreaterThanOrEqual(PORT_HANDLE_MIN_CENTER_GAP_PX);
    expect(nudged.side).toBe('right');
    expect(nudged.offset).toBeGreaterThan(0);
  });

  it('hugs the less-crowded edge when the side is saturated with ports', () => {
    const nudged = nudgeAwayFromPorts(
      { side: 'right', offset: 0.5 },
      [
        { side: 'right', offset: 0.1 },
        { side: 'right', offset: 0.3 },
        { side: 'right', offset: 0.5 },
        { side: 'right', offset: 0.7 },
        { side: 'right', offset: 0.9 },
      ],
      box
    );
    expect(nudged.offset).toBe(0);
  });

  it('returns the slot untouched when there are no occupants at all', () => {
    const nudged = nudgeAwayFromPorts({ side: 'bottom', offset: 0 }, [], box);
    expect(nudged).toEqual({ side: 'bottom', offset: 0 });
  });
});

describe('portLayout.bestRouteAwarePortPosition — no backward-hugging ports (#258)', () => {
  // Exact live ST-Demo geometry (flow coords) where the gap-facing right
  // port's exit was boxed in by iol-rtr and the route doubled back behind
  // net-bridge. The port must relocate to the unconstricted forward side.
  const NET = (x: number, y: number) => ({ x, y, w: 130, h: 50 });
  const NODE = (x: number, y: number) => ({ x, y, w: 152, h: 58 });

  const netBridge = NET(216, 65);
  const vmLinux = NODE(518, 242);
  const unrelated = [NODE(365, 53), NODE(162, 243), NODE(573, -65), NET(430, -85)];

  it('relocates a boxed-in gap-facing port to the forward-unconstricted side', () => {
    // closestPortPositionToBox alone would pick 'right' (gap 172 > 121).
    expect(closestPortPositionToBox(netBridge, vmLinux).side).toBe('right');

    const relocated = bestRouteAwarePortPosition(netBridge, vmLinux, unrelated);
    expect(relocated.side).toBe('bottom');
    // Projection toward the peer center, clamped to the side as before.
    expect(relocated.offset).toBe(1);
  });

  it('keeps the gap-facing side when its exit strip is clear', () => {
    const kept = bestRouteAwarePortPosition(netBridge, vmLinux, [
      NODE(365, -200), // far away — nothing near the exit
    ]);
    expect(kept.side).toBe('right');
  });

  it('reduces to closestPortPositionToBox when there are no obstacles', () => {
    expect(bestRouteAwarePortPosition(netBridge, vmLinux, [])).toEqual(
      closestPortPositionToBox(netBridge, vmLinux)
    );
  });

  it('falls back to the original side when every side is boxed in', () => {
    // Ring of cards around every face of the source box.
    const ring = [
      NODE(216 - 24, 65 - 24), // overlapping top-left front
      NODE(346 - 12, 65 - 24), // right front
      NODE(216 - 12, 115 - 12), // bottom front
      NODE(216 - 12, 65 - 12), // inside-adjacent top front
      NODE(400, 30), // right, again
    ];
    const result = bestRouteAwarePortPosition(netBridge, vmLinux, ring);
    expect(result.side).toBe(closestPortPositionToBox(netBridge, vmLinux).side);
  });

  it('prefers the unconstricted side that heads most toward the peer', () => {
    // Peer far to the bottom-right; a wall blocks only the right exit strip
    // (its inflated left edge stops short of the bottom corner strip), and
    // top/left are unconstricted but point away — bottom must win.
    const source = NODE(0, 0);
    const target = NODE(600, 400);
    const wall = NODE(175, 0);
    const result = bestRouteAwarePortPosition(source, target, [wall]);
    expect(result.side).toBe('bottom');
  });
});

describe('portLayout.bestRouteAwarePortPosition — never flees backwards (#258)', () => {
  // Live user arrangement: net-bridge moved so iol-rtr overlaps its right
  // exit AND the projected bottom corner. Right and bottom are both
  // constricted; top/left are clear but point AWAY from the bottom-right
  // target — the port must keep the gap-facing right side instead of
  // relocating to top.
  const NET = (x: number, y: number) => ({ x, y, w: 130, h: 50 });
  const NODE = (x: number, y: number) => ({ x, y, w: 152, h: 58 });

  const netBridge = NET(236, 88);
  const vmLinux = NODE(518, 242);
  const overlappingIolRtr = NODE(356, 105);
  const ctrAlpine = NODE(114, 225);

  it('keeps the facing side when every forward side is constricted', () => {
    const result = bestRouteAwarePortPosition(netBridge, vmLinux, [
      overlappingIolRtr,
      ctrAlpine,
    ]);
    expect(result.side).toBe('right');
  });
});
