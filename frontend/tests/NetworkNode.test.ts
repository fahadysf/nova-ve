// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

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

  it('places the info button at the bottom-left corner inside the network box', () => {
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

    expect(screen.getByTestId('network-info-button')).toHaveClass('absolute', 'bottom-1', 'left-1');
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
});
