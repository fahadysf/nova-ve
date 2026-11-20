// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import LabsPage from '../../src/routes/(shell)/manage/labs/+page.svelte';
import { authStore } from '$lib/stores/session';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => ({
  apiRequest: (...args: unknown[]) => apiRequest(...args),
}));

const DATA = {
  labs: [
    {
      path: '/arc-lab.json',
      file: 'arc-lab.json',
      name: 'arc-lab',
      author: 'admin',
      description: '',
      state: 'running',
      nodes_total: 3,
      nodes_running: 2,
      cpu_allocated: 6,
      ram_allocated_mb: 6144,
      lock: false,
    },
    {
      path: '/idle-lab.json',
      file: 'idle-lab.json',
      name: 'idle-lab',
      author: '',
      description: '',
      state: 'stopped',
      nodes_total: 1,
      nodes_running: 0,
      cpu_allocated: 1,
      ram_allocated_mb: 512,
      lock: false,
    },
  ],
  totals: {
    labs: 2,
    running: 1,
    nodes_total: 4,
    nodes_running: 2,
    cpu_allocated: 7,
    ram_allocated_mb: 6656,
  },
};

describe('Lab management page (#262, live)', () => {
  beforeEach(() => {
    authStore.clear();
    apiRequest.mockReset();
    apiRequest.mockImplementation(async (path: string) => {
      if (path === '/manage/labs') {
        return { code: 200, status: 'success', message: '', data: DATA };
      }
      return { code: 200, status: 'success', message: '', data: {} };
    });
  });

  it('lists labs with state, allocations, and KPI totals', async () => {
    render(LabsPage);

    await waitFor(() => screen.getByTestId('labs-row-arc-lab.json'));

    expect(screen.getByText('1 / 2')).toBeInTheDocument(); // labs running KPI
    expect(screen.getByText('2 / 4')).toBeInTheDocument(); // nodes running KPI
    expect(screen.getByText('6.5 GB')).toBeInTheDocument(); // RAM KPI (6656 MB)
    expect(screen.getByText('idle-lab')).toBeInTheDocument();
  });

  it('stop action posts to the manage endpoint after confirm', async () => {
    authStore.setUser({ username: 'admin', role: 'admin', email: '', name: '' });
    render(LabsPage);

    await waitFor(() => screen.getByTestId('labs-stop-arc-lab.json'));
    await fireEvent.click(screen.getByTestId('labs-stop-arc-lab.json'));
    expect(screen.getByTestId('lab-action-confirm')).toBeInTheDocument();

    await fireEvent.click(screen.getByTestId('lab-action-confirm-button'));

    await waitFor(() => {
      const stopCall = apiRequest.mock.calls.find(
        ([path, init]) =>
          path === '/manage/labs/arc-lab.json/stop' &&
          (init as { method?: string })?.method === 'POST'
      );
      expect(stopCall).toBeTruthy();
    });
  });

  it('non-admins see view-only rows', async () => {
    authStore.setUser({ username: 'trainee', role: 'user', email: '', name: '' });
    render(LabsPage);

    await waitFor(() => screen.getByTestId('labs-row-arc-lab.json'));

    expect(screen.queryByTestId('labs-stop-arc-lab.json')).toBeNull();
    expect(screen.getAllByText('view only').length).toBe(DATA.labs.length);
  });
});
