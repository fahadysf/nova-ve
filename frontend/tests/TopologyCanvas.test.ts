// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { get, writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { DragEndpoint, InterfaceEndpoint, NetworkEndpoint, NodeSlotEndpoint } from '$lib/stores/dragLink';
import { dragLinkStore } from '$lib/stores/dragLink';
import { selectedPortInfoStore } from '$lib/stores/portInfo';
import type { Link, NetworkData, NodeCatalog, NodeData, TemplateCapabilities } from '$lib/types';

vi.mock('@xyflow/svelte', async () => {
  const SvelteFlow = (await import('./mocks/SvelteFlowMock.svelte')).default;
  const SlotStub = (await import('./mocks/SlotStub.svelte')).default;
  const LeafStub = (await import('./mocks/LeafStub.svelte')).default;

  return {
    SvelteFlow,
    Background: LeafStub,
    MiniMap: LeafStub,
    Panel: SlotStub,
    useSvelteFlow: () => ({
      fitView: vi.fn(async () => {}),
      screenToFlowPosition: vi.fn(() => ({ x: 200, y: 200 })),
      zoomIn: vi.fn(async () => {}),
      zoomOut: vi.fn(async () => {}),
    }),
  };
});

vi.mock('$lib/api', () => {
  const emptyCatalog: NodeCatalog = {
    templates: [],
    icon_options: [],
    create_fields: [],
    edit_fields: [],
    runtime_editability: { always: [], stopped_only: [], immutable: [] },
  };

  return {
    ApiError: class ApiError extends Error {},
    apiRequest: vi.fn(async (path: string) => {
      if (path.endsWith('/node-catalog')) {
        return { data: emptyCatalog };
      }
      return { data: {} };
    }),
  };
});

const { debouncerMocks } = vi.hoisted(() => ({
  debouncerMocks: [] as Array<{
    pushNodePosition: ReturnType<typeof vi.fn>;
    pushNetworkPosition: ReturnType<typeof vi.fn>;
    pushViewport: ReturnType<typeof vi.fn>;
    flush: ReturnType<typeof vi.fn>;
    flushKeepalive: ReturnType<typeof vi.fn>;
    cancel: ReturnType<typeof vi.fn>;
    pending: ReturnType<typeof vi.fn>;
    pendingPositionIds: ReturnType<typeof vi.fn>;
  }>,
}));

vi.mock('$lib/services/labApi', () => ({
  LayoutConflictError: class LayoutConflictError extends Error {
    serverRevision: number | null = null;
  },
  createLayoutDebouncer: () => {
    const mock = {
      pushNodePosition: vi.fn(),
      pushNetworkPosition: vi.fn(),
      pushViewport: vi.fn(),
      flush: vi.fn(async () => {}),
      flushKeepalive: vi.fn(),
      cancel: vi.fn(),
      pending: vi.fn(() => false),
      pendingPositionIds: vi.fn(() => ({ nodeIds: new Set(), networkIds: new Set() })),
    };
    debouncerMocks.push(mock);
    return mock;
  },
}));

vi.mock('$lib/services/wsClient', () => ({
  createWsClient: () => ({
    on: vi.fn(),
    connect: vi.fn(),
    close: vi.fn(),
  }),
}));

vi.mock('$lib/stores/labWs', () => ({
  createLabWsStores: () => ({
    liveMacs: writable({}),
    linkStates: writable({}),
    nodeStates: writable({}),
    linkReconciliation: writable({}),
    connected: writable(false),
    onLinkChange: vi.fn(() => vi.fn()),
  }),
}));

import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';
import CanvasChangedHarness from './mocks/CanvasChangedHarness.svelte';
import { apiRequest } from '$lib/api';
import { infoPanels } from '$lib/stores/infoPanels';
import { labState } from '$lib/stores/labState';
import { toastStore } from '$lib/stores/toasts';
import { tick } from 'svelte';

function makeNode(
  id: number,
  name: string,
  status: 0 | 2,
  capabilities: TemplateCapabilities,
  interfaceCount = 1
): NodeData {
  return {
    id,
    name,
    type: 'qemu',
    template: 'vyos',
    image: 'vyos-1.4',
    console: 'telnet',
    status,
    delay: 0,
    cpu: 1,
    ram: 1024,
    ethernet: 4,
    left: 100 * id,
    top: 120,
    icon: 'Router.png',
    interfaces: Array.from({ length: interfaceCount }, (_, index) => ({
      index,
      name: `eth${index}`,
      network_id: 0,
      planned_mac: null,
      port_position: { side: 'right', offset: 0.25 + index * 0.25 },
    })),
    capabilities,
  };
}

function makeNetwork(id: number, name: string): NetworkData {
  return {
    id,
    name,
    type: 'bridge',
    left: 360,
    top: 120,
    icon: 'Cloud.png',
    visibility: true,
    count: 0,
    width: 110,
  };
}

function interfaceEndpoint(nodeId: number, interfaceName: string): InterfaceEndpoint {
  return {
    kind: 'interface',
    nodeId,
    interfaceIndex: 0,
    interfaceName,
    port: { side: 'right', offset: 0.5 },
  };
}

function networkEndpoint(networkId: number, networkName: string): NetworkEndpoint {
  return {
    kind: 'network',
    networkId,
    networkName,
    side: 'left',
    offset: 0.5,
  };
}

function nodeSlotEndpoint(nodeId: number, nodeName: string): NodeSlotEndpoint {
  return {
    kind: 'node-slot',
    nodeId,
    nodeName,
    port: { side: 'right', offset: 0.5 },
  };
}

// #245: the canvas no longer takes lab-state props — it renders the shared
// labState store. Seed the store before each mount.
function renderCanvas(
  nodes: Record<string, NodeData>,
  networks: Record<string, NetworkData> = {},
  links: Link[] = []
) {
  labState.reset();
  labState.applySnapshot({
    nodes,
    networks,
    topology: [],
    links,
    defaults: { link_style: 'orthogonal' },
  });
  return render(TopologyCanvas, {
    props: {
      labId: 'lab-hotplug',
      consoleSelectorWindows: [],
      consoleMinimizedWindows: [],
    },
  });
}

function makeSharingNetwork(overrides: Partial<NetworkData> = {}): NetworkData {
  return {
    id: 7, name: 'core', type: 'linux_bridge', left: 100, top: 100, icon: '',
    visibility: true, count: 0, config: {}, ...overrides,
  };
}

