// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import NodesPage from '../../src/routes/(shell)/manage/nodes/+page.svelte';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => ({
  apiRequest: (...args: unknown[]) => apiRequest(...args),
}));

const RUNNING_NODES = {
  nodes: [
    {
      lab_path: '/arc-lab.json',
      lab_name: 'arc-lab',
      node_id: 1,
      name: 'veos-leaf1',
      type: 'qemu',
      template: 'veos',
      image: 'veos',
      status: 2,
      state: 'running',
      console: 'telnet',
      url: 'telnet://host:5001',
      cpu_allocated: 2,
      ram_allocated_mb: 2048,
      cpu_usage: 21.4,
      ram_usage: 512,
      disk_usage: 1073741824,
    },
    {
      lab_path: '/st-demo/ST-Demo.json',
      lab_name: 'ST-Demo',
      node_id: 2,
      name: 'iol-rtr',
      type: 'iol',
      template: 'iol',
      image: 'iol-image',
      status: 2,
      state: 'running',
      console: 'telnet',
      url: null,
      cpu_allocated: 1,
      ram_allocated_mb: 512,
      cpu_usage: 11,
      ram_usage: 128,
      disk_usage: 0,
    },
  ],
  totals: { nodes: 2, running: 2, cpu_allocated: 3, ram_allocated_mb: 2560 },
};

describe('Running nodes page (#263, live)', () => {
  beforeEach(() => {
    apiRequest.mockReset();
    apiRequest.mockImplementation(async (path: string) => {
      if (String(path).startsWith('/manage/nodes')) {
        return { code: 200, status: 'success', message: '', data: RUNNING_NODES };
      }
      return { code: 200, status: 'success', message: '', data: {} };
    });
  });

  it('lists running nodes with lab, usage, and totals', async () => {
    render(NodesPage);

    await waitFor(() => screen.getByTestId('node-row-veos-leaf1'));

    expect(screen.getByText('iol-rtr')).toBeInTheDocument();
    expect(screen.getByText('arc-lab')).toBeInTheDocument();
    expect(screen.getByText('2.5 GB')).toBeInTheDocument(); // RAM allocated KPI
    expect(screen.getByText('21%')).toBeInTheDocument(); // CPU usage cell
  });

  it('stop action calls the labs stop endpoint after confirm', async () => {
    render(NodesPage);

    await waitFor(() => screen.getByTestId('node-stop-veos-leaf1'));
    await fireEvent.click(screen.getByTestId('node-stop-veos-leaf1'));
    expect(screen.getByTestId('node-stop-confirm')).toBeInTheDocument();

    await fireEvent.click(screen.getByTestId('node-stop-confirm-button'));

    await waitFor(() => {
      const stopCall = apiRequest.mock.calls.find(
        ([path]) => String(path) === '/labs/arc-lab.json/nodes/1/stop'
      );
      expect(stopCall).toBeTruthy();
    });
  });

  it('sends filters as query params on refresh', async () => {
    render(NodesPage);

    await waitFor(() => screen.getByTestId('nodes-state-filter'));
    await fireEvent.change(screen.getByTestId('nodes-state-filter'), {
      target: { value: 'all' },
    });

    await waitFor(() => {
      const call = apiRequest.mock.calls.find(([path]) => String(path).includes('state=all'));
      expect(call).toBeTruthy();
    });
  });
});
