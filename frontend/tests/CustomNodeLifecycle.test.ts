// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { fireEvent, render, screen } from '@testing-library/svelte';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@xyflow/svelte', async () => ({
  Handle: (await import('./mocks/LeafStub.svelte')).default,
  Position: { Left: 'left', Right: 'right' },
}));

import CustomNode from '$lib/components/canvas/CustomNode.svelte';

describe('node card lifecycle action', () => {
  it('starts a stopped node and stops a running node without bubbling to the canvas', async () => {
    const onLifecycleAction = vi.fn();
    const props = { label: 'edge-a', nodeId: 1, interfaces: [], status: 0, onLifecycleAction };
    const { rerender } = render(CustomNode, { props: { data: props } });
    const card = screen.getByTestId('custom-node-card');
    const onCardClick = vi.fn();
    card.addEventListener('click', onCardClick);
    await fireEvent.click(screen.getByRole('button', { name: 'Start edge-a' }));
    expect(onLifecycleAction).toHaveBeenCalledOnce();
    expect(onLifecycleAction).toHaveBeenCalledWith('start');
    expect(onCardClick).not.toHaveBeenCalled();

    await rerender({ data: { ...props, status: 2 } });
    await fireEvent.click(screen.getByRole('button', { name: 'Stop edge-a' }));
    expect(onLifecycleAction).toHaveBeenLastCalledWith('stop');
    expect(onCardClick).not.toHaveBeenCalled();
  });

  it('shows and disables the action throughout an in-flight lifecycle operation', async () => {
    const onLifecycleAction = vi.fn();
    render(CustomNode, { props: { data: {
      label: 'edge-b', nodeId: 2, interfaces: [], status: 0,
      transientStatus: 'starting', onLifecycleAction,
    } } });
    const button = screen.getByRole('button', { name: 'Starting edge-b' });
    expect(button).toBeDisabled();
    expect(button).toHaveClass('node-lifecycle-pending', 'nodrag');
    await fireEvent.click(button);
    expect(onLifecycleAction).not.toHaveBeenCalled();
  });
});