async function openNetworkMenu() {
  await fireEvent(screen.getByTestId('svelte-flow-mock'), new CustomEvent('mock-flow-event', {
    detail: {
      type: 'nodecontextmenu',
      detail: { node: { id: 'network7' }, event: new MouseEvent('contextmenu') },
    },
  }));
}

function driveConfirm(source: DragEndpoint, target: DragEndpoint) {
  dragLinkStore.start({ source, pointer: { x: 24, y: 24 } });
  dragLinkStore.move({ pointer: { x: 80, y: 80 } });
  dragLinkStore.move({ pointer: { x: 140, y: 120 }, target, nearTarget: true });
  dragLinkStore.release({ pointer: { x: 140, y: 120 }, targetReachable: true });
}

afterEach(() => {
  dragLinkStore.cancel();
  selectedPortInfoStore.set(null);
  labState.reset();
  cleanup();
});

describe('TopologyCanvas lab controls', () => {
  const capabilities = { hotplug: true, max_nics: 8, machine: 'q35' };
  const catalogPath = '/labs/lab-hotplug/node-catalog';
  const nodesPath = '/labs/lab-hotplug/nodes';
  const apiMock = vi.mocked(apiRequest);

  beforeEach(() => {
    apiMock.mockClear();
  });

  it('starts eligible nodes, blocks duplicate requests, and preserves success on partial failure', async () => {
    const first = makeNode(1, 'edge-a', 0, capabilities);
    const second = makeNode(2, 'edge-b', 0, capabilities);
    const transitioning: NodeData = {
      ...makeNode(4, 'edge-d', 0, capabilities),
      transientStatus: 'starting',
    };
    renderCanvas({
      '1': first,
      '2': second,
      '3': makeNode(3, 'edge-c', 2, capabilities),
      '4': transitioning,
    }, { '9': makeNetwork(9, 'bridge-a') });
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(catalogPath, expect.anything()));

    const toast = vi.spyOn(toastStore, 'push').mockImplementation(() => {});
    const start = screen.getByRole('button', { name: 'Start all nodes' });
    const stop = screen.getByRole('button', { name: 'Stop all nodes' });
    let finishFirst!: () => void;
    const firstStarted = new Promise<void>((resolve) => { finishFirst = resolve; });
    apiMock
      .mockImplementationOnce(async () => {
        await firstStarted;
        return { code: 200, status: 'success', message: '', data: {} };
      })
      .mockResolvedValueOnce({
        code: 200,
        status: 'success',
        message: '',
        data: { ...first, status: 2 },
      })
      .mockRejectedValueOnce(new Error('Image unavailable.'));

    try {
      expect(screen.getByRole('button', { name: 'Restart all nodes' })).toBeDisabled();
      expect(screen.getByRole('button', { name: 'Open all consoles' })).toBeDisabled();
      await fireEvent.click(start);
      expect(start).toBeDisabled();
      expect(stop).toBeDisabled();
      expect(screen.getByRole('status')).toHaveTextContent('Starting nodes (0/2)');
      expect(get(labState).nodes['1'].transientStatus).toBe('starting');
      await fireEvent.click(start);
      expect(apiMock.mock.calls.map(([path]) => path)).toEqual([catalogPath, `${nodesPath}/1/start`]);

      // A status response still has the old coordinates if the layout save
      // is pending when the bulk run finishes.
      labState.upsertNode(first.id, { ...get(labState).nodes['1'], left: 432 });
      finishFirst();
      await waitFor(() => expect(toast).toHaveBeenCalledWith(
        'Started 1 node. Failed to start edge-b. Image unavailable.', 'error',
      ));
      expect(apiMock.mock.calls.map(([path]) => path)).toEqual([
        catalogPath, `${nodesPath}/1/start`, `${nodesPath}/1`, `${nodesPath}/2/start`,
      ]);
      expect(get(labState).nodes['1']).toMatchObject({ status: 2, left: 432 });
      expect(get(labState).nodes['2'].transientStatus).toBeUndefined();
      expect(get(labState).nodes['4'].transientStatus).toBe('starting');
      expect(start).toBeEnabled();
      expect(stop).toBeEnabled();
    } finally {
      finishFirst();
      toast.mockRestore();
    }
  });

  it('stops running nodes and disables stop when they have all settled', async () => {
    const node = makeNode(1, 'edge-a', 2, capabilities);
    renderCanvas({ '1': node });
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(catalogPath, expect.anything()));

    const toast = vi.spyOn(toastStore, 'push').mockImplementation(() => {});
    apiMock.mockResolvedValueOnce({ code: 200, status: 'success', message: '', data: {} });
    apiMock.mockResolvedValueOnce({ code: 200, status: 'success', message: '', data: { ...node, status: 0 } });
    try {
      const start = screen.getByRole('button', { name: 'Start all nodes' });
      const stop = screen.getByRole('button', { name: 'Stop all nodes' });
      expect(start).toBeDisabled();
      await fireEvent.click(stop);
      await waitFor(() => expect(toast).toHaveBeenCalledWith('Stopped 1 node.', 'success'));
      expect(apiMock.mock.calls.map(([path]) => path)).toEqual([
        catalogPath, `${nodesPath}/1/stop`, `${nodesPath}/1`,
      ]);
      expect(stop).toBeDisabled();
      expect(start).toBeEnabled();
    } finally {
      toast.mockRestore();
    }
  });

  it('reports an accepted lifecycle request that never reaches its requested state', async () => {
    const node = makeNode(1, 'edge-a', 0, capabilities);
    renderCanvas({ '1': node });
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(catalogPath, expect.anything()));

    vi.useFakeTimers();
    const toast = vi.spyOn(toastStore, 'push').mockImplementation(() => {});
    apiMock.mockResolvedValueOnce({ code: 200, status: 'success', message: '', data: {} });
    for (let poll = 0; poll < 5; poll += 1) {
      apiMock.mockResolvedValueOnce({ code: 200, status: 'success', message: '', data: node });
    }
    try {
      const start = screen.getByRole('button', { name: 'Start all nodes' });
      await fireEvent.click(start);
      await vi.advanceTimersByTimeAsync(4000);
      expect(get(labState).nodes['1']).toMatchObject({ status: 0 });
      expect(get(labState).nodes['1'].transientStatus).toBeUndefined();
      expect(toast).toHaveBeenCalledWith(
        'Started 0 nodes. Failed to start edge-a. Node did not reach the requested state.', 'error',
      );
      expect(start).toBeEnabled();
    } finally {
      toast.mockRestore();
      vi.useRealTimers();
    }
  });

  it('keeps the canvas controls and topology mounted in Zen mode', async () => {
    const view = renderCanvas({ '1': makeNode(1, 'edge-a', 0, capabilities) });
    selectedPortInfoStore.set({
      kind: 'interface', nodeId: 1, interfaceIndex: 0, anchorRect: null,
      interfaceName: 'eth0', plannedMac: null,
    });
    const topology = screen.getByTestId('svelte-flow-mock');
    const nodeSummary = topology.getAttribute('data-flow-nodes');
    const controls = screen.getByRole('group', { name: 'Lab controls' });
    const zoom = screen.getByRole('button', { name: 'zoom in' });
    expect(await screen.findByRole('dialog', { name: 'Port info for eth0' })).toBeVisible();

    await view.rerender({ zenMode: true });
    expect(screen.getByRole('group', { name: 'Lab controls' })).toBe(controls);
    expect(screen.getByRole('button', { name: 'zoom in' })).toBe(zoom);
    expect(screen.getByRole('button', { name: 'center content' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'lock canvas' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Show minimap' })).toBeEnabled();
    expect(screen.queryByTestId('port-info-popover')).toBeNull();
    expect(screen.getByTestId('svelte-flow-mock')).toBe(topology);
    expect(topology).toHaveAttribute('data-flow-nodes', nodeSummary);

    await view.rerender({ zenMode: false });
    expect(screen.getByRole('group', { name: 'Lab controls' })).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Port info for eth0' })).toBeVisible();
    expect(screen.getByTestId('svelte-flow-mock')).toBe(topology);
  });

  it('opens node context menus and configuration dialogs in Zen mode', async () => {
    const view = renderCanvas({ '1': makeNode(1, 'edge-a', 0, capabilities) });
    await view.rerender({ zenMode: true });
    await fireEvent(screen.getByTestId('svelte-flow-mock'), new CustomEvent('mock-flow-event', {
      detail: {
        type: 'nodecontextmenu',
        detail: { node: { id: 'node1' }, event: new MouseEvent('contextmenu') },
      },
    }));
    const menu = screen.getByRole('menu');
    expect(within(menu).getByRole('button', { name: 'Start' })).toBeEnabled();
    await fireEvent.click(within(menu).getByRole('button', { name: 'Edit Node' }));
    expect(screen.getByRole('dialog', { name: 'Edit node' })).toBeVisible();
    await fireEvent.click(screen.getByRole('button', { name: 'Close node configuration modal' }));

    await fireEvent.click(screen.getByRole('button', { name: 'Open add element menu' }));
    await fireEvent.click(screen.getByRole('button', { name: /network container type/i }));
    await fireEvent.click(screen.getByRole('button', { name: /bridge network/i }));
    expect(screen.getByTestId('network-config-modal')).toBeVisible();
  });

  it.each([false, true])('opens network info from the context menu (zen: %s, #318)', async (zenMode) => {
    const view = renderCanvas({}, { '7': makeSharingNetwork() });
    await view.rerender({ zenMode });
    await openNetworkMenu();
    await fireEvent.click(screen.getByTestId('network-menu-info'));
    expect(screen.queryByRole('menu')).toBeNull();
    expect(infoPanels._peek()).toMatchObject([{ kind: 'network', refId: 7 }]);
    infoPanels.reset();
  });

  it('toggles sharing from the context menu and adopts the revision (#319)', async () => {
    renderCanvas({}, { '7': makeSharingNetwork() });
    vi.mocked(apiRequest).mockClear();
    for (const shared of [true, false]) {
      vi.mocked(apiRequest).mockResolvedValueOnce({
        message: 'ok', network: { ...makeSharingNetwork(), shared }, revision: shared ? 41 : 42,
      } as never);
      await openNetworkMenu();
      const toggle = screen.getByRole('menuitemcheckbox', { name: 'Make shared' });
      expect(toggle).toHaveAttribute('aria-checked', String(!shared));
      await fireEvent.click(toggle);
      await waitFor(() => expect(get(labState).networks['7'].shared).toBe(shared));
      expect(apiRequest).toHaveBeenLastCalledWith('/labs/lab-hotplug/networks/7/sharing', {
        method: 'PUT', body: { shared },
      });
      expect(get(labState).revision).toBe(shared ? 41 : 42);
    }
  });

  it('keeps the network shared when the server refuses to stop (#319)', async () => {
    renderCanvas({}, { '7': makeSharingNetwork({ shared: true }) });
    vi.mocked(apiRequest).mockRejectedValueOnce(new Error('in use by lab-b'));
    await openNetworkMenu();
    await fireEvent.click(screen.getByTestId('network-menu-sharing'));
    await tick();
    expect(get(labState).networks['7'].shared).toBe(true);
  });

  it('does not offer the toggle on a network joined from another lab (#319)', async () => {
    renderCanvas({}, { '7': makeSharingNetwork({ config: { shared_network_id: 'shared-net:b:1' } }) });
    await openNetworkMenu();
    const toggle = screen.getByRole('menuitemcheckbox', { name: 'Shared from another lab' });
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute('aria-checked', 'true');
  });

  const natConfig = {
    cidr: '10.20.0.0/24', gateway: '10.20.0.1', dhcp: true,
    dhcp_start: '10.20.0.100', dhcp_end: '10.20.0.254',
  };

  it('saves NAT-Cloud edits with PATCH and adopts the response (#320)', async () => {
    const network = makeSharingNetwork({ type: 'nat_cloud', config: natConfig });
    renderCanvas({}, { '7': network });
    await openNetworkMenu();
    await fireEvent.click(screen.getByTestId('network-menu-configure'));
    await fireEvent.click(screen.getByLabelText('Enable DHCP'));
    vi.mocked(apiRequest).mockClear();
    vi.mocked(apiRequest)
      .mockResolvedValueOnce({ data: {} } as never)
      .mockRejectedValueOnce(new Error('Could not apply the DHCP settings.'));

    await fireEvent.click(screen.getByTestId('network-confirm-button'));
    // A failed save keeps the dialog open with the reason.
    expect(await screen.findByText('Could not apply the DHCP settings.')).toBeInTheDocument();

    vi.mocked(apiRequest)
      .mockResolvedValueOnce({ data: {} } as never)
      .mockResolvedValueOnce({
        network: { ...network, config: { ...natConfig, dhcp: false } }, revision: 51,
      } as never);
    await fireEvent.click(screen.getByTestId('network-confirm-button'));

    await waitFor(() => expect(screen.queryByTestId('network-config-modal')).toBeNull());
    expect(apiRequest).toHaveBeenLastCalledWith('/labs/lab-hotplug/networks/7', {
      method: 'PATCH', body: { config: { dhcp: false } }, suppressToast: true,
    });
    expect(get(labState).networks['7'].config?.dhcp).toBe(false);
    expect(get(labState).revision).toBe(51);
    await openNetworkMenu();
    expect(screen.getByTestId('network-menu-configure')).toHaveTextContent('DHCP off');
  });

  it('keeps the create form open when the server refuses the network (#320)', async () => {
    renderCanvas({});
    await fireEvent.click(screen.getByRole('button', { name: 'Open add element menu' }));
    await fireEvent.click(await screen.findByRole('button', { name: /network container type/i }));
    await fireEvent.click(screen.getByRole('button', { name: /bridge network/i }));
    const name = screen.getByTestId('network-name-input');
    await fireEvent.input(name, { target: { value: 'taken' } });
    vi.mocked(apiRequest).mockRejectedValueOnce(new Error("A network named 'taken' already exists."));
    await fireEvent.click(screen.getByTestId('network-confirm-button'));

    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
    expect(name).toHaveValue('taken');

    await fireEvent.input(name, { target: { value: 'free' } });
    vi.mocked(apiRequest).mockResolvedValueOnce({
      network: makeSharingNetwork({ name: 'free' }), revision: 52,
    } as never);
    await fireEvent.click(screen.getByTestId('network-confirm-button'));

    await waitFor(() => expect(screen.queryByTestId('network-config-modal')).toBeNull());
    expect(get(labState).networks['7'].name).toBe('free');
  });

  it('offers Configure on NAT-Clouds and bridges, read-only for shared members (#320, #325)', async () => {
    renderCanvas({}, { '7': makeSharingNetwork({ type: 'bridge_cloud' }) });
    await openNetworkMenu();
    expect(screen.queryByTestId('network-menu-configure')).toBeNull();
    expect(screen.queryByTestId('network-menu-dhcp')).toBeNull();
    cleanup();

    renderCanvas({}, { '7': makeSharingNetwork({ implicit: true, config: { cidr: '10.1.2.0/24' } }) });
    await openNetworkMenu();
    expect(screen.queryByTestId('network-menu-configure')).toBeNull();
    expect(screen.queryByTestId('network-menu-dhcp')).toBeNull();
    cleanup();

    renderCanvas({}, {
      '7': makeSharingNetwork({ type: 'nat_cloud', config: { ...natConfig, shared_cloud_id: 'nat-cloud:b:1' } }),
    });
    await openNetworkMenu();
    const configure = screen.getByTestId('network-menu-configure');
    expect(configure).toBeEnabled();
    expect(configure).toHaveTextContent('Read-only');
    expect(configure).toHaveAttribute('title', expect.stringContaining('owns this network'));
    expect(screen.queryByTestId('network-menu-dhcp')).toBeNull();
    await fireEvent.click(configure);
    expect(screen.getByTestId('network-member-settings')).toBeInTheDocument();
  });

  const servedBridge = {
    cidr: '10.1.2.0/29', dhcp: true, dhcp_server: '10.1.2.6', dhcp_start: '10.1.2.4', dhcp_end: '10.1.2.5',
  };

  it.each([
    ['a bridge without a subnet', {}, null, 'DHCP off'],
    ['a bridge with a /21', { cidr: '10.0.0.0/21' }, null, 'DHCP off'],
    ['a bridge with a /24', { cidr: '10.1.2.0/24' }, 'Enable DHCP', 'DHCP off'],
    ['a bridge serving DHCP', servedBridge, 'Disable DHCP', 'DHCP on'],
    ['a bridge with a bare dhcp: true from before #325', { cidr: '10.1.2.0/24', dhcp: true }, 'Enable DHCP', 'DHCP off'],
    ['a shared bridge member', { cidr: '10.1.2.0/24', dhcp: true, shared_network_id: 'shared-net:b:1' }, null, 'Read-only'],
  ])('labels the DHCP actions of %s (#325)', async (_label, config, toggle, state) => {
    renderCanvas({}, { '7': makeSharingNetwork({ config }) });
    await openNetworkMenu();
    if (toggle) expect(screen.getByTestId('network-menu-dhcp')).toHaveTextContent(toggle);
    else expect(screen.queryByTestId('network-menu-dhcp')).toBeNull();
    expect(screen.getByTestId('network-menu-configure')).toHaveTextContent(new RegExp(`^Configure…\\s*${state}$`));
  });

  it.each([false, true])('turns NAT-Cloud DHCP off from the menu with progress and a lease note (zen: %s, #325)', async (zenMode) => {
    const network = makeSharingNetwork({ type: 'nat_cloud', config: natConfig });
    const view = renderCanvas({}, { '7': network });
    await view.rerender({ zenMode });
    const toast = vi.spyOn(toastStore, 'push').mockImplementation(() => {});
    await openNetworkMenu();
    const toggle = screen.getByTestId('network-menu-dhcp');
    expect(toggle).toHaveTextContent('Disable DHCP');
    expect(toggle).toHaveAttribute('title', expect.stringContaining('lease expires (12 h)'));

    vi.mocked(apiRequest).mockClear();
    let finish: (value: unknown) => void = () => {};
    vi.mocked(apiRequest).mockReturnValueOnce(new Promise((resolve) => (finish = resolve)) as never);
    await fireEvent.click(toggle);
    expect(toggle).toHaveTextContent('Disabling DHCP…');
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByTestId('network-menu-configure')).toBeDisabled();
    await fireEvent.click(toggle);
    expect(apiRequest).toHaveBeenCalledTimes(1);
    expect(apiRequest).toHaveBeenCalledWith('/labs/lab-hotplug/networks/7', {
      method: 'PATCH', body: { config: { dhcp: false } }, suppressToast: true,
    });

    finish({ network: { ...network, config: { ...natConfig, dhcp: false } }, revision: 61 });
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
    expect(get(labState).networks['7'].config?.dhcp).toBe(false);
    expect(get(labState).revision).toBe(61);
    expect(toast).toHaveBeenCalledWith(
      'DHCP disabled. Nodes keep their addresses until the lease expires (12 h) or they renew.', 'success', 8000
    );
    await openNetworkMenu();
    expect(screen.getByTestId('network-menu-dhcp')).toHaveTextContent('Enable DHCP');
    toast.mockRestore();
  });

  it('keeps the bridge setting and reports why when enabling DHCP fails (#325)', async () => {
    renderCanvas({}, { '7': makeSharingNetwork({ config: { cidr: '10.1.2.0/24' } }) });
    const toast = vi.spyOn(toastStore, 'push').mockImplementation(() => {});
    vi.mocked(apiRequest).mockRejectedValueOnce(new Error('Node r1 already uses 10.1.2.200 inside the DHCP range.'));
    await openNetworkMenu();
    await fireEvent.click(screen.getByTestId('network-menu-dhcp'));

    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'Could not enable DHCP on core: Node r1 already uses 10.1.2.200 inside the DHCP range.', 'error', 8000
    ));
    expect(get(labState).networks['7'].config?.dhcp).toBeUndefined();
    await openNetworkMenu();
    expect(screen.getByTestId('network-menu-dhcp')).toBeEnabled();
    expect(screen.getByTestId('network-menu-dhcp')).toHaveTextContent('Enable DHCP');
    toast.mockRestore();
  });

  it('enables bridge DHCP from the menu and says whether its server runs (#325)', async () => {
    const network = makeSharingNetwork({ config: { cidr: '10.1.2.0/24' } });
    renderCanvas({}, { '7': network });
    const toast = vi.spyOn(toastStore, 'push').mockImplementation(() => {});
    vi.mocked(apiRequest).mockResolvedValueOnce({
      network: {
        ...network,
        config: { cidr: '10.1.2.0/24', dhcp: true, dhcp_server: '10.1.2.254', dhcp_start: '10.1.2.128', dhcp_end: '10.1.2.253' },
        runtime: { dhcp: true, dhcp_pid: 42 },
      },
      revision: 62,
    } as never);
    await openNetworkMenu();
    // The menu repeats the modal's warning about a second DHCP server.
    expect(screen.getByTestId('network-menu-dhcp')).toHaveAttribute(
      'title', 'Start a DHCP server on this network. Turn off DHCP on any node attached here, such as a lab router.'
    );
    await fireEvent.click(screen.getByTestId('network-menu-dhcp'));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'DHCP enabled; server running. Turn off DHCP on any node attached here, such as a lab router.', 'success', 8000
    ));
    expect(get(labState).networks['7'].config?.dhcp).toBe(true);
    toast.mockRestore();
  });

  it('configures a bridge from the menu with PATCH (#325)', async () => {
    const network = makeSharingNetwork();
    renderCanvas({}, { '7': network });
    await openNetworkMenu();
    await fireEvent.click(screen.getByTestId('network-menu-configure'));
    expect(screen.getByRole('dialog', { name: 'Configure bridge' })).toBeVisible();
    await fireEvent.input(screen.getByLabelText('CIDR'), { target: { value: '10.1.2.0/24' } });
    vi.mocked(apiRequest).mockClear();
    vi.mocked(apiRequest)
      .mockResolvedValueOnce({ data: {} } as never)
      .mockResolvedValueOnce({ network: { ...network, config: { cidr: '10.1.2.0/24' } }, revision: 63 } as never);
    await fireEvent.click(screen.getByTestId('network-confirm-button'));

    await waitFor(() => expect(screen.queryByTestId('network-config-modal')).toBeNull());
    expect(apiRequest).toHaveBeenCalledWith('/labs/lab-hotplug/networks/validate-config', {
      method: 'POST', body: { type: 'linux_bridge', config: { cidr: '10.1.2.0/24' }, network_id: 7 }, suppressToast: true,
    });
    expect(apiRequest).toHaveBeenLastCalledWith('/labs/lab-hotplug/networks/7', {
      method: 'PATCH', body: { config: { cidr: '10.1.2.0/24' } }, suppressToast: true,
    });
    expect(get(labState).networks['7'].config?.cidr).toBe('10.1.2.0/24');
    await openNetworkMenu();
    expect(screen.getByTestId('network-menu-dhcp')).toHaveTextContent('Enable DHCP');
  });

  it('leaves Zen shortcuts available and preserves a pending link', async () => {
    const view = renderCanvas({
      '1': makeNode(1, 'edge-a', 0, capabilities, 2),
      '2': makeNode(2, 'edge-b', 0, capabilities, 2),
    });
    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), nodeSlotEndpoint(2, 'edge-b'));
    const dialog = await screen.findByTestId('link-confirm-modal');
    const source = screen.getByTestId('link-source-interface-select');
    await fireEvent.change(source, { target: { value: '1' } });
    const style = screen.getByRole('radio', { name: 'bezier' });
    await fireEvent.click(style);

    await view.rerender({ zenMode: true });
    for (const key of ['Enter', ' ', 'Escape']) {
      const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
      await fireEvent(document.body, event);
      expect(event.defaultPrevented).toBe(false);
    }
    expect(dialog).not.toBeVisible();
    expect(get(dragLinkStore).state).toBe('confirming');
    expect(get(labState).links).toHaveLength(0);

    await view.rerender({ zenMode: false });
    expect(dialog).toBeVisible();
    expect(source).toHaveValue('1');
    expect(style).toBeChecked();
  });

  it('leaves Zen shortcuts available and preserves a network draft', async () => {
    const view = renderCanvas({});
    await fireEvent.click(screen.getByRole('button', { name: 'Open add element menu' }));
    await fireEvent.click(await screen.findByRole('button', { name: /network container type/i }));
    await fireEvent.click(screen.getByRole('button', { name: /bridge network/i }));
    const dialog = screen.getByTestId('network-config-modal');
    const name = screen.getByTestId('network-name-input');
    await fireEvent.input(name, { target: { value: 'bridge-draft' } });

    await view.rerender({ zenMode: true });
    for (const key of ['Enter', ' ', 'Escape']) {
      const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
      await fireEvent(document.body, event);
      expect(event.defaultPrevented).toBe(false);
    }
    expect(dialog).not.toBeVisible();
    expect(apiMock).not.toHaveBeenCalledWith('/labs/lab-hotplug/networks', expect.anything());

    await view.rerender({ zenMode: false });
    expect(dialog).toBeVisible();
    expect(name).toHaveValue('bridge-draft');
  });
});

