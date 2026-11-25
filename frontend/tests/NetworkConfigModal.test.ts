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
    conflictingCidr?: string;
    source?: string;
    constructor(message: string, options?: { field?: string; conflictingCidr?: string; source?: string }) {
      super(message);
      this.field = options?.field;
      this.conflictingCidr = options?.conflictingCidr;
      this.source = options?.source;
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

const bridge = (config: Record<string, unknown> = {}, overrides: Partial<NetworkData> = {}): NetworkData => ({
  id: 7,
  name: 'core',
  type: 'linux_bridge',
  left: 0,
  top: 0,
  icon: '',
  visibility: true,
  config,
  ...overrides
});

const validateConfigCall = (config: Record<string, unknown>, networkId?: number) => [
  '/labs/lab/networks/validate-config',
  {
    method: 'POST',
    body: { type: 'linux_bridge', config, ...(networkId === undefined ? {} : { network_id: networkId }) },
    suppressToast: true
  }
];

async function openBridge(props: Record<string, unknown> = {}) {
  const opened = open({ defaultType: 'linux_bridge', defaultName: 'net-1', ...props });
  await opened.show();
  return opened;
}

const type = (label: string | RegExp, value: string) => fireEvent.input(screen.getByLabelText(label), { target: { value } });

describe('NetworkConfigModal linux_bridge create (#325)', () => {
  it('creates a plain layer 2 bridge without a check or config', async () => {
    const { confirm } = await openBridge();
    expect(screen.getByRole('dialog', { name: 'Configure new network' })).toHaveTextContent('Configure bridge');
    expect(screen.getByLabelText('CIDR')).not.toHaveAttribute('readonly');
    expect(screen.queryByLabelText('Enable DHCP')).toBeNull();
    expect(screen.getByTestId('bridge-dhcp-unavailable')).toHaveTextContent('Add a subnet from /22 to /29');
    // No NAT settings on a bridge.
    expect(screen.queryByLabelText(/^Gateway/)).toBeNull();
    expect(screen.queryByLabelText(/^Egress/)).toBeNull();
    await fireEvent.click(button());

    expect(confirm).toHaveBeenCalledWith({ name: 'net-1', type: 'linux_bridge', config: {} });
    expect(apiRequest).not.toHaveBeenCalled();
  });

  it('creates a bridge with an informational subnet and DHCP off by default', async () => {
    const { confirm } = await openBridge();
    await type('CIDR', '198.51.100.0/24');
    expect(screen.getByLabelText('Enable DHCP')).not.toBeChecked();
    expect(screen.queryByLabelText('DHCP server')).toBeNull();
    await fireEvent.click(button());

    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'net-1', type: 'linux_bridge', config: { cidr: '198.51.100.0/24' } })
    );
    expect(apiRequest).toHaveBeenCalledWith(...validateConfigCall({ cidr: '198.51.100.0/24' }));
  });

  it('prefills the scope from the subnet, warns about a second server, and sends only edits', async () => {
    const { confirm } = await openBridge();
    await type('CIDR', '10.1.2.0/29');
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));

    expect(screen.getByLabelText('DHCP server')).toHaveValue('10.1.2.6');
    expect(screen.getByLabelText('DHCP start')).toHaveValue('10.1.2.4');
    expect(screen.getByLabelText('DHCP end')).toHaveValue('10.1.2.5');
    expect(screen.getByTestId('bridge-dhcp-warning')).toHaveTextContent(/only one DHCP server.*lab\s+router/i);
    await fireEvent.click(button());
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({
        name: 'net-1', type: 'linux_bridge', config: { cidr: '10.1.2.0/29', dhcp: true }
      })
    );

    confirm.mockClear();
    await type('DHCP server', '10.1.2.1');
    await fireEvent.click(button());
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({
        name: 'net-1', type: 'linux_bridge', config: { cidr: '10.1.2.0/29', dhcp: true, dhcp_server: '10.1.2.1' }
      })
    );
  });

  it.each([
    ['10.0.0.0/21', false, /\/21 is too large/],
    ['10.1.2.0/30', false, /\/30 is too small/],
    ['10.1.2', false, /valid subnet first/],
    ['10.1.0.0/22', true, null],
    ['10.1.2.0/29', true, null]
  ])('offers DHCP for %s: %s', async (cidr, offered, hint) => {
    await openBridge();
    await type('CIDR', cidr);
    if (offered) {
      expect(screen.getByLabelText('Enable DHCP')).toBeInTheDocument();
      expect(screen.queryByTestId('bridge-dhcp-unavailable')).toBeNull();
    } else {
      expect(screen.queryByLabelText('Enable DHCP')).toBeNull();
      expect(screen.getByTestId('bridge-dhcp-unavailable')).toHaveTextContent(hint as RegExp);
    }
  });

  it('keeps the choice and edited values while the subnet is retyped, refilling only untouched defaults', async () => {
    const { confirm } = await openBridge();
    await type('CIDR', '10.1.0.0/24');
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    await type('DHCP start', '10.1.0.150');

    await type('CIDR', '10.1.0.0/2');
    expect(screen.queryByLabelText('DHCP start')).toBeNull();
    await type('CIDR', '10.1.0.0/23');

    expect(screen.getByLabelText('Enable DHCP')).toBeChecked();
    expect(screen.getByLabelText('DHCP start')).toHaveValue('10.1.0.150');
    expect(screen.getByLabelText('DHCP server')).toHaveValue('10.1.1.254');
    expect(screen.getByLabelText('DHCP end')).toHaveValue('10.1.1.253');
    await fireEvent.click(button());
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({
        name: 'net-1', type: 'linux_bridge', config: { cidr: '10.1.0.0/23', dhcp: true, dhcp_start: '10.1.0.150' }
      })
    );
  });

  it('does not send DHCP when the subnet stops qualifying', async () => {
    const { confirm } = await openBridge();
    await type('CIDR', '10.1.0.0/24');
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    await type('CIDR', '10.0.0.0/16');
    await fireEvent.click(button());
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'net-1', type: 'linux_bridge', config: { cidr: '10.0.0.0/16' } })
    );
  });

  it('marks invalid fields while typing and blocks saving', async () => {
    await openBridge();
    await type('CIDR', '10.1.2.3/24');
    expect(screen.getByLabelText('CIDR')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent('Did you mean 10.1.2.0/24?');
    expect(button()).toBeDisabled();

    await type('CIDR', '10.1.2.0/24');
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    await type('DHCP server', '10.1.2.200');
    expect(screen.getByLabelText('DHCP server')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent('Must be outside the DHCP range.');
    expect(screen.getByLabelText('DHCP server')).toHaveAttribute(
      'aria-describedby', 'bridge-dhcp-server-error bridge-dhcp-server-note'
    );
    expect(button()).toBeDisabled();
  });

  it.each([
    ['config.cidr', 'Subnet overlaps NAT-Cloud lab-b/inet (10.1.2.0/24).', 'bridge-cidr-error', 'CIDR'],
    ['config.dhcp_server', 'Address 10.1.2.254 is already used by node r1.', 'bridge-dhcp-server-error', 'DHCP server'],
    ['config.dhcp', 'DHCP needs a CIDR between /22 and /29', 'bridge-dhcp-error', 'Enable DHCP']
  ])('shows a server rejection of %s on its field', async (field, message, id, label) => {
    const { confirm } = await openBridge();
    await type('CIDR', '10.1.2.0/24');
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    vi.mocked(apiRequest).mockRejectedValueOnce(new ApiError(message, { field } as never));
    await fireEvent.click(button());

    expect(await screen.findByText(message)).toHaveAttribute('id', id);
    expect(screen.getByLabelText(label)).toHaveAttribute('aria-invalid', 'true');
    expect(confirm).not.toHaveBeenCalled();

    // A new value makes the verdict stale.
    await type('CIDR', '10.9.2.0/24');
    expect(screen.queryByText(message)).toBeNull();
  });

  it('blocks a second submit while the check runs', async () => {
    const { confirm } = await openBridge();
    await type('CIDR', '10.1.2.0/24');
    let finish: (value: unknown) => void = () => {};
    vi.mocked(apiRequest).mockReturnValueOnce(new Promise((resolve) => (finish = resolve)) as never);
    await fireEvent.click(button());
    expect(button()).toHaveTextContent('Validating…');
    expect(button()).toBeDisabled();
    expect(button()).toHaveAttribute('aria-busy', 'true');
    await fireEvent.click(button());
    await fireEvent.keyDown(screen.getByLabelText('CIDR'), { key: 'Enter' });
    expect(apiRequest).toHaveBeenCalledTimes(1);

    finish({ data: {} });
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
  });

  it('creates the type that was validated, not one picked during the check (N1)', async () => {
    const opened = open({ defaultType: 'nat_cloud', defaultName: 'net-1' });
    await opened.show();
    let finish: (value: unknown) => void = () => {};
    vi.mocked(apiRequest).mockReturnValueOnce(new Promise((resolve) => (finish = resolve)) as never);
    await fireEvent.click(button());
    expect(button()).toHaveTextContent('Validating…');
    await fireEvent.change(screen.getByTestId('network-type-select'), { target: { value: 'linux_bridge' } });
    finish({ data: {} });

    await waitFor(() =>
      expect(opened.confirm).toHaveBeenCalledWith({ name: 'net-1', type: 'nat_cloud', config: { dhcp: true } })
    );
  });

  it('submits with Enter from a field and cancels with Escape', async () => {
    const { confirm, cancel } = await openBridge();
    await type('CIDR', '10.1.2.0/24');
    await fireEvent.keyDown(screen.getByLabelText('CIDR'), { key: 'Enter' });
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    await fireEvent.keyDown(screen.getByLabelText('CIDR'), { key: 'Escape' });
    expect(cancel).toHaveBeenCalled();
  });
});

