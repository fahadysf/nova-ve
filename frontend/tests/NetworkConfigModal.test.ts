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
    expect(screen.getByRole('alert')).toHaveTextContent(/10\.20\.0\.100 or later/);
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

  it('sends only what changed, and blank as a return to the default', async () => {
    const { show, confirm } = open({ mode: 'edit', network: nat({ dhcp_start: '10.20.0.200' }) });
    await show();
    await fireEvent.input(screen.getByLabelText('DHCP start'), { target: { value: '' } });
    await fireEvent.click(button());

    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'internet', type: 'nat_cloud', config: { dhcp_start: '' } })
    );
  });

  it('drops a check that finishes after the dialog was reopened for another network', async () => {
    const { view, show, confirm } = open({ mode: 'edit', network: nat() });
    await show();
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    let finish: (value: unknown) => void = () => {};
    vi.mocked(apiRequest).mockReturnValueOnce(new Promise((resolve) => (finish = resolve)) as never);
    await fireEvent.click(button());
    expect(button()).toHaveTextContent('Validating…');

    await view.rerender({ open: false });
    await view.rerender({ open: true, network: { ...nat(), id: 4, name: 'other' } });
    finish({ data: {} });
    await new Promise((resolve) => setTimeout(resolve));

    expect(confirm).not.toHaveBeenCalled();
    expect(screen.getByTestId('network-name-input')).toHaveValue('other');
    expect(button()).toHaveTextContent('Save changes');
  });

  it.each([
    [true, false, 'Start DHCP server'],
    [false, true, 'Stop DHCP server']
  ])('offers to repair a drifted server (enabled %s, running %s)', async (enabled, running, label) => {
    vi.mocked(apiGetData).mockResolvedValueOnce({
      ip: { dhcp: { enabled, running, lease_count: 0, leases: [] } }
    } as never);
    const { show, confirm } = open({ mode: 'edit', network: nat({ dhcp: enabled }) });
    await show();
    const repair = await screen.findByRole('button', { name: label });
    expect(repair).toBeEnabled();
    await fireEvent.click(repair);

    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'internet', type: 'nat_cloud', config: { dhcp: enabled } })
    );
  });

  it('renames a drifted network without touching its DHCP server', async () => {
    vi.mocked(apiGetData).mockResolvedValueOnce({
      ip: { dhcp: { enabled: true, running: false, lease_count: 0, leases: [] } }
    } as never);
    const { show, confirm } = open({ mode: 'edit', network: nat() });
    await show();
    await screen.findByRole('button', { name: 'Start DHCP server' });
    await fireEvent.input(screen.getByTestId('network-name-input'), { target: { value: 'wan' } });
    await fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(confirm).toHaveBeenCalledWith({ name: 'wan', type: 'nat_cloud', config: {} });
  });

  it('submits the name that was validated, not one edited during the check', async () => {
    const { show, confirm } = open({ defaultType: 'nat_cloud', defaultName: 'net-1' });
    await show();
    let finish: (value: unknown) => void = () => {};
    vi.mocked(apiRequest).mockReturnValueOnce(new Promise((resolve) => (finish = resolve)) as never);
    await fireEvent.click(button());
    await fireEvent.input(screen.getByTestId('network-name-input'), { target: { value: '' } });
    finish({ data: {} });

    await waitFor(() => expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ name: 'net-1' })));
  });

  it('keeps a server error through blur and links it to its field', async () => {
    const { show } = open({ defaultType: 'nat_cloud', defaultName: 'net-1' });
    await show();
    const egress = screen.getByLabelText('Egress');
    await fireEvent.input(egress, { target: { value: 'eth9' } });
    vi.mocked(apiRequest).mockRejectedValueOnce(
      new ApiError('Host has no network interface named eth9.', { field: 'config.egress_interface' } as never)
    );
    await fireEvent.click(button());
    const message = await screen.findByText(/no network interface named eth9/);
    await fireEvent.change(egress);

    expect(message).toBeInTheDocument();
    expect(egress).toHaveAttribute('aria-describedby', message.id);
    // Switching to an existing NAT-Cloud makes the verdict irrelevant.
    await fireEvent.click(screen.getByRole('button', { name: 'Existing' }));
    expect(screen.queryByText(/no network interface named eth9/)).toBeNull();
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
