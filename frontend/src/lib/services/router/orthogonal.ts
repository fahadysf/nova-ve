// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * Orthogonal A* router with crossings-reduction sweep.
 *
 * - Visibility grid: 20px cells. Cells overlapping any obstacle inflated by
 *   PORT_CLEARANCE = 12px are blocked.
 * - A*: 4-connected; cost = manhattan_step + BEND_PENALTY * is_bend.
 *   Tie-break on lower bend count when f-scores are equal.
 * - Path emission: collinear cells coalesce into single segments.
 * - Crossings reduction: single greedy sweep over parallel runs in each
 *   row/column band; swap adjacent channel offsets when it strictly reduces
 *   crossings. O(R log R + R). Idempotent.
 * - AbortController: the A* loop checks `signal.aborted` every 256 expansions.
 */

import type {
  Obstacle,
  PortRef,
  RouteRequest,
  RouteResult,
} from './index';

// ---------- tuning constants ------------------------------------------------

export const GRID_STEP = 20;
export const PORT_CLEARANCE = 12;
export const BEND_PENALTY = 1.0;
export const LANE_WIDTH = 12; // half of "2 * laneWidth = 24px" band width

const ABORT_CHECK_EVERY = 256;

// ---------- public types ----------------------------------------------------

/** Axis-parallel segment used by the crossings-reduction pass. */
export interface Segment {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** Stable id per source path, useful for swap bookkeeping. */
  pathId?: number;
}

// ---------- main entry ------------------------------------------------------

export async function routeOrthogonal(
  req: RouteRequest,
  signal?: AbortSignal,
): Promise<RouteResult> {
  if (signal?.aborted) {
    throw new DOMException('Route aborted', 'AbortError');
  }

  const { from, to, obstacles } = req;

  // Compute bounds based only on the from/to box (with padding). Filter
  // obstacles to those that intersect this window — A*'s grid scales with
  // the bbox area, so clipping irrelevant obstacles is critical for perf
  // on large canvases.
  const bounds = computeRouteBounds(from, to, GRID_STEP * 6);
  const localObstacles = filterObstacles(obstacles, bounds, PORT_CLEARANCE);
  const grid = buildGrid(bounds, localObstacles, PORT_CLEARANCE, GRID_STEP);

  // Snap endpoints to nearest grid cells, free them so A* can enter/exit.
  const startCell = grid.cellAt(from.x, from.y);
  const endCell = grid.cellAt(to.x, to.y);
  grid.unblock(startCell.cx, startCell.cy);
  grid.unblock(endCell.cx, endCell.cy);

  // Carve a one-cell exit lane in the port-tangent direction so the path
  // does not fight the inflated obstacle of its own host node.
  carveExit(grid, from, startCell);
  carveExit(grid, to, endCell);

  const cells = await runAStar(grid, startCell, endCell, signal);

  // Convert the cell path to world segments anchored at the actual port world
  // coords (so the rendered path lands exactly on the handle).
  const segments = emitSegments(cells, grid, from, to);

  // Crossings reduction is a no-op for a single edge, but the helper is
  // exposed for the multi-edge pass driven by the canvas.
  const reduced = reduceCrossings(segments);

  // Build path string + metrics.
  const { d, bends, lengthPx } = serialize(reduced);
  return { d, bends, lengthPx };
}

// ---------- bounds + grid ---------------------------------------------------

