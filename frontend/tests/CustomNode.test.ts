// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';

vi.mock('@xyflow/svelte', async () => {
  const LeafStub = (await import('./mocks/LeafStub.svelte')).default;
  return {
    Handle: LeafStub,
    Position: {
      Left: 'left',
      Right: 'right',
    },
  };
});

import CustomNode from '$lib/components/canvas/CustomNode.svelte';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CustomNode', () => {
  it('shows hostname only on the node face without template metadata', () => {
    render(CustomNode, {
      props: {
        data: {
          label: 'edge-router-core-01',
          status: 2,
          type: 'qemu',
          template: 'vyos',
          console: 'telnet',
          nodeId: 1,
          interfaces: [],
        },
      },
    });

    expect(screen.getByText('edge-router-core-01')).toBeInTheDocument();
    expect(screen.queryByText('vyos')).toBeNull();
    expect(screen.queryByText('QEMU')).toBeNull();
    expect(screen.queryByText('running')).toBeNull();
    expect(screen.queryByTestId('node-status-indicator')).toBeNull();
  });

  it('renders connected interfaces plus one dotted new-connection handle', () => {
    render(CustomNode, {
      props: {
        data: {
          label: 'edge-router-core-01',
          status: 2,
          nodeId: 1,
          connectedInterfaceIndexes: [1],
          interfaces: [
            { index: 0, name: 'eth0', network_id: 0 },
            { index: 1, name: 'eth1', network_id: 9 },
            { index: 2, name: 'eth2', network_id: 0 },
          ],
        },
      },
    });

    expect(screen.getAllByTestId('port-handle')).toHaveLength(1);
    expect(screen.getAllByTestId('port-new-connection-handle')).toHaveLength(1);
    expect(screen.getByText('eth1')).toBeInTheDocument();
    expect(screen.queryByText('eth0')).toBeNull();
    expect(screen.queryByText('eth2')).toBeNull();
  });

  it('binds connected node handles to their persisted link endpoint', () => {
    render(CustomNode, {
      props: {
        data: {
          label: 'edge-router-core-01',
          status: 2,
          nodeId: 1,
          connectedInterfaceIndexes: [1],
          portRefsByInterfaceIndex: {
            '1': { linkId: 'lnk_004', endpointKey: 'from' },
          },
          interfaces: [
            { index: 1, name: 'eth1', network_id: 9 },
          ],
        },
      },
    });

    const portWrapper = screen.getByTestId('port-handle').closest('[data-port-link-id]');
    expect(portWrapper).toHaveAttribute('data-port-link-id', 'lnk_004');
    expect(portWrapper).toHaveAttribute('data-port-endpoint-key', 'from');
  });

  it('hides the dotted new-connection handle when no unused interface remains', () => {
    render(CustomNode, {
      props: {
        data: {
          label: 'edge-router-core-01',
          status: 2,
          nodeId: 1,
          connectedInterfaceIndexes: [0, 1],
          interfaces: [
            { index: 0, name: 'eth0', network_id: 9 },
            { index: 1, name: 'eth1', network_id: 10 },
          ],
        },
      },
    });

    expect(screen.getAllByTestId('port-handle')).toHaveLength(2);
    expect(screen.queryByTestId('port-new-connection-handle')).toBeNull();
  });

  it('shows ctrl-drag preview for connected interfaces whose real index differs from array order', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      x: 0,
      y: 0,
      left: 0,
      top: 0,
      right: 100,
      bottom: 100,
      width: 100,
      height: 100,
      toJSON: () => ({}),
    } as DOMRect);

    render(CustomNode, {
      props: {
        data: {
          label: 'pa-vm-01',
          status: 2,
          nodeId: 1,
          connectedInterfaceIndexes: [5],
          interfaces: [
            { index: 5, name: 'ethernet1/5', network_id: 9, port_position: { side: 'right', offset: 0.5 } },
          ],
        },
      },
    });

    const portWrapper = document.querySelector(
      '[data-port-node-id="1"][data-port-interface-index="5"]'
    ) as HTMLElement | null;
    expect(portWrapper).not.toBeNull();
    expect(portWrapper).toHaveAttribute('data-port-side', 'right');

    portWrapper?.dispatchEvent(
      new MouseEvent('mousedown', {
        button: 0,
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
        clientX: 100,
        clientY: 50,
      })
    );
    await tick();
    window.dispatchEvent(
      new MouseEvent('mousemove', {
        bubbles: true,
        cancelable: true,
        clientX: 50,
        clientY: -5,
      })
    );
    await tick();

    const movedPortWrapper = document.querySelector(
      '[data-port-node-id="1"][data-port-interface-index="5"]'
    ) as HTMLElement | null;
    expect(movedPortWrapper).toHaveAttribute('data-port-side', 'top');
    expect(movedPortWrapper?.getAttribute('data-port-offset')).toBe('0.5000');

    window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
  });
});
