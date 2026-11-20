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

  it('places the dotted new-connection handle at the canvas-nudged position (#258)', () => {
    const { container } = render(CustomNode, {
      props: {
        data: {
          label: 'edge-router-core-01',
          status: 2,
          nodeId: 1,
          connectedInterfaceIndexes: [1],
          newConnectionPortPosition: { side: 'left', offset: 0.25 },
          interfaces: [
            { index: 0, name: 'eth0', network_id: 0 },
            { index: 1, name: 'eth1', network_id: 9 },
          ],
        },
      },
    });

    const slot = container.querySelector('[data-port-new-connection="true"]');
    expect(slot).not.toBeNull();
    expect(slot!.getAttribute('data-port-side')).toBe('left');
    expect(slot!.getAttribute('data-port-offset')).toBe('0.2500');
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

  it('ignores the legacy persisted port_position — auto-positioning only (#258)', async () => {
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
          interfaces: [
            // Stale manual placement from the removed ctrl+drag feature must
            // not be honored; unconnected ports use the canonical default.
            { index: 0, name: 'eth0', network_id: 0, port_position: { side: 'bottom', offset: 0.9 } },
          ],
        },
      },
    });

    const portWrapper = document.querySelector(
      '[data-port-node-id="1"][data-port-interface-index="0"]'
    ) as HTMLElement | null;
    expect(portWrapper).not.toBeNull();
    // placeInterfaces(1) → t=0.5, the start of the "bottom" quadrant.
    expect(portWrapper).toHaveAttribute('data-port-side', 'bottom');
    expect(portWrapper?.getAttribute('data-port-offset')).toBe('0.0000');

    // A full ctrl+drag gesture must not persist any placement: the port may
    // follow the cursor as a link-drag preview, but settles back to the
    // canonical auto position once the gesture ends.
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
    window.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
    await tick();

    const settled = document.querySelector(
      '[data-port-node-id="1"][data-port-interface-index="0"]'
    ) as HTMLElement | null;
    expect(settled).toHaveAttribute('data-port-side', 'bottom');
    expect(settled?.getAttribute('data-port-offset')).toBe('0.0000');
  });
});

describe('CustomNode netbird-style card visuals (#254)', () => {
  it('renders the flat neutral card with status dot and quiet secondary line', () => {
    render(CustomNode, {
      props: {
        data: {
          label: 'edge-router-core-01',
          status: 2,
          type: 'qemu',
          console: 'telnet',
          nodeId: 1,
          interfaces: [
            { index: 0, name: 'eth0', network_id: 0 },
            { index: 1, name: 'eth1', network_id: 0 },
          ],
        },
      },
    });

    // Neutral card surface (not the old status-colored borders).
    const card = screen.getByTestId('custom-node-card');
    expect(card).toHaveClass('bg-[var(--card-canvas-bg)]');
    expect(card).not.toHaveClass('border-emerald-500/80');

    // Status is a dot, never text, and never the legacy indicator testid.
    const dot = screen.getByTestId('node-status-dot');
    expect(dot).toHaveClass('bg-emerald-400');
    expect(screen.queryByTestId('node-status-indicator')).toBeNull();

    // Secondary line: NIC count + console, no template/type metadata.
    expect(screen.getByText('2 NICs · telnet')).toBeInTheDocument();
    expect(screen.queryByText('vyos')).toBeNull();
    expect(screen.queryByText('qemu')).toBeNull();
  });

  it('renders an amber pulsing dot while transitioning and gray when stopped', () => {
    const stopped = render(CustomNode, {
      props: { data: { label: 'n1', status: 0, nodeId: 1, interfaces: [] } },
    });
    expect(screen.getByTestId('node-status-dot')).toHaveClass('bg-gray-500');
    stopped.unmount();

    render(CustomNode, {
      props: {
        data: { label: 'n1', status: 0, transientStatus: 'starting', nodeId: 1, interfaces: [] },
      },
    });
    expect(screen.getByTestId('node-status-dot')).toHaveClass('bg-amber-300', 'animate-pulse');
  });
});

describe('CustomNode icon-container run-state outline (#274)', () => {
  it('rings the icon container green while running', () => {
    render(CustomNode, {
      props: { data: { label: 'n1', status: 2, nodeId: 1, interfaces: [] } },
    });

    const icon = screen.getByTestId('node-icon-container');
    expect(icon).toHaveClass('shadow-[0_0_0_2px_#34d399]');
    expect(icon).not.toHaveClass('shadow-[0_0_0_2px_#6b7280]');
  });

  it('rings grey when stopped and amber while transitioning', () => {
    const stopped = render(CustomNode, {
      props: { data: { label: 'n1', status: 0, nodeId: 1, interfaces: [] } },
    });
    expect(screen.getByTestId('node-icon-container')).toHaveClass('shadow-[0_0_0_2px_#6b7280]');
    stopped.unmount();

    render(CustomNode, {
      props: {
        data: { label: 'n1', status: 0, transientStatus: 'stopping', nodeId: 1, interfaces: [] },
      },
    });
    expect(screen.getByTestId('node-icon-container')).toHaveClass('shadow-[0_0_0_2px_#fcd34d]');
  });

  it('keeps the outline off the card border — no layout-bearing status classes on the card', () => {
    render(CustomNode, {
      props: { data: { label: 'n1', status: 2, nodeId: 1, interfaces: [] } },
    });

    const card = screen.getByTestId('custom-node-card');
    expect(card.className).not.toContain('shadow-');
    expect(card.className).toContain('border-[var(--card-canvas-border)]');
  });
});
