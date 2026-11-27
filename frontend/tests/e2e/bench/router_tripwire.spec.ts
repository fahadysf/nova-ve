// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, expect, it, vi } from 'vitest';
import { expectRouterTripwire } from './routerTripwire';

describe('router tripwire promotion', () => {
  it('only publishes a signal for a finite measurement above its limit', () => {
    const directory = mkdtempSync(join(tmpdir(), 'router-tripwire-'));
    const output = join(directory, 'output');
    vi.stubEnv('ROUTER_TRIPWIRE_OUTPUT', output);
    try {
      expectRouterTripwire(150, 150);
      expect(() => expectRouterTripwire(Number.NaN, 150)).toThrow();
      expect(() => expectRouterTripwire(Number.POSITIVE_INFINITY, 150)).toThrow();
      expect(existsSync(output)).toBe(false);
      expect(() => expectRouterTripwire(150.01, 150)).toThrow();
      expect(readFileSync(output, 'utf8')).toBe('exceeded=true\n');
    } finally {
      vi.unstubAllEnvs();
      rmSync(directory, { recursive: true });
    }
  });
});