describe('TopologyCanvas hotplug gate', () => {
  it('allows link confirm between boot interfaces of running non-hotplug nodes', async () => {
    renderCanvas({
      '1': makeNode(1, 'edge-a', 2, { hotplug: false, max_nics: 8, machine: 'pc' }),
      '2': makeNode(2, 'edge-b', 2, { hotplug: false, max_nics: 8, machine: 'pc' }),
    });

    driveConfirm(interfaceEndpoint(1, 'eth0'), interfaceEndpoint(2, 'eth0'));

    await waitFor(() => {
      expect(screen.getByTestId('link-confirm-modal')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('topology-hotplug-banner')).toBeNull();
    expect(get(dragLinkStore).state).toBe('confirming');
  });

  it('opens the interface picker for running non-hotplug nodes from the new-connection slot', async () => {
    // Unused interfaces are only reachable via the node slot (ports render
    // only for linked interfaces) and the picker only offers existing boot
    // NICs — a host-only attach that works live on any machine type.
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 2, { hotplug: false, max_nics: 8, machine: 'pc' }, 2),
      },
      {
        '9': makeNetwork(9, 'bridge-a'),
      }
    );

    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), networkEndpoint(9, 'bridge-a'));

    await waitFor(() => {
      expect(screen.getByTestId('link-source-interface-select')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('topology-hotplug-banner')).toBeNull();
    expect(get(dragLinkStore).state).toBe('confirming');
  });

  it('gates link confirm for running IOL nodes even on boot interfaces', async () => {
    const iolNode = {
      ...makeNode(4, 'iol-a', 2, { hotplug: false, max_nics: 8, machine: null }),
      type: 'iol' as const,
      template: 'iol',
    };
    renderCanvas(
      { '4': iolNode },
      { '9': makeNetwork(9, 'bridge-a') }
    );

    driveConfirm(interfaceEndpoint(4, 'eth0'), networkEndpoint(9, 'bridge-a'));

    const banner = await screen.findByTestId('topology-hotplug-banner');
    expect(banner).toHaveTextContent('Restart required to change topology');
    expect(banner).toHaveTextContent('iol-a');
    expect(screen.queryByTestId('link-confirm-modal')).toBeNull();
    expect(get(dragLinkStore).state).toBe('idle');
  });

  it('allows link confirm for a stopped non-hotplug node', async () => {
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 0, { hotplug: false, max_nics: 8, machine: 'pc' }),
      },
      {
        '9': makeNetwork(9, 'bridge-a'),
      }
    );

    driveConfirm(interfaceEndpoint(1, 'eth0'), networkEndpoint(9, 'bridge-a'));

    await waitFor(() => {
      expect(screen.getByTestId('link-confirm-modal')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('topology-hotplug-banner')).toBeNull();
    expect(get(dragLinkStore).state).toBe('confirming');
  });

  it('allows link confirm for a running hotplug-capable node', async () => {
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 2, { hotplug: true, max_nics: 8, machine: 'q35' }),
      },
      {
        '9': makeNetwork(9, 'bridge-a'),
      }
    );

    driveConfirm(interfaceEndpoint(1, 'eth0'), networkEndpoint(9, 'bridge-a'));

    await waitFor(() => {
      expect(screen.getByTestId('link-confirm-modal')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('topology-hotplug-banner')).toBeNull();
    expect(get(dragLinkStore).state).toBe('confirming');
  });
});

