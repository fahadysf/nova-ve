// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import NetworkConfigModal from '$lib/components/canvas/NetworkConfigModal.svelte';
import { ApiError, apiGetData, apiRequest } from '$lib/api';
import { natDefaults, validateNatForm } from '$lib/services/natCloudForm';
import type { NetworkData } from '$lib/types';

vi.mock('$lib/api', () => ({
  ApiError: class ApiError extends Error {
    field?: string;
    constructor(message: string, options?: { field?: string }) {
      super(message);
      this.field = options?.field;
    }
  },
  apiRequest: vi.fn(async () => ({ data: {} })),
  apiGetData: vi.fn(async () => [])
}));

const nat = (config: Record<string, unknown> = {}): NetworkData => ({
  id: 3,
  name: 'internet',
  type: 'nat_cloud',
  left: 0,
  top: 0,
  icon: '',
  visibility: true,
  config: {
    cidr: '10.20.0.0/24',
    gateway: '10.20.0.1',
    dhcp: true,
    dhcp_start: '10.20.0.100',
    dhcp_end: '10.20.0.254',
    ...config
  },
  runtime: { egress_interface: 'eth0' }
});

function open(props: Record<string, unknown> = {}) {
  const confirm = vi.fn();
  const cancel = vi.fn();
  const view = render(NetworkConfigModal, {
    props: { open: false, labId: 'lab', ...props },
    events: { confirm: (event: CustomEvent) => confirm(event.detail), cancel }
  });
  return { view, confirm, cancel, show: () => view.rerender({ open: true }) };
}

const button = () => screen.getByTestId('network-confirm-button');

beforeEach(() => vi.mocked(apiRequest).mockClear());
afterEach(cleanup);

describe('NAT-Cloud form rules (#320)', () => {
  const base = { cidr: '10.20.0.0/24', gateway: '', dhcp: true, dhcpStart: '', dhcpEnd: '' };

  it('derives the defaults the server would use', () => {
    expect(natDefaults('10.20.0.0/22')).toEqual({
      gateway: '10.20.0.1',
      dhcpStart: '10.20.0.100',
      dhcpEnd: '10.20.3.254'
    });
    expect(natDefaults('10.20.0.0/28')).toBeNull();
  });

  it.each([
    [{ cidr: 'abc' }, 'cidr'],
    [{ cidr: '10.20.0.5/24' }, 'cidr'],
    [{ cidr: '10.20.0.0/28' }, 'cidr'],
    [{ gateway: '10.21.0.1' }, 'gateway'],
    [{ gateway: '10.20.0.255' }, 'gateway'],
    [{ dhcpStart: '10.20.0.50' }, 'dhcp_start'],
    [{ dhcpStart: '10.20.0.200', dhcpEnd: '10.20.0.150' }, 'dhcp_end']
  ])('rejects %o on %s', (change, field) => {
    expect(Object.keys(validateNatForm({ ...base, ...change }))).toEqual([field]);
  });

  it('ignores the scope while DHCP is off', () => {
    expect(validateNatForm({ ...base, dhcp: false, dhcpStart: 'junk' })).toEqual({});
  });
});

