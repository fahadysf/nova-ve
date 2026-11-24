// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import LogsPage from '../../src/routes/(shell)/system/logs/+page.svelte';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => ({
  apiRequest: (...args: unknown[]) => apiRequest(...args)
}));

describe('Logs page: runtime logs and activity log (#265, #297)', () => {
  beforeEach(() => {
    apiRequest.mockReset();
    apiRequest.mockImplementation(async (path: string) => {
      if (path === '/system/logs') {
        return { code: 200, status: 'success', message: '', data: { logs: [] } };
      }
      return {
        code: 200,
        status: 'success',
        message: '',
        data: { entries: [], total: 0, limit: 100, offset: 0, actors: [] }
      };
    });
  });

  it('opens on the runtime log viewer', async () => {
    render(LogsPage);

    await waitFor(() => expect(screen.getByTestId('logs-file-select')).toBeInTheDocument());
    expect(screen.queryByTestId('activity-search')).not.toBeInTheDocument();
    expect(screen.getByTestId('logs-view-runtime')).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByTestId('logs-view-activity')).toHaveAttribute('aria-selected', 'false');
    // The viewer's refresh button now lives in its own toolbar.
    expect(screen.getByTestId('logs-refresh')).toBeInTheDocument();
    expect(apiRequest).toHaveBeenCalledTimes(1);
  });

  it('switches to the activity log, and back', async () => {
    render(LogsPage);
    await waitFor(() => expect(screen.getByTestId('logs-file-select')).toBeInTheDocument());

    await fireEvent.click(screen.getByTestId('logs-view-activity'));

    await waitFor(() => expect(screen.getByTestId('activity-search')).toBeInTheDocument());
    expect(screen.queryByTestId('logs-file-select')).not.toBeInTheDocument();
    expect(screen.getByTestId('logs-view-activity')).toHaveAttribute('aria-selected', 'true');
    expect(apiRequest.mock.calls.some(([path]) => String(path).startsWith('/system/logs/activity?'))).toBe(true);

    await fireEvent.click(screen.getByTestId('logs-view-runtime'));

    await waitFor(() => expect(screen.getByTestId('logs-file-select')).toBeInTheDocument());
    expect(screen.queryByTestId('activity-search')).not.toBeInTheDocument();
  });

  it('points each view at the issue that tracks it', async () => {
    render(LogsPage);
    await waitFor(() => expect(screen.getByRole('link', { name: 'tracked in #265' })).toBeInTheDocument());

    await fireEvent.click(screen.getByTestId('logs-view-activity'));

    await waitFor(() => expect(screen.getByRole('link', { name: 'tracked in #297' })).toBeInTheDocument());
  });
});