describe('TopologyCanvas node-slot link confirmation', () => {
  it('asks for source and target interfaces for node-to-node new-slot links', async () => {
    renderCanvas({
      '1': makeNode(1, 'edge-a', 0, { hotplug: true, max_nics: 8, machine: 'q35' }, 2),
      '2': makeNode(2, 'edge-b', 0, { hotplug: true, max_nics: 8, machine: 'q35' }, 2),
    });

    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), nodeSlotEndpoint(2, 'edge-b'));

    expect(await screen.findByTestId('link-source-interface-select')).toBeInTheDocument();
    expect(screen.getByTestId('link-target-interface-select')).toBeInTheDocument();
  });

  it('asks only for the node-side source interface for node-to-network new-slot links', async () => {
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 0, { hotplug: true, max_nics: 8, machine: 'q35' }, 2),
      },
      {
        '9': makeNetwork(9, 'bridge-a'),
      }
    );

    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), networkEndpoint(9, 'bridge-a'));

    expect(await screen.findByTestId('link-source-interface-select')).toBeInTheDocument();
    expect(screen.queryByTestId('link-target-interface-select')).toBeNull();
  });

  it('asks only for the node-side target interface for network-to-node new-slot links', async () => {
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 0, { hotplug: true, max_nics: 8, machine: 'q35' }, 2),
      },
      {
        '9': makeNetwork(9, 'bridge-a'),
      }
    );

    driveConfirm(networkEndpoint(9, 'bridge-a'), nodeSlotEndpoint(1, 'edge-a'));

    expect(await screen.findByTestId('link-target-interface-select')).toBeInTheDocument();
    expect(screen.queryByTestId('link-source-interface-select')).toBeNull();
  });

  it('posts the selected unconnected interfaces and omits already connected interfaces', async () => {
    const apiMock = vi.mocked(apiRequest);
    apiMock.mockClear();
    const existingLink: Link = {
      id: 'lnk_001',
      from: { node_id: 1, interface_index: 0 },
      to: { network_id: 9 },
      style_override: null,
      label: '',
      color: '',
      width: '1',
    };
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 0, { hotplug: true, max_nics: 8, machine: 'q35' }, 2),
      },
      {
        '9': makeNetwork(9, 'bridge-a'),
      },
      [existingLink]
    );

    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), networkEndpoint(9, 'bridge-a'));

    const select = (await screen.findByTestId('link-source-interface-select')) as HTMLSelectElement;
    expect([...select.options].map((option) => option.value)).toEqual(['1']);
    screen.getByTestId('link-confirm-button').click();

    await waitFor(() => {
      expect(apiMock).toHaveBeenCalledWith(
        '/labs/lab-hotplug/links',
        expect.objectContaining({
          method: 'POST',
          body: expect.objectContaining({
            from: { node_id: 1, interface_index: 1 },
            to: { network_id: 9 },
          }),
        })
      );
    });
  });
});