describe('NetworkConfigModal backend contract (#325)', () => {
  it('shows a 409 subnet conflict on the CIDR field, naming the subnet', async () => {
    const { confirm } = await openBridge();
    await type('CIDR', '10.20.0.0/24');
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    vi.mocked(apiRequest).mockRejectedValueOnce(
      new ApiError('Subnet is in use.', {
        field: 'config.cidr', conflictingCidr: '10.20.0.0/16', source: 'a NAT-Cloud in another lab'
      } as never)
    );
    await fireEvent.click(button());

    const message = await screen.findByText('Subnet is in use. It overlaps a NAT-Cloud in another lab (10.20.0.0/16).');
    expect(message).toHaveAttribute('id', 'bridge-cidr-error');
    expect(screen.getByLabelText('CIDR')).toHaveAttribute('aria-describedby', 'bridge-cidr-error bridge-cidr-note');
    expect(confirm).not.toHaveBeenCalled();
  });

  it('shows a refused type change as a form error', async () => {
    const network = bridge({
      cidr: '10.1.2.0/29', dhcp: true, dhcp_server: '10.1.2.6', dhcp_start: '10.1.2.4', dhcp_end: '10.1.2.5'
    });
    const { view, show } = open({ mode: 'edit', network });
    await show();
    await view.rerender({ saveError: { message: 'Turn DHCP off before changing the network type.', field: 'type' } });
    expect(screen.getByRole('alert')).toHaveTextContent('Turn DHCP off before changing the network type.');
  });

  it('shows a bare dhcp: true from before #325 as off and enables it properly', async () => {
    const { show, confirm } = open({ mode: 'edit', network: bridge({ cidr: '10.1.2.0/24', dhcp: true }) });
    await show();
    const toggle = screen.getByLabelText('Enable DHCP');
    expect(toggle).not.toBeChecked();
    expect(screen.queryByText(/keep their current addresses/)).toBeNull();
    await fireEvent.click(toggle);
    expect(screen.getByLabelText('DHCP server')).toHaveValue('10.1.2.254');
    await fireEvent.click(button());
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'core', type: 'linux_bridge', config: { dhcp: true } })
    );
  });

  it('never offers DHCP on a point-to-point link', async () => {
    await open({ mode: 'edit', network: bridge({ cidr: '10.1.2.0/24' }, { implicit: true }) }).show();
    expect(screen.queryByLabelText('Enable DHCP')).toBeNull();
    expect(screen.getByTestId('bridge-dhcp-unavailable')).toHaveTextContent('not available on point-to-point links');
  });
});

