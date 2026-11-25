// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';

const { apiGetData } = vi.hoisted(() => ({ apiGetData: vi.fn() }));
vi.mock('$lib/api', () => ({ apiGetData }));

import InfoPanelStack from '$lib/components/canvas/InfoPanelStack.svelte';
import NetworkConfigModal from '$lib/components/canvas/NetworkConfigModal.svelte';
import NetworkInfoContent from '$lib/components/canvas/NetworkInfoContent.svelte';
import { networkSharing } from '$lib/services/networkSharing';
import { infoPanels } from '$lib/stores/infoPanels';
import type { NetworkData, NetworkInfo, NodeData } from '$lib/types';

function network(overrides: Partial<NetworkData> = {}): NetworkData {
  return {
    id: 3, name: 'internet', type: 'nat_cloud', left: 0, top: 0, icon: '', visibility: true, count: 1,
    config: {}, ...overrides
  };
}

function info(overrides: Partial<NetworkInfo> = {}): NetworkInfo {
  return {
    id: 3, name: 'internet', type: 'nat_cloud', type_label: 'NAT-Cloud', visible: true,
    created_at: null,
    bridge: {
      name: 'noveabcdn3', driver: 'nat_cloud', exists: true, admin_up: true,
      oper_state: 'up', mac: '02:00:00:00:00:01', mtu: 1500, ports: 2
    },
    host_bridge: null,
    ip: {
      cidr: '10.255.0.0/24', gateway: '10.255.0.1', egress_interface: 'eth0', nat: true,
      static_start: '10.255.0.2', static_end: '10.255.0.99', used_ips: ['10.255.0.2'],
      dhcp: {
        enabled: true, start: '10.255.0.100', end: '10.255.0.254', size: 155, lease_time: '12h',
        running: true, pid: 9, lease_count: 1,
        leases: [{ ip: '10.255.0.140', mac: '50:00:00:01:00:00', hostname: 'r1', node_name: 'edge-1', expires_at: null }]
      }
    },
    endpoints: [
      { lab_name: 'lab-a', current_lab: true, node_id: 1, node_name: 'edge-1', interface: 'Gi0', mac: '50:00:00:01:00:00' }
    ],
    mac_conflicts: [],
    sharing: { role: 'none', shareable: true, reason: null, owner_lab: null, owner_missing: false, labs: [] },
    ...overrides
  };
}

beforeEach(() => {
  apiGetData.mockReset();
  apiGetData.mockResolvedValue(info());
});

afterEach(() => infoPanels.reset());