describe('TopologyCanvas minimap collapse (#273)', () => {
  beforeEach(() => {
    window.localStorage.removeItem('nova-ve.canvas-prefs');
  });

  it('renders collapsed by default with the floating toggle box', async () => {
    renderCanvas({
      '1': makeNode(1, 'edge-a', 0, { hotplug: false, max_nics: 8, machine: 'pc' }),
    });

    const toggle = screen.getByTestId('minimap-toggle');
    expect(toggle.getAttribute('aria-label')).toBe('Show minimap');
    expect(screen.queryByTestId('minimap-frame')).toBeNull();
  });

  it('swaps the minimap into the corner and minimizes back', async () => {
    renderCanvas({
      '1': makeNode(1, 'edge-a', 0, { hotplug: false, max_nics: 8, machine: 'pc' }),
    });

    // Expanding replaces the toggle box with the minimap itself — the
    // minimap carries its own minimize control.
    await fireEvent.click(screen.getByTestId('minimap-toggle'));
    await tick();
    expect(screen.getByTestId('minimap-frame')).toBeInTheDocument();
    expect(screen.queryByTestId('minimap-toggle')).toBeNull();
    const minimize = screen.getByTestId('minimap-minimize');
    expect(minimize.getAttribute('aria-label')).toBe('Hide minimap');
    expect(JSON.parse(window.localStorage.getItem('nova-ve.canvas-prefs') ?? '{}')).toEqual({
      minimapExpanded: true
    });

    await fireEvent.click(minimize);
    await tick();
    expect(screen.queryByTestId('minimap-frame')).toBeNull();
    expect(screen.getByTestId('minimap-toggle').getAttribute('aria-label')).toBe('Show minimap');
    expect(JSON.parse(window.localStorage.getItem('nova-ve.canvas-prefs') ?? '{}')).toEqual({
      minimapExpanded: false
    });
  });

  it('restores the persisted expanded choice on load', async () => {
    window.localStorage.setItem(
      'nova-ve.canvas-prefs',
      JSON.stringify({ minimapExpanded: true })
    );
    // Simulate a fresh module load: the store reads storage at creation.
    vi.resetModules();
    const { canvasPrefs: freshStore } = await import('$lib/stores/canvasPrefs');
    expect(get(freshStore).minimapExpanded).toBe(true);
  });
});

