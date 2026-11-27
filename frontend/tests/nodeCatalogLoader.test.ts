import { afterEach, describe, expect, it, vi } from 'vitest';

import { createNodeCatalogLoader } from '$lib/services/nodeCatalogLoader';
import type { NodeCatalog } from '$lib/types';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function catalog(version: string): NodeCatalog {
  return { version } as unknown as NodeCatalog;
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

afterEach(() => {
  vi.useRealTimers();
});

describe('createNodeCatalogLoader', () => {
  it('recovers a completion missed while hidden and never applies the stale HTTP callback', async () => {
    const first = deferred<NodeCatalog>();
    const second = deferred<NodeCatalog>();
    const load = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const onCatalog = vi.fn();
    const loader = createNodeCatalogLoader({ load, onCatalog });

    loader.refresh();
    loader.refresh();
    loader.refresh();
    expect(load).toHaveBeenCalledTimes(1);

    first.resolve(catalog('stale'));
    await flushPromises();
    expect(onCatalog).not.toHaveBeenCalled();
    expect(load).toHaveBeenCalledTimes(2);

    second.resolve(catalog('fresh'));
    await flushPromises();
    expect(onCatalog).toHaveBeenCalledTimes(1);
    expect(onCatalog).toHaveBeenCalledWith(catalog('fresh'));
    loader.close();
  });

  it('retries an initial failure with one bounded timer', async () => {
    vi.useFakeTimers();
    const first = deferred<NodeCatalog>();
    const second = deferred<NodeCatalog>();
    const load = vi.fn()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const onError = vi.fn();
    const onCatalog = vi.fn();
    const loader = createNodeCatalogLoader({
      load,
      onCatalog,
      onError,
      retryDelayMs: 2000,
    });

    loader.refresh();
    first.reject(new Error('unavailable'));
    await flushPromises();
    expect(onError).toHaveBeenCalledTimes(1);
    expect(load).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1999);
    expect(load).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(load).toHaveBeenCalledTimes(2);
    second.resolve(catalog('recovered'));
    await flushPromises();
    expect(onCatalog).toHaveBeenCalledWith(catalog('recovered'));

    await vi.advanceTimersByTimeAsync(60_000);
    expect(load).toHaveBeenCalledTimes(2);
    loader.close();
  });

  it('clears a pending retry when a socket refresh arrives', async () => {
    vi.useFakeTimers();
    const load = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(catalog('socket-fresh'));
    const onCatalog = vi.fn();
    const loader = createNodeCatalogLoader({ load, onCatalog, retryDelayMs: 2000 });

    loader.refresh();
    await flushPromises();
    loader.refresh();
    await flushPromises();

    expect(load).toHaveBeenCalledTimes(2);
    expect(onCatalog).toHaveBeenCalledWith(catalog('socket-fresh'));
    await vi.advanceTimersByTimeAsync(10_000);
    expect(load).toHaveBeenCalledTimes(2);
    loader.close();
  });

  it('polls a stale successful response until it becomes fresh', async () => {
    vi.useFakeTimers();
    const load = vi.fn()
      .mockResolvedValueOnce({ ...catalog('stale'), pending: true })
      .mockResolvedValueOnce({ ...catalog('fresh'), pending: false });
    const onCatalog = vi.fn();
    const loader = createNodeCatalogLoader({ load, onCatalog, retryDelayMs: 2000 });

    loader.refresh();
    await flushPromises();
    expect(onCatalog).toHaveBeenCalledWith({ ...catalog('stale'), pending: true });
    expect(load).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1999);
    expect(load).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await flushPromises();
    expect(load).toHaveBeenCalledTimes(2);
    expect(onCatalog).toHaveBeenLastCalledWith({ ...catalog('fresh'), pending: false });

    await vi.advanceTimersByTimeAsync(60_000);
    expect(load).toHaveBeenCalledTimes(2);
    loader.close();
  });

  it('a socket refresh cancels the delayed stale-response retry', async () => {
    vi.useFakeTimers();
    const load = vi.fn()
      .mockResolvedValueOnce({ ...catalog('stale'), pending: true })
      .mockResolvedValueOnce({ ...catalog('event-fresh'), pending: false });
    const loader = createNodeCatalogLoader({ load, onCatalog: vi.fn(), retryDelayMs: 2000 });

    loader.refresh();
    await flushPromises();
    loader.refresh();
    await flushPromises();

    expect(load).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(load).toHaveBeenCalledTimes(2);
    loader.close();
  });

  it('ignores a late completion after the editor is destroyed', async () => {
    const pending = deferred<NodeCatalog>();
    const onCatalog = vi.fn();
    const onError = vi.fn();
    const loader = createNodeCatalogLoader({
      load: () => pending.promise,
      onCatalog,
      onError,
    });

    loader.refresh();
    loader.close();
    pending.resolve(catalog('late'));
    await flushPromises();

    expect(onCatalog).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });
});