describe('NetworkConfigModal NAT-Cloud (#320)', () => {
  it('opens an existing NAT-Cloud with locked subnet settings and nothing to save', async () => {
    const { show } = open({ mode: 'edit', network: nat() });
    await show();

    expect(screen.getByRole('dialog', { name: 'Configure NAT-Cloud' })).toBeInTheDocument();
    for (const label of [/^CIDR/, /^Gateway/, /^Egress/]) {
      expect(screen.getByLabelText(label)).toHaveAttribute('readonly');
    }
    expect(screen.getByText(/delete this NAT-Cloud and create a new one/i)).toBeInTheDocument();
    expect(screen.queryByTestId('network-type-select')).toBeNull();
    expect(screen.getByLabelText('DHCP start')).toHaveValue('10.20.0.100');
    expect(button()).toHaveTextContent('Save changes');
    expect(button()).toBeDisabled();
  });

  it('hides the scope and explains leases when DHCP is turned off, then sends only dhcp', async () => {
    const { show, confirm } = open({ mode: 'edit', network: nat() });
    await show();
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));

    expect(screen.queryByLabelText('DHCP start')).toBeNull();
    expect(screen.getByText(/keep their current addresses/i)).toBeInTheDocument();
    await fireEvent.click(button());

    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'internet', type: 'nat_cloud', config: { dhcp: false } })
    );
    expect(apiRequest).toHaveBeenCalledWith('/labs/lab/networks/validate-nat-cloud', {
      method: 'POST',
      body: { config: { dhcp: false }, network_id: 3 },
      suppressToast: true
    });
  });

  it('shows the live DHCP server state, and when it cannot be read', async () => {
    vi.mocked(apiGetData).mockResolvedValueOnce({
      ip: { dhcp: { enabled: true, running: true, lease_count: 2, leases: [] } }
    } as never);
    await open({ mode: 'edit', network: nat() }).show();
    expect(await screen.findByText(/Server: Running · 2 leased/)).toBeInTheDocument();
    cleanup();

    vi.mocked(apiGetData).mockRejectedValueOnce(new Error('offline'));
    await open({ mode: 'edit', network: nat() }).show();
    expect(await screen.findByText('Server: status unavailable')).toBeInTheDocument();
  });

  it('renames without sending or re-validating any config', async () => {
    const { show, confirm } = open({ mode: 'edit', network: nat() });
    await show();
    await fireEvent.input(screen.getByTestId('network-name-input'), { target: { value: 'wan' } });
    await fireEvent.click(button());

    expect(confirm).toHaveBeenCalledWith({ name: 'wan', type: 'nat_cloud', config: {} });
    expect(apiRequest).not.toHaveBeenCalled();
  });

  it('shows a field error while typing and blocks saving', async () => {
    const { show } = open({ mode: 'edit', network: nat() });
    await show();
    await fireEvent.input(screen.getByLabelText('DHCP start'), { target: { value: '10.20.0.20' } });

    expect(screen.getByLabelText('DHCP start')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent(/reserved for static use/);
    expect(button()).toBeDisabled();
  });

  it('puts a server conflict on the CIDR field and does not confirm', async () => {
    const { show, confirm } = open({ defaultType: 'nat_cloud', defaultName: 'net-1' });
    await show();
    await fireEvent.input(screen.getByLabelText('CIDR'), { target: { value: '192.168.1.0/24' } });
    vi.mocked(apiRequest).mockRejectedValueOnce(
      new ApiError('NAT-Cloud CIDR overlaps host interface br-eth0.', { field: 'config.cidr' } as never)
    );
    await fireEvent.click(button());

    expect(await screen.findByText(/overlaps host interface br-eth0/)).toHaveAttribute('id', 'nat-cidr-error');
    expect(confirm).not.toHaveBeenCalled();

    await fireEvent.input(screen.getByLabelText('CIDR'), { target: { value: '10.30.0.0/24' } });
    expect(screen.queryByText(/overlaps host interface/)).toBeNull();
  });

  it('blocks double submission while saving and shows a failed save', async () => {
    const { view, show } = open({ mode: 'edit', network: nat() });
    await show();
    await view.rerender({ saving: true });
    expect(button()).toHaveTextContent('Saving…');
    expect(button()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();

    await view.rerender({ saving: false, saveError: { message: 'Could not apply the DHCP settings.' } });
    expect(screen.getByRole('alert')).toHaveTextContent('Could not apply the DHCP settings.');
  });

  it('cancels, not saves, on Enter over the Cancel button and keeps Tab inside', async () => {
    const { show, confirm, cancel } = open({ defaultType: 'nat_cloud', defaultName: 'net-1' });
    await show();
    const cancelButton = screen.getByRole('button', { name: 'Cancel' });
    await fireEvent.keyDown(cancelButton, { key: 'Enter' });
    expect(confirm).not.toHaveBeenCalled();

    button().focus();
    await fireEvent.keyDown(button(), { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Cancel new network' })).toHaveFocus();
    await fireEvent.keyDown(document.body, { key: 'Escape' });
    expect(cancel).toHaveBeenCalled();
  });
});
