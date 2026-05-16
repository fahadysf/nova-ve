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
});
