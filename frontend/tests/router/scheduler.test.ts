// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * Scheduler tests. Verifies AbortController cancels in-flight compute and
 * that a follow-up request supersedes the previous one for the same linkId.
 */

import { describe, it, expect } from 'vitest';
import {
  RouterScheduler,
  type RouteRequest,
  type RouteResult,
  type RouterService,
} from '$lib/services/router';

class StubRouter implements RouterService {
  delayMs: number;
  constructor(delayMs = 50) {
    this.delayMs = delayMs;
  }
  route(_req: RouteRequest, opts?: { signal?: AbortSignal }): Promise<RouteResult> {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => {
        resolve({ d: 'M 0,0 L 10,0', bends: 0, lengthPx: 10 });
      }, this.delayMs);
      opts?.signal?.addEventListener('abort', () => {
        clearTimeout(t);
        reject(new DOMException('aborted', 'AbortError'));
      });
    });
  }
}

const REQ: RouteRequest = {
  from: { x: 0, y: 0, side: 'right' },
  to: { x: 100, y: 0, side: 'left' },
  obstacles: [],
  style: 'orthogonal',
};

describe('RouterScheduler', () => {
  it('resolves a single request', async () => {
    const sched = new RouterScheduler(new StubRouter(5));
    const r = await sched.route('link-1', REQ);
    expect(r.d).toBe('M 0,0 L 10,0');
    expect(sched.size()).toBe(0);
  });

  it('aborts previous in-flight when a second request lands for the same id', async () => {
    const sched = new RouterScheduler(new StubRouter(50));
    const first = sched.route('link-X', REQ);
    const second = sched.route('link-X', REQ);
    await expect(first).rejects.toMatchObject({ name: 'AbortError' });
    const r = await second;
    expect(r.d).toBe('M 0,0 L 10,0');
    expect(sched.size()).toBe(0);
  });

  it('cancel() aborts pending compute', async () => {
    const sched = new RouterScheduler(new StubRouter(100));
    const promise = sched.route('link-Y', REQ);
    sched.cancel('link-Y');
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' });
    expect(sched.size()).toBe(0);
  });

  it('cancelAll() aborts every in-flight request', async () => {
    const sched = new RouterScheduler(new StubRouter(100));
    const a = sched.route('a', REQ);
    const b = sched.route('b', REQ);
    sched.cancelAll();
    await expect(a).rejects.toMatchObject({ name: 'AbortError' });
    await expect(b).rejects.toMatchObject({ name: 'AbortError' });
    expect(sched.size()).toBe(0);
  });

  it('different linkIds are independent', async () => {
    const sched = new RouterScheduler(new StubRouter(20));
    const a = sched.route('a', REQ);
    const b = sched.route('b', REQ);
    const [ra, rb] = await Promise.all([a, b]);
    expect(ra.d).toBe('M 0,0 L 10,0');
    expect(rb.d).toBe('M 0,0 L 10,0');
  });
});
