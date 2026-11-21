// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import type { DragEndpoint, InterfaceEndpoint, NetworkEndpoint, NodeSlotEndpoint } from '$lib/stores/dragLink';
import { dragLinkStore } from '$lib/stores/dragLink';
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
import { get } from 'svelte/store';
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
  labState.reset();
  cleanup();
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

  it('expands the minimap on click and collapses again', async () => {
    renderCanvas({
      '1': makeNode(1, 'edge-a', 0, { hotplug: false, max_nics: 8, machine: 'pc' }),
    });

    await fireEvent.click(screen.getByTestId('minimap-toggle'));
    await tick();
    expect(screen.getByTestId('minimap-frame')).toBeInTheDocument();
    expect(screen.getByTestId('minimap-toggle').getAttribute('aria-label')).toBe('Hide minimap');
    expect(JSON.parse(window.localStorage.getItem('nova-ve.canvas-prefs') ?? '{}')).toEqual({
      minimapExpanded: true
    });

    await fireEvent.click(screen.getByTestId('minimap-toggle'));
    await tick();
    expect(screen.queryByTestId('minimap-frame')).toBeNull();
    expect(screen.getByTestId('minimap-toggle').getAttribute('aria-label')).toBe('Show minimap');
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

  function faded(kind: 'nodes' | 'edges', id: string): boolean {
    return flowSummary(kind).find((item) => item.id === id)?.class?.includes('nv-focus-faded') ?? false;
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

  it('keeps the hovered node, its links and neighbors lit; fades the rest', async () => {
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

    // Hovered node + network broker + peer stay lit (both endpoint nodes
    // and both links of the node → network → node path).
    expect(faded('nodes', 'node1')).toBe(false);
    expect(faded('nodes', 'network9')).toBe(false);
    expect(faded('nodes', 'node2')).toBe(false);
    expect(faded('edges', 'link:l1')).toBe(false);
    expect(faded('edges', 'link:l2')).toBe(false);
    // The unrelated island fades.
    expect(faded('nodes', 'node3')).toBe(true);
    expect(faded('nodes', 'network8')).toBe(true);
    expect(faded('edges', 'link:l3')).toBe(true);

    leaveNode();
    await tick();
    await tick();
    expect(flowSummary('nodes').every((item) => !item.class?.includes('nv-focus-faded'))).toBe(true);
    expect(flowSummary('edges').every((item) => !item.class?.includes('nv-focus-faded'))).toBe(true);
  });

  it('lights an isolated hovered node alone', async () => {
    renderCanvas(
      { '1': makeNode(1, 'solo', 0, caps), '2': makeNode(2, 'edge-b', 0, caps) },
      { '8': makeNetwork(8, 'bridge-b') },
      [link('l3', 2, 8)]
    );

    hoverNode('node1');
    await tick();
    await tick();

    expect(faded('nodes', 'node1')).toBe(false);
    expect(faded('nodes', 'node2')).toBe(true);
    expect(faded('edges', 'link:l3')).toBe(true);
  });
});