describe('NetworkInfoContent (#318)', () => {
  it('shows the DHCP scope, server state, and leases of a NAT-Cloud', async () => {
    render(NetworkInfoContent, { labId: 'a.json', network: network() });
    expect(await screen.findByText('10.255.0.100 – 10.255.0.254')).toBeInTheDocument();
    expect(apiGetData).toHaveBeenCalledWith('/labs/a.json/networks/3/info', expect.anything());
    for (const text of ['Running', '1 of 155 leased', '12h', '10.255.0.2 – 10.255.0.99', '10.255.0.140', 'noveabcdn3']) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
  });

  it('does not repeat the NAT-Cloud gateway as the DHCP server address (#325)', async () => {
    const data = info();
    data.ip!.dhcp!.server = '10.255.0.1';
    apiGetData.mockResolvedValue(data);
    render(NetworkInfoContent, { labId: 'a.json', network: network() });
    expect(await screen.findByText('10.255.0.100 – 10.255.0.254')).toBeInTheDocument();
    expect(screen.getAllByText('10.255.0.1')).toHaveLength(1);
    expect(screen.queryByText('Server address')).toBeNull();
  });

  it('says when DHCP is off instead of showing a scope', async () => {
    const data = info();
    Object.assign(data.ip!.dhcp!, { enabled: false, running: false });
    apiGetData.mockResolvedValue(data);
    render(NetworkInfoContent, { labId: 'a.json', network: network() });
    expect(await screen.findByText('Disabled')).toBeInTheDocument();
    expect(screen.queryByText('Scope')).toBeNull();
  });

  it.each([
    [true, true, 'Running', ''],
    [true, false, 'Not running', 'Start it from Configure…'],
    [false, true, 'Still running', 'Stop it from Configure…'],
    [false, false, 'Disabled', '']
  ])('reports the live server (enabled %s, running %s, #320)', async (enabled, running, label, problem) => {
    const data = info();
    Object.assign(data.ip!.dhcp!, { enabled, running });
    apiGetData.mockResolvedValue(data);
    render(NetworkInfoContent, { labId: 'a.json', network: network() });
    expect(await screen.findByTestId('network-info-dhcp-status')).toHaveTextContent(label);
    if (problem) expect(screen.getByRole('alert')).toHaveTextContent(problem);
    else expect(screen.queryByRole('alert')).toBeNull();
  });

  it('lists leases with device, MAC and remaining time, and keeps them after DHCP is off (#320)', async () => {
    const data = info();
    data.ip!.dhcp!.enabled = false;
    data.ip!.dhcp!.running = false;
    data.ip!.dhcp!.leases[0].expires_at = new Date(Date.now() + 90 * 60_000).toISOString();
    apiGetData.mockResolvedValue(data);
    render(NetworkInfoContent, { labId: 'a.json', network: network() });
    const leases = within(await screen.findByTestId('network-info-leases'));
    for (const text of ['Leases (1)', '10.255.0.140', 'edge-1', '50:00:00:01:00:00', '1 h 30 min left']) {
      expect(leases.getByText(text)).toBeInTheDocument();
    }
    expect(leases.getByText(/before DHCP was disabled/)).toBeInTheDocument();
  });

  it('says when nothing is leased yet and reloads when DHCP settings change (#320)', async () => {
    const data = info();
    Object.assign(data.ip!.dhcp!, { leases: [], lease_count: 0 });
    apiGetData.mockResolvedValue(data);
    const view = render(NetworkInfoContent, { labId: 'a.json', network: network({ config: { dhcp: true } }) });
    expect(await screen.findByText('No addresses leased yet.')).toBeInTheDocument();
    expect(apiGetData).toHaveBeenCalledTimes(1);
    await view.rerender({ network: network({ config: { dhcp: false } }) });
    await waitFor(() => expect(apiGetData).toHaveBeenCalledTimes(2));
  });

  it('omits addressing for a layer 2 bridge', async () => {
    apiGetData.mockResolvedValue(info({ type: 'linux_bridge', type_label: 'Isolated bridge', ip: null, endpoints: [] }));
    render(NetworkInfoContent, { labId: 'a.json', network: network({ type: 'linux_bridge' }) });
    expect(await screen.findByText('Isolated bridge')).toBeInTheDocument();
    expect(screen.queryByText('DHCP')).toBeNull();
    expect(screen.getByText('Nothing is connected.')).toBeInTheDocument();
  });

  const bridgeIp = (dhcp: NonNullable<NetworkInfo['ip']>['dhcp']): NetworkInfo['ip'] => ({
    cidr: '10.1.2.0/24', gateway: null, egress_interface: null, nat: false,
    static_start: null, static_end: null, used_ips: [], dhcp
  });

  it('shows a bridge subnet with its DHCP server address and scope (#325)', async () => {
    apiGetData.mockResolvedValue(info({
      type: 'linux_bridge', type_label: 'Isolated bridge',
      ip: bridgeIp({
        enabled: true, server: '10.1.2.254', start: '10.1.2.128', end: '10.1.2.253', size: 126, lease_time: '12h',
        running: true, pid: 12, lease_count: 0, leases: []
      })
    }));
    render(NetworkInfoContent, { labId: 'a.json', network: network({ type: 'linux_bridge' }) });
    const dhcp = within(await screen.findByTestId('network-info-dhcp'));
    for (const text of ['Running', '10.1.2.254', '10.1.2.128 – 10.1.2.253', '0 of 126 leased']) {
      expect(dhcp.getByText(text)).toBeInTheDocument();
    }
    expect(screen.getByText('10.1.2.0/24')).toBeInTheDocument();
    expect(screen.queryByText('Internet via')).toBeNull();
  });

  it('says DHCP is off on a bridge whose subnet is only a label (#325)', async () => {
    apiGetData.mockResolvedValue(info({ type: 'linux_bridge', type_label: 'Isolated bridge', ip: bridgeIp(null) }));
    render(NetworkInfoContent, { labId: 'a.json', network: network({ type: 'linux_bridge' }) });
    expect(await screen.findByText('10.1.2.0/24')).toBeInTheDocument();
    expect(screen.getByText('DHCP')).toBeInTheDocument();
    expect(screen.getByText('Off')).toBeInTheDocument();
    expect(screen.queryByTestId('network-info-dhcp')).toBeNull();
  });

  it('shows the saved bridge subnet and DHCP state before details load, and reloads when they change (#325)', async () => {
    apiGetData.mockRejectedValue(new Error('offline'));
    const view = render(NetworkInfoContent, {
      labId: 'a.json', network: network({ type: 'linux_bridge', config: { cidr: '10.1.2.0/29', dhcp: true } })
    });
    expect(await screen.findByRole('status')).toHaveTextContent('Could not load details');
    expect(screen.getByText('10.1.2.0/29')).toBeInTheDocument();
    expect(screen.getByText('On')).toBeInTheDocument();
    await view.rerender({
      network: network({ type: 'linux_bridge', config: { cidr: '10.1.2.0/29', dhcp: true, dhcp_server: '10.1.2.1' } })
    });
    await waitFor(() => expect(apiGetData).toHaveBeenCalledTimes(2));
  });

  it('names the labs on a shared segment and warns about duplicate MACs', async () => {
    apiGetData.mockResolvedValue(
      info({
        mac_conflicts: [{ mac: '50:00:00:01:00:00', endpoints: ['lab-a / r1 Gi0', 'lab-b / r1 Gi0'] }],
        sharing: {
          role: 'member', shareable: false, reason: 'x', owner_lab: 'lab-b', owner_missing: false,
          labs: [
            { lab_name: 'lab-b', lab_path: 'b.json', network_name: 'core', role: 'owner', current_lab: false, connections: 2 },
            { lab_name: 'lab-a', lab_path: 'a.json', network_name: 'internet', role: 'member', current_lab: true, connections: 1 }
          ]
        }
      })
    );
    render(NetworkInfoContent, { labId: 'a.json', network: network() });
    expect(await screen.findByText('Shared by lab-b')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'lab-b' })).toHaveAttribute('href', '/labs/b.json');
    expect(screen.getByRole('alert')).toHaveTextContent('lab-a / r1 Gi0, lab-b / r1 Gi0');
  });

  it('keeps the basic fields when the details cannot be loaded', async () => {
    apiGetData.mockRejectedValue(new Error('offline'));
    render(NetworkInfoContent, { labId: 'a.json', network: network() });
    expect(await screen.findByRole('status')).toHaveTextContent('Could not load details: offline');
    expect(screen.getByText('nat_cloud')).toBeInTheDocument();
  });

  it('reloads when the network changes and stops after unmount', async () => {
    const view = render(NetworkInfoContent, { labId: 'a.json', network: network() });
    await waitFor(() => expect(apiGetData).toHaveBeenCalledTimes(1));
    await view.rerender({ network: network({ count: 2 }) });
    await waitFor(() => expect(apiGetData).toHaveBeenCalledTimes(2));
    vi.useFakeTimers();
    view.unmount();
    vi.advanceTimersByTime(60_000);
    vi.useRealTimers();
    expect(apiGetData).toHaveBeenCalledTimes(2);
  });
});