describe('TopologyCanvas hover focus (#275)', () => {
  const caps = { hotplug: false, max_nics: 8, machine: 'pc' } as TemplateCapabilities;

  it('pulses only the outer card from white to half-brightness over two seconds', () => {
    const source = readFileSync(
      resolve(process.cwd(), 'src/lib/components/canvas/TopologyCanvas.svelte'),
      'utf8'
    );
    const pulseStyles = source.slice(
      source.indexOf('@keyframes nv-focus-pulse'),
      source.indexOf('.canvas-minimap svg', source.indexOf('@keyframes nv-focus-pulse'))
    );

    expect(pulseStyles).toContain('outline-color: #c0c0c0');
    expect(pulseStyles).toContain('outline-color: #606060');
    expect(pulseStyles).toContain('animation: nv-focus-pulse 2s ease-in-out infinite');
    expect(pulseStyles).not.toContain("[data-testid='node-icon-container']");
  });

  function link(id: string, nodeId: number, networkId: number): Link {
    return {
      id,
      from: { node_id: nodeId, interface_index: 0 },
      to: { network_id: networkId },
      style_override: null,
      label: '',
      color: '',
      width: '1',
    };
  }

  function flowSummary(kind: 'nodes' | 'edges'): Array<{ id: string; class: string | null }> {
    const flow = screen.getByTestId('svelte-flow-mock');
    return JSON.parse(flow.getAttribute(`data-flow-${kind}`) ?? '[]');
  }

  function pulsing(kind: 'nodes' | 'edges', id: string): boolean {
    return flowSummary(kind).find((item) => item.id === id)?.class?.includes('nv-focus-pulse') ?? false;
  }

  function classOf(kind: 'nodes' | 'edges', id: string): string | null {
    return flowSummary(kind).find((item) => item.id === id)?.class ?? null;
  }

  function hoverNode(id: string) {
    screen.getByTestId('svelte-flow-mock').dispatchEvent(
      new CustomEvent('mock-flow-event', { detail: { type: 'nodemouseenter', detail: { node: { id } } } })
    );
  }

  function leaveNode() {
    screen.getByTestId('svelte-flow-mock').dispatchEvent(
      new CustomEvent('mock-flow-event', { detail: { type: 'nodemouseleave', detail: {} } })
    );
  }

  it('pulses the hovered node and its connected nodes; everything else is untouched', async () => {
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 0, caps),
        '2': makeNode(2, 'edge-b', 0, caps),
        '3': makeNode(3, 'edge-c', 0, caps),
      },
      { '9': makeNetwork(9, 'bridge-a'), '8': makeNetwork(8, 'bridge-b') },
      [link('l1', 1, 9), link('l2', 2, 9), link('l3', 3, 8)]
    );

    hoverNode('node1');
    await tick();
    await tick();

    // Hovered node + network broker + peer pulse (both endpoint nodes of
    // the node → network → node path).
    expect(pulsing('nodes', 'node1')).toBe(true);
    expect(pulsing('nodes', 'network9')).toBe(true);
    expect(pulsing('nodes', 'node2')).toBe(true);
    // The unrelated island receives no focus class at all.
    expect(classOf('nodes', 'node3')).toBeNull();
    expect(classOf('nodes', 'network8')).toBeNull();
    // Edges are never touched by the revision.
    expect(classOf('edges', 'link:l1')).toBeNull();
    expect(classOf('edges', 'link:l2')).toBeNull();
    expect(classOf('edges', 'link:l3')).toBeNull();

    leaveNode();
    await tick();
    await tick();
    // The clear is debounced by a short grace period (rapid node crossings
    // cancel it); settle past it before asserting the pulse lifted.
    await waitFor(
      () => {
        expect(flowSummary('nodes').every((item) => !item.class?.includes('nv-focus-pulse'))).toBe(true);
      },
      { timeout: 1500 }
    );
  });

  it('does not drop the pulse while the pointer sweeps from one node to another (#275 flicker)', async () => {
    renderCanvas(
      {
        '1': makeNode(1, 'edge-a', 0, caps),
        '2': makeNode(2, 'edge-b', 0, caps),
        '3': makeNode(3, 'edge-c', 0, caps),
      },
      { '9': makeNetwork(9, 'bridge-a'), '8': makeNetwork(8, 'bridge-b') },
      [link('l1', 1, 9), link('l2', 2, 9), link('l3', 3, 8)]
    );

    hoverNode('node1');
    await tick();
    await tick();
    expect(pulsing('nodes', 'node1')).toBe(true);

    // Rapid crossing: leave fires BEFORE the next mouseenter — the debounced
    // clear must not drop the highlight in between.
    leaveNode();
    hoverNode('node2');
    await tick();
    await tick();

    // node2's neighborhood applies immediately: node1/network9 pulse via
    // the shared bridge, node3's island stays untouched.
    expect(pulsing('nodes', 'node1')).toBe(true);
    expect(pulsing('nodes', 'network9')).toBe(true);
    expect(pulsing('nodes', 'node2')).toBe(true);
    expect(pulsing('nodes', 'node3')).toBe(false);

    // The pending clear was cancelled by the enter: past the grace period
    // the node2 focus is still applied (no late wipe).
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(pulsing('nodes', 'node2')).toBe(true);
    expect(pulsing('nodes', 'node3')).toBe(false);
  });

  it('pulses an isolated hovered node alone', async () => {
    renderCanvas(
      { '1': makeNode(1, 'solo', 0, caps), '2': makeNode(2, 'edge-b', 0, caps) },
      { '8': makeNetwork(8, 'bridge-b') },
      [link('l3', 2, 8)]
    );

    hoverNode('node1');
    await tick();
    await tick();

    expect(pulsing('nodes', 'node1')).toBe(true);
    expect(pulsing('nodes', 'node2')).toBe(false);
    expect(classOf('edges', 'link:l3')).toBeNull();
  });
});

