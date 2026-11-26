import { expect, it, vi } from 'vitest';
import { createLabRefreshQueue } from '$lib/services/labRefreshQueue';

it('runs one trailing refresh for notifications received during a request', async () => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const refresh = vi.fn().mockResolvedValue(undefined).mockImplementationOnce(() => held);
  const queue = createLabRefreshQueue(refresh);

  const first = queue.request({ blocking: true, reconcileRuntime: true });
  await Promise.resolve();
  queue.request({ blocking: false, reconcileRuntime: false });
  queue.request({ blocking: false, reconcileRuntime: false });
  release();
  await first;

  expect(refresh.mock.calls).toEqual([
    [{ blocking: true, reconcileRuntime: true }],
    [{ blocking: false, reconcileRuntime: false }],
  ]);
});

it('drops queued work for a lab that is no longer open', async () => {
  let release!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const refresh = vi.fn(() => held);
  const queue = createLabRefreshQueue(refresh);

  const first = queue.request({ blocking: true });
  await Promise.resolve();
  queue.request({ blocking: false });
  queue.close();
  release();
  await first;
  await queue.request({ blocking: false });

  expect(refresh).toHaveBeenCalledTimes(1);
});
