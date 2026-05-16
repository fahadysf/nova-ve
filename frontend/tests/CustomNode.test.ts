// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

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
});
