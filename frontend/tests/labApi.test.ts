// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createLayoutDebouncer, type LayoutPayload } from '$lib/services/labApi';

interface CallRecord {
  path: string;
  body: LayoutPayload;
  method: string;
}

describe('labApi.createLayoutDebouncer', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('coalesces two pushes within the debounce window into a single PUT', async () => {
    const calls: CallRecord[] = [];
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 500,
      request: async (path, init) => {
        calls.push({ path, body: init.body, method: init.method });
      },
    });

    debouncer.pushNodePosition(1, 100, 200);
    vi.advanceTimersByTime(100);
    debouncer.pushNodePosition(1, 110, 220);

    expect(calls).toHaveLength(0);
    vi.advanceTimersByTime(499);
    expect(calls).toHaveLength(0);
    vi.advanceTimersByTime(2);

    // Drain the microtask scheduled inside the timer.
    await vi.runOnlyPendingTimersAsync();

    expect(calls).toHaveLength(1);
    expect(calls[0].path).toBe('/labs/demo.json/layout');
    expect(calls[0].method).toBe('PUT');
    expect(calls[0].body.nodes).toEqual([{ id: 1, left: 110, top: 220 }]);
  });

  it('flush() forces an immediate PUT and clears pending state', async () => {
    const calls: CallRecord[] = [];
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 500,
      request: async (path, init) => {
        calls.push({ path, body: init.body, method: init.method });
      },
    });

    debouncer.pushNetworkPosition(2, 50, 60);
    expect(calls).toHaveLength(0);

    await debouncer.flush();
    expect(calls).toHaveLength(1);
    expect(calls[0].body.networks).toEqual([{ id: 2, left: 50, top: 60 }]);
    expect(debouncer.pending()).toBe(false);
  });

  it('cancel() drops the pending payload without issuing a PUT', async () => {
    const calls: CallRecord[] = [];
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 500,
      request: async (path, init) => {
        calls.push({ path, body: init.body, method: init.method });
      },
    });

    debouncer.pushNodePosition(7, 1, 2);
    debouncer.cancel();

    vi.advanceTimersByTime(2000);
    await vi.runOnlyPendingTimersAsync();

    expect(calls).toHaveLength(0);
    expect(debouncer.pending()).toBe(false);
  });

  it('PUT body contains only allow-listed layout fields', async () => {
    const calls: CallRecord[] = [];
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 200,
      request: async (path, init) => {
        calls.push({ path, body: init.body, method: init.method });
      },
    });

    debouncer.pushNodePosition(1, 10, 20);
    debouncer.pushNetworkPosition(3, 30, 40);
    debouncer.pushViewport({ x: 5, y: 6, zoom: 1.5 });
    debouncer.pushDefaultLinkStyle('orthogonal');
    await debouncer.flush();

    expect(calls).toHaveLength(1);
    const body = calls[0].body;
    expect(Object.keys(body).sort()).toEqual(['defaults', 'networks', 'nodes', 'viewport']);
    expect(body.nodes).toEqual([{ id: 1, left: 10, top: 20 }]);
    expect(body.networks).toEqual([{ id: 3, left: 30, top: 40 }]);
    expect(body.viewport).toEqual({ x: 5, y: 6, zoom: 1.5 });
    expect(body.defaults).toEqual({ link_style: 'orthogonal' });
  });

  it('extends the debounce timer when new pushes arrive', async () => {
    const calls: CallRecord[] = [];
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 500,
      request: async (_path, init) => {
        calls.push({ path: _path, body: init.body, method: init.method });
      },
    });

    debouncer.pushNodePosition(1, 10, 10);
    vi.advanceTimersByTime(400);
    debouncer.pushNodePosition(2, 20, 20);
    vi.advanceTimersByTime(400);
    expect(calls).toHaveLength(0); // timer was reset by the second push
    vi.advanceTimersByTime(200);
    await vi.runOnlyPendingTimersAsync();
    expect(calls).toHaveLength(1);
    expect(calls[0].body.nodes).toEqual([
      { id: 1, left: 10, top: 10 },
      { id: 2, left: 20, top: 20 },
    ]);
  });

  it('forwards request errors through onError without throwing', async () => {
    const errors: unknown[] = [];
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 100,
      request: async () => {
        throw new Error('boom');
      },
      onError: (err) => errors.push(err),
    });

    debouncer.pushNodePosition(1, 1, 1);
    await debouncer.flush();
    expect(errors).toHaveLength(1);
    expect((errors[0] as Error).message).toBe('boom');
  });
});