// #306: the canvas read the created link from ``response.data``, but
// POST /labs/{id}/links returns it as ``link`` (and an implicit bridge as
// ``network``), so the optimistic placeholder was never replaced and the
// WebSocket's copy of the same link drew beside it.
describe('TopologyCanvas link creation adopts the create response (#306)', () => {
  const defaultApi = vi.mocked(apiRequest).getMockImplementation()!;

  function link(id: string, from: Link['from'], to: Link['to']): Link {
    return { id, from, to, style_override: null, label: '', color: '', width: '1' };
  }

  const edgeIds = () =>
    (
      JSON.parse(screen.getByTestId('svelte-flow-mock').getAttribute('data-flow-edges') ?? '[]') as Array<{
        id: string;
      }>
    ).map((edge) => edge.id);
  const linkIds = () => get(labState).links.map((entry) => entry.id).sort();

  /**
   * Serve POST /links with the endpoint's real response shape.
   * ``beforeResponding`` stands in for the WebSocket events the backend
   * publishes before the HTTP response goes out.
   */
  function onCreate(
    respond: (body: { from: Link['from']; to: Link['to'] }) => unknown,
    beforeResponding?: () => void
  ) {
    vi.mocked(apiRequest).mockImplementation(async (path: string, options) => {
      if (path === '/labs/lab-hotplug/links') {
        beforeResponding?.();
        return respond((options as { body: { from: Link['from']; to: Link['to'] } }).body) as never;
      }
      return defaultApi(path, options);
    });
  }

  const hotplug = { hotplug: true, max_nics: 8, machine: 'q35' as const };
  const bridge: NetworkData = {
    id: 21,
    name: 'Net 21',
    type: 'bridge',
    left: 0,
    top: 0,
    icon: 'Cloud.png',
    visibility: false,
    implicit: true,
    count: 2
  };

  afterEach(() => {
    vi.mocked(apiRequest).mockImplementation(defaultApi);
  });

  it('draws one link when the WebSocket and the response both announce a node-to-network link', async () => {
    onCreate((body) => {
      const created = link('lnk_101', body.from, body.to);
      labState.upsertLink(created);
      return {
        code: 201,
        status: 'success',
        message: 'Link created successfully.',
        link: created,
        revision: 5
      };
    });
    renderCanvas(
      { '1': makeNode(1, 'edge-a', 0, hotplug, 2) },
      { '9': makeNetwork(9, 'bridge-a') }
    );

    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), networkEndpoint(9, 'bridge-a'));
    (await screen.findByTestId('link-confirm-button')).click();

    await waitFor(() => expect(edgeIds()).toEqual(['link:lnk_101']));
    expect(linkIds()).toEqual(['lnk_101']);
    // The dialog's default choice is the first free interface.
    expect(get(labState).nodes['1'].interfaces[0].network_id).toBe(9);
  });

  it('collapses a node-to-node link into one edge once the bridge and both halves are known', async () => {
    onCreate((body) => {
      const first = link('lnk_201', body.from, { network_id: 21 });
      const second = link('lnk_202', body.to, { network_id: 21 });
      labState.upsertLink(first);
      labState.upsertLink(second);
      return {
        code: 201,
        status: 'success',
        message: 'Link created successfully.',
        link: first,
        network: bridge,
        revision: 6
      };
    });
    renderCanvas({
      '1': makeNode(1, 'edge-a', 0, hotplug, 2),
      '2': makeNode(2, 'edge-b', 0, hotplug, 2)
    });

    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), nodeSlotEndpoint(2, 'edge-b'));
    (await screen.findByTestId('link-confirm-button')).click();

    await waitFor(() => expect(edgeIds()).toEqual(['pair:net21']));
    expect(linkIds()).toEqual(['lnk_201', 'lnk_202']);
    expect(get(labState).networks['21']?.implicit).toBe(true);
  });

  it('asks for a lab reload instead of guessing when the response beats the peer half', async () => {
    onCreate((body) => ({
      code: 201,
      status: 'success',
      message: 'Link created successfully.',
      link: link('lnk_201', body.from, { network_id: 21 }),
      network: bridge,
      revision: 6
    }));
    const reasons: string[] = [];
    labState.reset();
    labState.applySnapshot({
      nodes: { '1': makeNode(1, 'edge-a', 0, hotplug, 2), '2': makeNode(2, 'edge-b', 0, hotplug, 2) },
      networks: {},
      topology: [],
      links: [],
      defaults: { link_style: 'orthogonal' }
    });
    render(CanvasChangedHarness, { props: { labId: 'lab-hotplug', reasons } });

    driveConfirm(nodeSlotEndpoint(1, 'edge-a'), nodeSlotEndpoint(2, 'edge-b'));
    (await screen.findByTestId('link-confirm-button')).click();

    await waitFor(() => expect(reasons).toContain('link-create-reload'));
    // The optimistic line stays until the snapshot lands, so the link does
    // not blink out while the lab reloads.
    expect(edgeIds()).toEqual([expect.stringMatching(/^link:tmp_/)]);
  });
});