describe('InfoPanelStack (#318)', () => {
  const node = {
    id: 1, name: 'r1', type: 'qemu', template: 'vyos', image: 'a-very-long-image-name', console: 'telnet',
    cpu: 1, ram: 512, ethernet: 1, status: 2, interfaces: []
  } as unknown as NodeData;

  it('uses the main font and wraps values in node and network panels', async () => {
    const { container } = render(InfoPanelStack, {
      labId: 'a.json', nodesById: { '1': node }, networksById: { '3': network() }
    });
    infoPanels.open('node', 1);
    infoPanels.open('network', 3);
    await screen.findByText('Running');
    expect(container.querySelector('.font-mono')).toBeNull();
    expect(screen.getByText('a-very-long-image-name')).not.toHaveClass('truncate');
  });

  it('hides earlier panels in Zen mode and shows the ones opened there', async () => {
    const view = render(InfoPanelStack, {
      labId: 'a.json', nodesById: { '1': node }, networksById: { '3': network() }
    });
    infoPanels.open('node', 1);
    await tick();
    await view.rerender({ zenMode: true });
    expect(screen.getByText('a-very-long-image-name')).not.toBeVisible();

    infoPanels.open('network', 3);
    expect(await screen.findByText('Running')).toBeVisible();
    infoPanels.open('node', 1);
    await tick();
    expect(screen.getByText('a-very-long-image-name')).toBeVisible();

    await view.rerender({ zenMode: false });
    expect(screen.getAllByTestId('info-panel')).toHaveLength(2);
  });
});

