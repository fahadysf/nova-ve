// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createLayoutDebouncer,
  defaultRequester,
  LayoutConflictError,
  type LayoutPayload,
} from '$lib/services/labApi';

interface CallRecord {
  path: string;
  body: LayoutPayload;
  method: string;
  ifMatch?: string;
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

  it('requeues a failed PUT so the next flush retries it (newer pushes win)', async () => {
    const calls: CallRecord[] = [];
    let failFirst = true;
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 100,
      request: async (path, init) => {
        if (failFirst) {
          failFirst = false;
          throw new Error('transient');
        }
        calls.push({ path, body: init.body, method: init.method });
      },
    });

    debouncer.pushNodePosition(1, 10, 20);
    debouncer.pushNetworkPosition(2, 30, 40);
    await debouncer.flush(); // fails
    expect(debouncer.pending()).toBe(true);

    // No timer was armed by the requeue — nothing fires on its own.
    vi.advanceTimersByTime(5000);
    await vi.runOnlyPendingTimersAsync();
    expect(calls).toHaveLength(0);

    // A newer push for the same node must win over the requeued entry.
    debouncer.pushNodePosition(1, 99, 99);
    await debouncer.flush();

    expect(calls).toHaveLength(1);
    expect(calls[0].body.nodes).toEqual([{ id: 1, left: 99, top: 99 }]);
    expect(calls[0].body.networks).toEqual([{ id: 2, left: 30, top: 40 }]);
    expect(debouncer.pending()).toBe(false);
  });

  it('pendingPositionIds reports queued ids and stays populated while a PUT is in flight', async () => {
    let release: (() => void) | null = null;
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 100,
      request: () =>
        new Promise((resolve) => {
          release = () => resolve(null);
        }),
    });

    debouncer.pushNodePosition(1, 1, 1);
    expect(debouncer.pendingPositionIds().nodeIds.has(1)).toBe(true);

    vi.advanceTimersByTime(100);
    // send() has started; the request is in flight and still pending.
    expect(debouncer.pendingPositionIds().nodeIds.has(1)).toBe(true);

    release?.();
    await debouncer.flush();
    expect(debouncer.pendingPositionIds().nodeIds.has(1)).toBe(false);
  });

  it('serializes layout writes and keeps both positions protected as they drain', async () => {
    let releaseFirst!: () => void;
    let releaseSecond!: () => void;
    const first = new Promise<void>((resolve) => { releaseFirst = resolve; });
    const second = new Promise<void>((resolve) => { releaseSecond = resolve; });
    const request = vi.fn().mockReturnValueOnce(first).mockReturnValueOnce(second);
    const debouncer = createLayoutDebouncer('demo.json', { request });

    debouncer.pushNodePosition(1, 10, 10);
    const firstFlush = debouncer.flush();
    debouncer.pushNetworkPosition(2, 20, 20);
    const secondFlush = debouncer.flush();
    expect(request).toHaveBeenCalledTimes(1);
    expect(debouncer.pendingPositionIds()).toEqual({ nodeIds: new Set([1]), networkIds: new Set([2]) });

    releaseFirst();
    await firstFlush;
    expect(request).toHaveBeenCalledTimes(2);
    expect(debouncer.pendingPositionIds()).toEqual({ nodeIds: new Set(), networkIds: new Set([2]) });
    releaseSecond();
    await secondFlush;
    expect(debouncer.pending()).toBe(false);
  });

  it('flushKeepalive fires the pending payload as a keepalive fetch', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', fetchMock);
    try {
      const debouncer = createLayoutDebouncer('demo.json', { delayMs: 500 });

      debouncer.pushNodePosition(4, 11, 22);
      debouncer.flushKeepalive();

      expect(fetchMock).toHaveBeenCalledTimes(1);
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe('/api/labs/demo.json/layout');
      expect(init.method).toBe('PUT');
      expect(init.keepalive).toBe(true);
      expect(init.headers['If-Match']).toBeUndefined();
      expect(JSON.parse(init.body)).toEqual({
        nodes: [{ id: 4, left: 11, top: 22 }],
      });
      // Payload was consumed — a second call is a no-op.
      debouncer.flushKeepalive();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('sends the versionProvider value as If-Match and reports the applied revision', async () => {
    const calls: CallRecord[] = [];
    const applied: unknown[] = [];
    let revision: number | null = 7;
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 100,
      versionProvider: () => revision,
      onApplied: (result) => applied.push(result),
      request: async (path, init) => {
        calls.push({ path, body: init.body, method: init.method, ifMatch: init.ifMatch });
        return { revision: 8 };
      },
    });

    debouncer.pushNodePosition(1, 10, 20);
    await debouncer.flush();

    expect(calls).toHaveLength(1);
    expect(calls[0].ifMatch).toBe('7');
    expect(applied).toEqual([{ revision: 8 }]);

    // A null revision must NOT produce an If-Match header.
    revision = null;
    debouncer.pushNodePosition(1, 11, 21);
    await debouncer.flush();
    expect(calls[1].ifMatch).toBeUndefined();
  });

  it('flushKeepalive sends If-Match when a revision is available', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal('fetch', fetchMock);
    try {
      const debouncer = createLayoutDebouncer('demo.json', {
        delayMs: 500,
        versionProvider: () => 12,
      });

      debouncer.pushNodePosition(5, 1, 2);
      debouncer.flushKeepalive();

      const [, init] = fetchMock.mock.calls[0];
      expect(init.headers['If-Match']).toBe('12');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('requeues and reports a 412 conflict through the default requester', async () => {
    const errors: unknown[] = [];
    const debouncer = createLayoutDebouncer('demo.json', {
      delayMs: 100,
      versionProvider: () => 3,
      onError: (err) => errors.push(err),
    });

    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 412,
      json: async () => ({ code: 412, revision: 9 }),
    });
    vi.stubGlobal('fetch', fetchMock);
    try {
      debouncer.pushNodePosition(2, 5, 6);
      await debouncer.flush();

      expect(errors).toHaveLength(1);
      expect(errors[0]).toBeInstanceOf(LayoutConflictError);
      expect((errors[0] as LayoutConflictError).serverRevision).toBe(9);
      // Payload survives for the post-snapshot retry.
      expect(debouncer.pending()).toBe(true);
      expect(debouncer.pendingPositionIds().nodeIds.has(2)).toBe(true);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('defaultRequester throws plain Error for non-412 failures', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response('{"message":"nope"}', { status: 404 })),
    );
    try {
      const error: unknown = await defaultRequester('/labs/demo.json/layout', {
        method: 'PUT',
        body: { nodes: [{ id: 1, left: 0, top: 0 }] },
      }).catch((e: unknown) => e);
      expect(error).toBeInstanceOf(Error);
      expect(error).not.toBeInstanceOf(LayoutConflictError);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
