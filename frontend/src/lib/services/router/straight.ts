// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { RouteRequest, RouteResult } from './index';

/** Straight-line router. Single segment, zero bends. */
export function routeStraight(req: RouteRequest): RouteResult {
  const { from, to } = req;
  const d = `M ${from.x},${from.y} L ${to.x},${to.y}`;
  const lengthPx = Math.hypot(to.x - from.x, to.y - from.y);
  return { d, bends: 0, lengthPx };
}