describe('NetworkConfigModal linux_bridge edit (#325)', () => {
  const dhcpBridge = () =>
    bridge({ cidr: '10.1.2.0/29', dhcp: true, dhcp_server: '10.1.2.6', dhcp_start: '10.1.2.4', dhcp_end: '10.1.2.5' });

  it('locks a saved subnet and says why', async () => {
    await open({ mode: 'edit', network: dhcpBridge() }).show();
    expect(screen.getByRole('dialog', { name: 'Configure bridge' })).toBeInTheDocument();
    const cidr = screen.getByLabelText(/^CIDR/);
    expect(cidr).toHaveAttribute('readonly');
    expect(cidr).toHaveValue('10.1.2.0/29');
    expect(cidr).toHaveAttribute('aria-describedby', 'bridge-locked-note');
    expect(screen.getByText(/delete and recreate the network/)).toHaveAttribute('id', 'bridge-locked-note');
    expect(screen.getByLabelText('Enable DHCP')).toBeChecked();
    expect(screen.getByLabelText('DHCP server')).toHaveValue('10.1.2.6');
    expect(screen.queryByTestId('network-type-select')).toBeNull();
    expect(button()).toHaveTextContent('Save changes');
    expect(button()).toBeDisabled();
  });

  it('turns DHCP off with the lease note and sends only dhcp', async () => {
    const { show, confirm } = open({ mode: 'edit', network: dhcpBridge() });
    await show();
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    expect(screen.queryByLabelText('DHCP server')).toBeNull();
    expect(screen.getByText(/keep their current addresses until the lease expires/)).toBeInTheDocument();
    await fireEvent.click(button());

    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'core', type: 'linux_bridge', config: { dhcp: false } })
    );
    expect(apiRequest).toHaveBeenCalledWith(...validateConfigCall({ dhcp: false }, 7));
  });

  it('sends a changed scope value and blank as a return to the default', async () => {
    const { show, confirm } = open({ mode: 'edit', network: dhcpBridge() });
    await show();
    await type('DHCP server', '');
    expect(screen.getByLabelText('DHCP server')).toHaveAttribute('placeholder', '10.1.2.6');
    await type('DHCP end', '10.1.2.4');
    await fireEvent.click(button());
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({
        name: 'core', type: 'linux_bridge', config: { dhcp_server: '', dhcp_end: '10.1.2.4' }
      })
    );
  });

  it('adds a subnet to a bridge that has none, and DHCP with it', async () => {
    const { show, confirm } = open({ mode: 'edit', network: bridge() });
    await show();
    const cidr = screen.getByLabelText('CIDR');
    expect(cidr).not.toHaveAttribute('readonly');
    expect(button()).toBeDisabled();
    await type('CIDR', '10.1.0.0/24');
    expect(button()).toBeEnabled();
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    expect(screen.getByLabelText('DHCP server')).toHaveValue('10.1.0.254');
    await fireEvent.click(button());

    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({
        name: 'core', type: 'linux_bridge', config: { cidr: '10.1.0.0/24', dhcp: true }
      })
    );
    expect(apiRequest).toHaveBeenCalledWith(...validateConfigCall({ cidr: '10.1.0.0/24', dhcp: true }, 7));
  });

  it('reuses the kept scope when DHCP is turned back on', async () => {
    const network = bridge({ cidr: '10.1.0.0/24', dhcp: false, dhcp_start: '10.1.0.200', dhcp_end: '10.1.0.220' });
    const { show, confirm } = open({ mode: 'edit', network });
    await show();
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    expect(screen.getByLabelText('DHCP start')).toHaveValue('10.1.0.200');
    expect(screen.getByLabelText('DHCP server')).toHaveValue('10.1.0.254');
    await fireEvent.click(button());
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'core', type: 'linux_bridge', config: { dhcp: true } })
    );
  });

  it('offers to start a stopped bridge DHCP server', async () => {
    vi.mocked(apiGetData).mockResolvedValueOnce({
      ip: { dhcp: { enabled: true, running: false, lease_count: 0, leases: [] } }
    } as never);
    const { show, confirm } = open({ mode: 'edit', network: dhcpBridge() });
    await show();
    expect(await screen.findByTestId('bridge-dhcp-status')).toHaveTextContent('Server: Not running');
    await fireEvent.click(await screen.findByRole('button', { name: 'Start DHCP server' }));
    await waitFor(() =>
      expect(confirm).toHaveBeenCalledWith({ name: 'core', type: 'linux_bridge', config: { dhcp: true } })
    );
  });

  it('shows a shared member read-only with the lab that owns it', async () => {
    vi.mocked(apiGetData).mockResolvedValueOnce({
      ip: { cidr: '10.1.2.0/24', dhcp: { enabled: true, start: '10.1.2.128', end: '10.1.2.253', running: true, lease_count: 0, leases: [] } },
      sharing: { role: 'member', owner_lab: 'lab-b' }
    } as never);
    const network = bridge({ shared_network_id: 'shared-net:b:1' });
    const { show, confirm } = open({ mode: 'edit', network });
    await show();

    const settings = await screen.findByTestId('network-member-settings');
    await waitFor(() => expect(settings).toHaveTextContent('Settings are managed by lab-b'));
    expect(settings).toHaveTextContent('10.1.2.0/24');
    expect(settings).toHaveTextContent('On · 10.1.2.128 – 10.1.2.253');
    expect(screen.queryByLabelText(/CIDR/)).toBeNull();
    expect(screen.queryByLabelText('Enable DHCP')).toBeNull();
    expect(button()).toBeDisabled();

    await fireEvent.input(screen.getByTestId('network-name-input'), { target: { value: 'core-b' } });
    await fireEvent.click(button());
    expect(confirm).toHaveBeenCalledWith({ name: 'core-b', type: 'linux_bridge', config: {} });
    expect(apiRequest).not.toHaveBeenCalled();
  });

  it('opens a shared NAT-Cloud member read-only too', async () => {
    await open({ mode: 'edit', network: nat({ shared_cloud_id: 'nat-cloud:b:1' }) }).show();
    expect(screen.getByTestId('network-member-settings')).toHaveTextContent(/managed by the lab that owns this network/);
    expect(screen.queryByLabelText(/^Gateway/)).toBeNull();
  });
});
