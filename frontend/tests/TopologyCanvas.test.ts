// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { cleanup, render, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import type { DragEndpoint, InterfaceEndpoint, NetworkEndpoint } from '$lib/stores/dragLink';
import { dragLinkStore } from '$lib/stores/dragLink';
import type { NetworkData, NodeCatalog, NodeData, TemplateCapabilities } from '$lib/types';

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

vi.mock('$lib/services/labApi', () => ({
  createLayoutDebouncer: () => ({
    pushNodePosition: vi.fn(),
    pushNetworkPosition: vi.fn(),
    pushViewport: vi.fn(),
    flush: vi.fn(async () => {}),
  }),
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
    divergentLinks: writable({}),
    connected: writable(false),
  }),
}));

import TopologyCanvas from '$lib/components/canvas/TopologyCanvas.svelte';

function makeNode(
  id: number,
  name: string,
  status: 0 | 2,
  capabilities: TemplateCapabilities
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
    interfaces: [
      {
        index: 0,
        name: 'eth0',
        network_id: 0,
        planned_mac: null,
        port_position: { side: 'right', offset: 0.5 },
      },
    ],
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

function renderCanvas(nodes: Record<string, NodeData>, networks: Record<string, NetworkData> = {}) {
  return render(TopologyCanvas, {
    props: {
      labId: 'lab-hotplug',
      nodes,
      networks,
      topology: [],
      links: [],
      defaults: { link_style: 'orthogonal' },
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
  cleanup();
});

describe('TopologyCanvas hotplug gate', () => {
  it('gates link confirm for running non-hotplug nodes and shows the canvas banner', async () => {
    renderCanvas({
      '1': makeNode(1, 'edge-a', 2, { hotplug: false, max_nics: 8, machine: 'pc' }),
      '2': makeNode(2, 'edge-b', 2, { hotplug: false, max_nics: 8, machine: 'pc' }),
    });

    driveConfirm(interfaceEndpoint(1, 'eth0'), interfaceEndpoint(2, 'eth0'));

    const banner = await screen.findByTestId('topology-hotplug-banner');
    expect(banner).toHaveTextContent('Restart required to change topology');
    expect(banner).toHaveTextContent('edge-a');
    expect(banner).toHaveTextContent('edge-b');
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
