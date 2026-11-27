// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { appendFileSync } from 'node:fs';
import { expect } from 'vitest';

export function expectRouterTripwire(value: number, limit: number): void {
  expect(Number.isFinite(value)).toBe(true);
  const output = process.env.ROUTER_TRIPWIRE_OUTPUT;
  if (value > limit && output) {
    appendFileSync(output, 'exceeded=true\n');
  }
  expect(value).toBeLessThanOrEqual(limit);
}