interface Bounds {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

function computeBounds(
  from: PortRef,
  to: PortRef,
  obstacles: Obstacle[],
  pad: number,
): Bounds {
  let minX = Math.min(from.x, to.x);
  let minY = Math.min(from.y, to.y);
  let maxX = Math.max(from.x, to.x);
  let maxY = Math.max(from.y, to.y);
  for (const o of obstacles) {
    if (o.x < minX) minX = o.x;
    if (o.y < minY) minY = o.y;
    if (o.x + o.w > maxX) maxX = o.x + o.w;
    if (o.y + o.h > maxY) maxY = o.y + o.h;
  }
  return {
    minX: Math.floor(minX - pad),
    minY: Math.floor(minY - pad),
    maxX: Math.ceil(maxX + pad),
    maxY: Math.ceil(maxY + pad),
  };
}

/** Bounds containing only the from/to ports plus padding (no obstacles). */
function computeRouteBounds(from: PortRef, to: PortRef, pad: number): Bounds {
  return {
    minX: Math.floor(Math.min(from.x, to.x) - pad),
    minY: Math.floor(Math.min(from.y, to.y) - pad),
    maxX: Math.ceil(Math.max(from.x, to.x) + pad),
    maxY: Math.ceil(Math.max(from.y, to.y) + pad),
  };
}

/** Keep only obstacles whose inflated bbox intersects the bounds. */
function filterObstacles(
  obstacles: Obstacle[],
  bounds: Bounds,
  clearance: number,
): Obstacle[] {
  const out: Obstacle[] = [];
  for (const o of obstacles) {
    const ox0 = o.x - clearance;
    const oy0 = o.y - clearance;
    const ox1 = o.x + o.w + clearance;
    const oy1 = o.y + o.h + clearance;
    if (
      ox1 < bounds.minX ||
      oy1 < bounds.minY ||
      ox0 > bounds.maxX ||
      oy0 > bounds.maxY
    ) {
      continue;
    }
    out.push(o);
  }
  return out;
}

interface GridCell {
  cx: number;
  cy: number;
}

class Grid {
  readonly bounds: Bounds;
  readonly step: number;
  readonly cols: number;
  readonly rows: number;
  /** 1 = blocked, 0 = free. */
  private blocked: Uint8Array;

  constructor(bounds: Bounds, step: number) {
    this.bounds = bounds;
    this.step = step;
    this.cols = Math.max(1, Math.ceil((bounds.maxX - bounds.minX) / step) + 1);
    this.rows = Math.max(1, Math.ceil((bounds.maxY - bounds.minY) / step) + 1);
    this.blocked = new Uint8Array(this.cols * this.rows);
  }

  idx(cx: number, cy: number): number {
    return cy * this.cols + cx;
  }

  inBounds(cx: number, cy: number): boolean {
    return cx >= 0 && cy >= 0 && cx < this.cols && cy < this.rows;
  }

  block(cx: number, cy: number): void {
    if (this.inBounds(cx, cy)) this.blocked[this.idx(cx, cy)] = 1;
  }

  unblock(cx: number, cy: number): void {
    if (this.inBounds(cx, cy)) this.blocked[this.idx(cx, cy)] = 0;
  }

  isBlocked(cx: number, cy: number): boolean {
    if (!this.inBounds(cx, cy)) return true;
    return this.blocked[this.idx(cx, cy)] === 1;
  }

  /** Snap a world point to the nearest cell. */
  cellAt(x: number, y: number): GridCell {
    const cx = clamp(
      Math.round((x - this.bounds.minX) / this.step),
      0,
      this.cols - 1,
    );
    const cy = clamp(
      Math.round((y - this.bounds.minY) / this.step),
      0,
      this.rows - 1,
    );
    return { cx, cy };
  }

