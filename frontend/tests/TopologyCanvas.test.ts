// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
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
import { apiRequest } from '$lib/api';
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

  it('hides the canvas controls in Zen mode while keeping the same topology mounted', async () => {
    const view = renderCanvas({ '1': makeNode(1, 'edge-a', 0, capabilities) });
    selectedPortInfoStore.set({
      kind: 'interface', nodeId: 1, interfaceIndex: 0, anchorRect: null,
      interfaceName: 'eth0', plannedMac: null,
    });
    const topology = screen.getByTestId('svelte-flow-mock');
    const nodeSummary = topology.getAttribute('data-flow-nodes');
    expect(screen.getByRole('group', { name: 'Lab controls' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'zoom in' })).toBeInTheDocument();
    expect(await screen.findByRole('dialog', { name: 'Port info for eth0' })).toBeVisible();

    await view.rerender({ zenMode: true });
    expect(screen.queryByRole('group', { name: 'Lab controls' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'zoom in' })).toBeNull();
    expect(screen.queryByTestId('port-info-popover')).toBeNull();
    expect(screen.getByTestId('svelte-flow-mock')).toBe(topology);
    expect(topology).toHaveAttribute('data-flow-nodes', nodeSummary);

    await view.rerender({ zenMode: false });
    expect(screen.getByRole('group', { name: 'Lab controls' })).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Port info for eth0' })).toBeVisible();
    expect(screen.getByTestId('svelte-flow-mock')).toBe(topology);
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
