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
      Bottom: 'bottom',
      Left: 'left',
      Right: 'right',
      Top: 'top',
    },
  };
});

import NetworkNode from '$lib/components/canvas/NetworkNode.svelte';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('NetworkNode', () => {
  it('shows the network name on the box face without a repeated type label', () => {
    render(NetworkNode, {
      props: {
        id: 'network7',
        data: {
          label: 'edge-transport-bridge-01',
          count: 2,
          networkId: 7,
          linkIds: [],
        },
      },
    });

    expect(screen.getByText('edge-transport-bridge-01')).toBeInTheDocument();
    expect(screen.queryByText('Network')).toBeNull();
  });

  it('places the info button at the bottom-right corner inside the network box', () => {
    render(NetworkNode, {
      props: {
        id: 'network7',
        data: {
          label: 'edge-transport-bridge-01',
          count: 2,
          networkId: 7,
          linkIds: [],
        },
      },
    });

    expect(screen.getByTestId('network-info-button')).toHaveClass('absolute', 'bottom-1', 'right-1');
  });

  it('renders the dotted open slot at the canvas-nudged position when provided (#258)', () => {
    const { container } = render(NetworkNode, {
      props: {
        id: 'network7',
        data: {
          label: 'edge-transport-bridge-01',
          count: 1,
          networkId: 7,
          linkIds: ['link-a'],
          openSlotPosition: { side: 'right', offset: 0.75 },
        },
      },
    });

    const openPort = container.querySelector('[data-network-port-open="true"]');
    expect(openPort).not.toBeNull();
    expect(openPort!.getAttribute('data-network-port-side')).toBe('right');
    expect(openPort!.getAttribute('data-network-port-offset')).toBe('0.7500');
  });

  it('binds connected network handles to their persisted link endpoint', () => {
    render(NetworkNode, {
      props: {
        id: 'network7',
        data: {
          label: 'edge-transport-bridge-01',
          count: 1,
          networkId: 7,
          linkIds: ['lnk_001'],
          portRefsByLinkId: {
            lnk_001: { endpointKey: 'to' },
          },
        },
      },
    });

    const connectedPort = screen
      .getAllByTestId('network-port-handle')
      .map((handle) => handle.closest('[data-network-port-link-id]'))
      .find((wrapper) => wrapper?.getAttribute('data-network-port-link-id') === 'lnk_001');

    expect(connectedPort).toHaveAttribute('data-network-port-endpoint-key', 'to');
  });

  it('keeps the dotted open connection handle at its canonical slot (#258 — no repositioning)', async () => {
    render(NetworkNode, {
      props: {
        id: 'network7',
        data: {
          label: 'edge-transport-bridge-01',
          count: 0,
          networkId: 7,
          linkIds: [],
        },
      },
    });

    const openPort = document.querySelector(
      '[data-network-port-id="7"][data-network-port-open="true"]'
    ) as HTMLElement | null;
    expect(openPort).not.toBeNull();
    expect(openPort).toHaveAttribute('data-network-port-side', 'right');
    expect(openPort?.getAttribute('data-network-port-offset')).toBe('0.5000');

    // Ctrl+drag repositioning was removed (#258): the port may follow the
    // cursor as a link-drag preview, but must settle back to its canonical
    // slot once the gesture ends.
    openPort?.dispatchEvent(
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

    const settledPort = document.querySelector(
      '[data-network-port-id="7"][data-network-port-open="true"]'
    ) as HTMLElement | null;
    expect(settledPort).toHaveAttribute('data-network-port-side', 'right');
    expect(settledPort?.getAttribute('data-network-port-offset')).toBe('0.5000');
  });
});