  /** World coords for the centre of a cell. */
  worldOf(cx: number, cy: number): { x: number; y: number } {
    return {
      x: this.bounds.minX + cx * this.step,
      y: this.bounds.minY + cy * this.step,
    };
  }
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}

function buildGrid(
  bounds: Bounds,
  obstacles: Obstacle[],
  clearance: number,
  step: number,
): Grid {
  const grid = new Grid(bounds, step);
  for (const o of obstacles) {
    const x0 = o.x - clearance;
    const y0 = o.y - clearance;
    const x1 = o.x + o.w + clearance;
    const y1 = o.y + o.h + clearance;
    const cx0 = clamp(
      Math.floor((x0 - bounds.minX) / step),
      0,
      grid.cols - 1,
    );
    const cy0 = clamp(
      Math.floor((y0 - bounds.minY) / step),
      0,
      grid.rows - 1,
    );
    const cx1 = clamp(
      Math.ceil((x1 - bounds.minX) / step),
      0,
      grid.cols - 1,
    );
    const cy1 = clamp(
      Math.ceil((y1 - bounds.minY) / step),
      0,
      grid.rows - 1,
    );
    for (let cy = cy0; cy <= cy1; cy++) {
      for (let cx = cx0; cx <= cx1; cx++) {
        grid.block(cx, cy);
      }
    }
  }
  return grid;
}

/** Free a one-cell exit in the tangent direction of the port. */
function carveExit(grid: Grid, port: PortRef, cell: GridCell): void {
  let dx = 0;
  let dy = 0;
  switch (port.side) {
    case 'top':
      dy = -1;
      break;
    case 'bottom':
      dy = 1;
      break;
    case 'left':
      dx = -1;
      break;
    case 'right':
      dx = 1;
      break;
  }
  const exitCx = cell.cx + dx;
  const exitCy = cell.cy + dy;
  if (grid.inBounds(exitCx, exitCy)) {
    grid.unblock(exitCx, exitCy);
  }
}

// ---------- min-heap priority queue (binary) -------------------------------

/** Each node: (f-score, bend-tiebreak, index). Lower f wins; ties → fewer bends. */
class MinHeap {
  private data: number[][] = [];

  push(item: number[]): void {
    this.data.push(item);
    this.siftUp(this.data.length - 1);
  }

  pop(): number[] | undefined {
    if (this.data.length === 0) return undefined;
    const top = this.data[0];
    const last = this.data.pop()!;
    if (this.data.length > 0) {
      this.data[0] = last;
      this.siftDown(0);
    }
    return top;
  }

  size(): number {
    return this.data.length;
  }

  private less(a: number[], b: number[]): boolean {
    if (a[0] !== b[0]) return a[0] < b[0];
    if (a[1] !== b[1]) return a[1] < b[1];
    return a[2] < b[2];
  }

  private siftUp(i: number): void {
    const data = this.data;
    while (i > 0) {
      const parent = (i - 1) >> 1;
      if (this.less(data[i], data[parent])) {
        [data[i], data[parent]] = [data[parent], data[i]];
        i = parent;
      } else {
        break;
      }
    }
  }

