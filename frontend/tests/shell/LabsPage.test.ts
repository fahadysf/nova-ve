// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import LabsPage from '../../src/routes/(shell)/labs/+page.svelte';
import { authStore } from '$lib/stores/session';

const apiGetData = vi.fn();

vi.mock('$lib/api', () => ({
  apiGetData: (...args: unknown[]) => apiGetData(...args),
  apiRequest: vi.fn(),
  ApiError: class extends Error {},
  moveLab: vi.fn()
}));

const folders = [
  { name: 'Running', path: '/Running', owner: 'system' },
  { name: 'Shared', path: '/Shared', owner: '*' },
  { name: 'idle-folder', path: '/idle-folder', owner: 'admin' }
];

const lab = (path: string, nodes_running: number) => ({
  file: path.split('/').pop()!,
  path,
  mtime: 'now',
  owner: 'admin',
  nodes_running
});

function serve(labs: ReturnType<typeof lab>[]): void {
  apiGetData.mockReset();
  apiGetData.mockImplementation(async () => ({ folders, labs }));
}

async function mount() {
  render(LabsPage);
  await waitFor(() => screen.getByRole('listbox'));
}

/** A row of the folder-contents list, found by the name it shows. */
function contentRow(name: string): HTMLElement {
  const row = within(screen.getByRole('listbox'))
    .getAllByRole('option')
    .find((option) => option.textContent?.includes(name));
  if (!row) throw new Error(`no content row for ${name}`);
  return row;
}

describe('Labs browser running indicators (#365)', () => {
  beforeEach(() => {
    authStore.setUser({ username: 'admin', role: 'admin', email: '', name: '' } as never);
  });

  it('puts a green count on labs with live nodes, and only those', async () => {
    serve([lab('/busy.json', 5), lab('/quiet.json', 0)]);
    await mount();

    expect(within(contentRow('busy')).getByTestId('lab-running-count')).toHaveTextContent('5 running');
    expect(within(contentRow('quiet')).queryByTestId('lab-running-count')).toBeNull();
  });

  it('marks the folders holding running labs, in the tree and in the list', async () => {
    serve([lab('/busy.json', 5), lab('/Shared/team.json', 2), lab('/idle-folder/quiet.json', 0)]);
    await mount();

    for (const pane of [screen.getByRole('tree'), screen.getByRole('listbox')]) {
      expect(within(pane).getAllByTestId('folder-running-dot').map((dot) => dot.getAttribute('aria-label'))).toEqual([
        '7 nodes running inside', // Running: every live node
        '2 nodes running inside' // Shared
      ]);
    }
    expect(within(contentRow('idle-folder')).queryByTestId('folder-running-dot')).toBeNull();
  });

  async function openRunning(): Promise<HTMLElement> {
    await fireEvent.click(within(screen.getByRole('tree')).getByRole('button', { name: /^Running/ }));
    return screen.getByRole('listbox', { name: 'Contents of Running' });
  }

  it('lists the running labs under Running', async () => {
    serve([lab('/busy.json', 5), lab('/Shared/team.json', 2), lab('/idle-folder/quiet.json', 0)]);
    await mount();

    const running = await openRunning();

    expect([...within(running).getAllByRole('option')].map((row) => row.textContent)).toEqual([
      expect.stringContaining('busy'),
      expect.stringContaining('team')
    ]);
    expect(within(running).getAllByTestId('lab-running-count')).toHaveLength(2);
  });

  it('empties Running once every node is down', async () => {
    serve([lab('/busy.json', 0)]);
    await mount();

    const running = await openRunning();

    expect(within(running).queryAllByRole('option')).toHaveLength(0);
    expect(within(running).getByText('No labs are running.')).toBeInTheDocument();
  });
});
