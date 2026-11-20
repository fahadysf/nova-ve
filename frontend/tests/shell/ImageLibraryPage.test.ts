// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import LibraryPage from '../../src/routes/(shell)/library/images/+page.svelte';
import type { LibraryImage, LibraryTemplate } from '../../src/lib/types';

const apiRequest = vi.fn();

vi.mock('$lib/api', () => ({
  apiRequest: (...args: unknown[]) => apiRequest(...args),
}));

const TEMPLATES: LibraryTemplate[] = [
  {
    type: 'qemu',
    key: 'veos',
    name: 'vEOS-QEMU',
    description: 'Arista vEOS',
    icon_type: 'router',
    cpu: 1,
    ram: 2048,
    ethernet: 4,
    console_type: 'telnet',
    cpulimit: 1,
    image_aliases: ['veos'],
    images: [
      { name: 'veos', filename: 'veos-4.31.3F.qcow2', state: 'available', size: 1298568192 },
    ],
  },
  {
    type: 'qemu',
    key: 'vqfx',
    name: 'vQFX',
    description: '',
    icon_type: 'router',
    cpu: 1,
    ram: 1024,
    ethernet: 2,
    console_type: 'telnet',
    cpulimit: 1,
    image_aliases: ['vqfx'],
    images: [],
  },
];

const IMAGES: LibraryImage[] = [
  {
    name: 'veos',
    filename: 'veos-4.31.3F.qcow2',
    type: 'qemu',
    path: '/var/lib/nova-ve/images/qemu/veos',
    files: ['veos-4.31.3F.qcow2'],
    size: 1298568192,
    mtime: 1735000000,
    mtime_iso: '2024-12-24 03:46',
    sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    state: 'available',
    referenced_by: [
      { lab: 'arc-lab.json', lab_path: '/arc-lab.json', lab_name: 'Arc Lab', node: 'sw1', template: 'veos' },
      { lab: 'ST-Demo.json', lab_path: '/ST-Demo.json', lab_name: 'ST Demo', node: 'leaf1', template: 'veos' },
    ],
    templates: [{ type: 'qemu', key: 'veos', name: 'vEOS-QEMU' }],
  },
  {
    name: 'vyos',
    filename: 'vyos-1.5-rolling.qcow2',
    type: 'qemu',
    path: '/var/lib/nova-ve/images/qemu/vyos',
    files: ['vyos-1.5-rolling.qcow2'],
    size: 1288490188,
    mtime: 1735000000,
    mtime_iso: '2024-12-24 03:46',
    sha256: null,
    state: 'available',
    referenced_by: [],
    templates: [],
  },
];

function mockApi() {
  apiRequest.mockImplementation(async (path: string) => {
    if (path === '/library/templates') {
      return { code: 200, status: 'success', message: '', data: { templates: TEMPLATES } };
    }
    if (path === '/library/images') {
      return { code: 200, status: 'success', message: '', data: { images: IMAGES } };
    }
    if (path === '/library/images/qemu/vyos/verify') {
      return {
        code: 200,
        status: 'success',
        message: '',
        data: { sha256: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', size: 1288490188 },
      };
    }
    return { code: 200, status: 'success', message: '', data: {} };
  });
}

describe('Images & Templates library page (#264, live)', () => {
  beforeEach(() => {
    apiRequest.mockReset();
    mockApi();
  });

  it('renders template cards grouped by type with their matching on-box images', async () => {
    render(LibraryPage);

    await waitFor(() => {
      expect(screen.getByTestId('template-card-veos')).toBeInTheDocument();
    });

    expect(screen.getByText('vEOS-QEMU')).toBeInTheDocument();
    expect(screen.getByText('veos-4.31.3F.qcow2')).toBeInTheDocument();
    // Missing-image template state is explicit.
    expect(screen.getByText(/No image matches/i)).toBeInTheDocument();
    expect(screen.getByText('1.2 GB')).toBeInTheDocument();
  });

  it('switches to the images tab and lists size, checksum, and lab references', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));

    await fireEvent.click(screen.getByTestId('images-tab-images'));

    expect(screen.getByTestId('images-panel')).toBeInTheDocument();
    expect(screen.getByText('vyos-1.5-rolling.qcow2')).toBeInTheDocument();
    // Reference chips render lab file names, not raw paths.
    expect(screen.getByText('arc-lab.json')).toBeInTheDocument();
    expect(screen.getAllByText('ST-Demo.json').length).toBeGreaterThanOrEqual(1);
    // Referenced images cannot be deleted — blocked marker replaces the action.
    expect(screen.getByTestId('image-delete-blocked-veos')).toBeInTheDocument();
    // Unreferenced images expose the delete action.
    expect(screen.getByTestId('image-delete-vyos')).toBeInTheDocument();
  });

  it('verify computes the checksum through the API and shows the cached value', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));
    await fireEvent.click(screen.getByTestId('images-tab-images'));

    await fireEvent.click(screen.getByTestId('image-verify-vyos'));

    await waitFor(() => {
      const verifyCall = apiRequest.mock.calls.find(
        ([path, init]) =>
          path === '/library/images/qemu/vyos/verify' &&
          (init as { method?: string })?.method === 'POST'
      );
      expect(verifyCall).toBeTruthy();
    });
    expect(screen.getByText('bbbbbbbbbbbb…bbbbbb')).toBeInTheDocument();
  });

  it('filters images by search query', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));
    await fireEvent.click(screen.getByTestId('images-tab-images'));

    await fireEvent.input(screen.getByTestId('images-search'), {
      target: { value: 'vyos' },
    });
    expect(screen.getByText('vyos-1.5-rolling.qcow2')).toBeInTheDocument();
    expect(screen.queryByText('veos-4.31.3F.qcow2')).toBeNull();
  });

  it('edits a template through the dialog and PATCHes the backend', async () => {
    render(LibraryPage);
    await waitFor(() => screen.getByTestId('template-card-veos'));

    await fireEvent.click(screen.getByTestId('template-edit-veos'));
    expect(screen.getByTestId('template-dialog')).toBeInTheDocument();

    await fireEvent.input(screen.getByTestId('template-dialog-ram'), {
      target: { value: '4096' },
    });
    await fireEvent.click(screen.getByTestId('template-dialog-save'));

    await waitFor(() => {
      const patchCall = apiRequest.mock.calls.find(
        ([path, init]) =>
          path === '/library/templates/qemu/veos' &&
          (init as { method?: string })?.method === 'PATCH'
      );
      expect(patchCall).toBeTruthy();
      const body = (patchCall![1] as { body: Record<string, unknown> }).body;
      expect(body.ram).toBe(4096);
    });
  });
});