  private siftDown(i: number): void {
    const data = this.data;
    const n = data.length;
    while (true) {
      const l = i * 2 + 1;
      const r = i * 2 + 2;
      let smallest = i;
      if (l < n && this.less(data[l], data[smallest])) smallest = l;
      if (r < n && this.less(data[r], data[smallest])) smallest = r;
      if (smallest === i) break;
      [data[i], data[smallest]] = [data[smallest], data[i]];
      i = smallest;
    }
  }
}

// ---------- A* --------------------------------------------------------------

const DIRS: ReadonlyArray<[number, number]> = [
  [1, 0],
  [-1, 0],
  [0, 1],
  [0, -1],
];

async function runAStar(
  grid: Grid,
  start: GridCell,
  goal: GridCell,
  signal?: AbortSignal,
): Promise<GridCell[]> {
  const cols = grid.cols;
  const rows = grid.rows;
  const total = cols * rows;
  const startIdx = start.cy * cols + start.cx;
  const goalIdx = goal.cy * cols + goal.cx;

  // Per-cell scalars. -1 means unset.
  const gScore = new Float64Array(total);
  const fScore = new Float64Array(total);
  const bends = new Int32Array(total);
  const cameFrom = new Int32Array(total);
  const dirIn = new Int8Array(total); // 0=none, 1=horiz, 2=vert
  const closed = new Uint8Array(total);
  for (let i = 0; i < total; i++) {
    gScore[i] = Infinity;
    fScore[i] = Infinity;
    cameFrom[i] = -1;
  }
  gScore[startIdx] = 0;
  fScore[startIdx] = heuristic(start, goal);
  bends[startIdx] = 0;
  dirIn[startIdx] = 0;

  let serial = 0;
  const open = new MinHeap();
  open.push([fScore[startIdx], 0, serial++, startIdx]);

  let expansions = 0;

  while (open.size() > 0) {
    expansions++;
    if (
      signal &&
      expansions % ABORT_CHECK_EVERY === 0 &&
      signal.aborted
    ) {
      throw new DOMException('Route aborted', 'AbortError');
    }

    const top = open.pop()!;
    const idx = top[3];
    if (closed[idx]) continue;
    closed[idx] = 1;
    if (idx === goalIdx) {
      return reconstruct(cameFrom, idx, cols);
    }

    const cx = idx % cols;
    const cy = (idx - cx) / cols;
    const incoming = dirIn[idx];

    for (const [dx, dy] of DIRS) {
      const nx = cx + dx;
      const ny = cy + dy;
      if (nx < 0 || ny < 0 || nx >= cols || ny >= rows) continue;
      const nIdx = ny * cols + nx;
      if (closed[nIdx]) continue;
      if (grid.isBlocked(nx, ny)) continue;

      const stepDir = dx !== 0 ? 1 : 2; // 1=horiz, 2=vert
      const isBend = incoming !== 0 && incoming !== stepDir;
      const stepCost = 1 + (isBend ? BEND_PENALTY : 0);
      const tentativeG = gScore[idx] + stepCost;
      if (tentativeG >= gScore[nIdx]) continue;

      cameFrom[nIdx] = idx;
      gScore[nIdx] = tentativeG;
      bends[nIdx] = bends[idx] + (isBend ? 1 : 0);
      dirIn[nIdx] = stepDir;
      const h = heuristic({ cx: nx, cy: ny }, goal);
      fScore[nIdx] = tentativeG + h;
      open.push([fScore[nIdx], bends[nIdx], serial++, nIdx]);
    }
  }

  // No path found — fall back to a direct L-route between start and goal.
  return fallbackL(start, goal);
}

function heuristic(a: GridCell, b: GridCell): number {
  return Math.abs(a.cx - b.cx) + Math.abs(a.cy - b.cy);
}

function reconstruct(cameFrom: Int32Array, endIdx: number, cols: number): GridCell[] {
  const path: GridCell[] = [];
  let cur = endIdx;
  while (cur !== -1) {
    const cx = cur % cols;
    const cy = (cur - cx) / cols;
    path.push({ cx, cy });
    cur = cameFrom[cur];
  }
  path.reverse();
  return path;
}

function fallbackL(start: GridCell, goal: GridCell): GridCell[] {
  // Pure deterministic L-bend (right then up/down).
  const out: GridCell[] = [{ cx: start.cx, cy: start.cy }];
  const stepX = goal.cx > start.cx ? 1 : -1;
  for (let cx = start.cx + stepX; cx !== goal.cx + stepX; cx += stepX) {
    out.push({ cx, cy: start.cy });
    if (cx === goal.cx) break;
  }
  const stepY = goal.cy > start.cy ? 1 : -1;
  for (let cy = start.cy + stepY; cy !== goal.cy + stepY; cy += stepY) {
    out.push({ cx: goal.cx, cy });
    if (cy === goal.cy) break;
  }
  return out;
}

// ---------- segment emission -----------------------------------------------

export function emitSegments(
  cells: GridCell[],
  grid: Grid,
  from: PortRef,
  to: PortRef,
): Segment[] {
  if (cells.length === 0) {
    return [{ x1: from.x, y1: from.y, x2: to.x, y2: to.y }];
  }

  // World coords for each cell, with first/last replaced by actual ports.
  const pts: Array<{ x: number; y: number }> = cells.map((c) =>
    grid.worldOf(c.cx, c.cy),
  );
  pts[0] = { x: from.x, y: from.y };
  pts[pts.length - 1] = { x: to.x, y: to.y };

  // Insert an explicit corner if the first or last segment isn't axis-aligned
  // (because the port may not align with grid centres).
  if (pts.length >= 2) {
    const a = pts[0];
    const b = pts[1];
    if (a.x !== b.x && a.y !== b.y) {
      // Drop a corner that respects the port tangent direction.
      const corner =
        from.side === 'left' || from.side === 'right'
          ? { x: b.x, y: a.y }
          : { x: a.x, y: b.y };
      pts.splice(1, 0, corner);
    }
  }
  if (pts.length >= 2) {
    const a = pts[pts.length - 2];
    const b = pts[pts.length - 1];
    if (a.x !== b.x && a.y !== b.y) {
      const corner =
        to.side === 'left' || to.side === 'right'
          ? { x: a.x, y: b.y }
          : { x: b.x, y: a.y };
      pts.splice(pts.length - 1, 0, corner);
    }
  }

  // Collapse collinear runs.
  const collapsed: Array<{ x: number; y: number }> = [pts[0]];
  for (let i = 1; i < pts.length - 1; i++) {
    const prev = collapsed[collapsed.length - 1];
    const cur = pts[i];
    const next = pts[i + 1];
    const collinearH = prev.y === cur.y && cur.y === next.y;
    const collinearV = prev.x === cur.x && cur.x === next.x;
    if (collinearH || collinearV) continue;
    collapsed.push(cur);
  }
  collapsed.push(pts[pts.length - 1]);

  // Emit segments. If a pair is diagonal (shouldn't happen post-corner-insert),
  // split it with an L-bend.
  const segments: Segment[] = [];
  for (let i = 0; i < collapsed.length - 1; i++) {
    const a = collapsed[i];
    const b = collapsed[i + 1];
    if (a.x === b.x || a.y === b.y) {
      segments.push({ x1: a.x, y1: a.y, x2: b.x, y2: b.y });
    } else {
      // Defensive split.
      const mid = { x: b.x, y: a.y };
      segments.push({ x1: a.x, y1: a.y, x2: mid.x, y2: mid.y });
      segments.push({ x1: mid.x, y1: mid.y, x2: b.x, y2: b.y });
    }
  }

  return segments;
}

// ---------- crossings reduction (US-067) -----------------------------------

/**
 * Single greedy sweep over axis-parallel segments: bucket by lane band, sort
 * each bucket by entry coordinate, swap adjacent pairs only when it strictly
 * reduces crossings against the orthogonal lanes. No fixpoint loop; idempotent.
 */
export function reduceCrossings(segments: Segment[]): Segment[] {
  if (segments.length <= 1) return segments;
  const out = segments.map((s) => ({ ...s }));
  const horiz: number[] = [];
  const vert: number[] = [];
  for (let i = 0; i < out.length; i++) {
    const s = out[i];
    if (s.y1 === s.y2) horiz.push(i);
    else if (s.x1 === s.x2) vert.push(i);
  }

  // Group horizontals by lane band along Y (band = 2 * LANE_WIDTH).
  const bandSize = 2 * LANE_WIDTH;
  const hBands = new Map<number, number[]>();
  for (const i of horiz) {
    const band = Math.round(out[i].y1 / bandSize);
    let arr = hBands.get(band);
    if (!arr) {
      arr = [];
      hBands.set(band, arr);
    }
    arr.push(i);
  }
  const vBands = new Map<number, number[]>();
  for (const i of vert) {
    const band = Math.round(out[i].x1 / bandSize);
    let arr = vBands.get(band);
    if (!arr) {
      arr = [];
      vBands.set(band, arr);
    }
    arr.push(i);
  }

  // Single sweep — sort each band by entry-x (or entry-y), then for each pair
  // of adjacent segments compute the would-be crossing count if we swap their
  // channel offsets and commit if it strictly drops.
  for (const [, group] of hBands) {
    sweepGroup(group, out, vert, 'h');
  }
  for (const [, group] of vBands) {
    sweepGroup(group, out, horiz, 'v');
  }

  return out;
}

function sweepGroup(
  group: number[],
  segments: Segment[],
  ortho: number[],
  axis: 'h' | 'v',
): void {
  if (group.length < 2) return;
  // Sort by entry coordinate (the smaller of x1/x2 for horiz, y1/y2 for vert).
  if (axis === 'h') {
    group.sort((a, b) => {
      const ea = Math.min(segments[a].x1, segments[a].x2);
      const eb = Math.min(segments[b].x1, segments[b].x2);
      return ea - eb;
    });
  } else {
    group.sort((a, b) => {
      const ea = Math.min(segments[a].y1, segments[a].y2);
      const eb = Math.min(segments[b].y1, segments[b].y2);
      return ea - eb;
    });
  }

  for (let k = 0; k < group.length - 1; k++) {
    const i = group[k];
    const j = group[k + 1];
    const before = pairCrossings(segments[i], ortho, segments) +
      pairCrossings(segments[j], ortho, segments);
    swapChannel(segments[i], segments[j], axis);
    const after = pairCrossings(segments[i], ortho, segments) +
      pairCrossings(segments[j], ortho, segments);
    if (after >= before) {
      // Revert — strict-improve only.
      swapChannel(segments[i], segments[j], axis);
    }
  }
}

function swapChannel(a: Segment, b: Segment, axis: 'h' | 'v'): void {
  if (axis === 'h') {
    const ay = a.y1;
    a.y1 = b.y1;
    a.y2 = b.y2;
    b.y1 = ay;
    b.y2 = ay;
  } else {
    const ax = a.x1;
    a.x1 = b.x1;
    a.x2 = b.x2;
    b.x1 = ax;
    b.x2 = ax;
  }
}

function pairCrossings(s: Segment, others: number[], all: Segment[]): number {
  let count = 0;
  for (const oi of others) {
    if (segmentsCross(s, all[oi])) count++;
  }
  return count;
}

/** True iff one axis-parallel segment is horizontal, the other vertical, and they cross. */
export function segmentsCross(a: Segment, b: Segment): boolean {
  const aHoriz = a.y1 === a.y2;
  const bHoriz = b.y1 === b.y2;
  if (aHoriz === bHoriz) return false;
  const h = aHoriz ? a : b;
  const v = aHoriz ? b : a;
  const hMinX = Math.min(h.x1, h.x2);
  const hMaxX = Math.max(h.x1, h.x2);
  const vMinY = Math.min(v.y1, v.y2);
  const vMaxY = Math.max(v.y1, v.y2);
  return (
    v.x1 > hMinX &&
    v.x1 < hMaxX &&
    h.y1 > vMinY &&
    h.y1 < vMaxY
  );
}

/** Total count of orthogonal crossings across all pairs. O(n^2) reference impl. */
export function countCrossings(segments: Segment[]): number {
  let n = 0;
  for (let i = 0; i < segments.length; i++) {
    for (let j = i + 1; j < segments.length; j++) {
      if (segmentsCross(segments[i], segments[j])) n++;
    }
  }
  return n;
}

// ---------- serialize -------------------------------------------------------

function serialize(segments: Segment[]): {
  d: string;
  bends: number;
  lengthPx: number;
} {
  if (segments.length === 0) {
    return { d: '', bends: 0, lengthPx: 0 };
  }
  let d = `M ${segments[0].x1},${segments[0].y1}`;
  d += ` L ${segments[0].x2},${segments[0].y2}`;
  let lengthPx = Math.abs(segments[0].x2 - segments[0].x1) +
    Math.abs(segments[0].y2 - segments[0].y1);
  let bends = 0;
  for (let i = 1; i < segments.length; i++) {
    const s = segments[i];
    d += ` L ${s.x2},${s.y2}`;
    lengthPx += Math.abs(s.x2 - s.x1) + Math.abs(s.y2 - s.y1);
    bends++;
  }
  return { d, bends, lengthPx };
}

// ---------- exports for tests ----------------------------------------------

export const __test = {
  Grid,
  buildGrid,
  computeBounds,
  runAStar,
  emitSegments,
  reduceCrossings,
  countCrossings,
  segmentsCross,
};