describe('networkSharing (#319)', () => {
  it.each([
    [network(), null, true],
    [network({ shared: true }), 'owner', true],
    [network({ type: 'linux_bridge', config: { shared_network_id: 'shared-net:b:1' } }), 'member', false],
    [network({ type: 'linux_bridge', config: { shared_cloud_id: 'nat-cloud:b:1' } }), null, true],
    [network({ type: undefined }), null, true],
    [network({ config: { shared_cloud_id: 'nat-cloud:b:1' } }), 'member', false],
    [network({ type: 'bridge_cloud' }), null, false],
    [network({ type: 'pnet0' }), null, false]
  ])('case %#', (input, role, canToggle) => {
    expect(networkSharing(input)).toMatchObject({ role, canToggle });
  });
});

describe('NetworkConfigModal shared networks (#319)', () => {
  it('joins the chosen network with its owner type', async () => {
    apiGetData.mockResolvedValue([
      { id: 'shared-net:b:1', type: 'linux_bridge', name: 'core', lab_id: 'b', lab_name: 'Lab B', cidr: null },
      { id: 'shared-net:c:4', type: 'nat_cloud', name: 'inet', lab_id: 'c', lab_name: 'Lab C', cidr: '10.255.0.0/24' }
    ]);
    const confirmed = vi.fn();
    render(NetworkConfigModal, {
      props: { open: true, labId: 'team/a.json', defaultName: 'net-1', defaultType: 'shared_network' },
      events: { confirm: (event: CustomEvent) => confirmed(event.detail) }
    } as never);
    const select = await screen.findByTestId('network-shared-select');
    expect(apiGetData).toHaveBeenCalledTimes(1);
    expect(apiGetData.mock.calls[0][0]).toBe('/system/shared-networks?for_lab=team%2Fa.json');
    await fireEvent.change(select, { target: { value: 'shared-net:c:4' } });
    await fireEvent.click(screen.getByTestId('network-confirm-button'));
    expect(confirmed).toHaveBeenCalledWith({
      name: 'net-1', type: 'nat_cloud', config: { shared_network_id: 'shared-net:c:4' }
    });
  });

  it('cannot be submitted when nothing is offered', async () => {
    apiGetData.mockResolvedValue([]);
    render(NetworkConfigModal, { open: true, labId: 'a.json', defaultName: 'net-1', defaultType: 'shared_network' });
    expect(await screen.findByText(/No other lab offers a network/)).toBeInTheDocument();
    expect(screen.getByTestId('network-confirm-button')).toBeDisabled();
    expect(within(screen.getByTestId('network-type-select')).getByText(/Shared network/)).toBeInTheDocument();
  });
});
