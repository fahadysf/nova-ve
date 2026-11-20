// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it } from 'vitest';
import { tick } from 'svelte';

import LinkConfirmModal from '$lib/components/canvas/LinkConfirmModal.svelte';
import { dragLinkStore } from '$lib/stores/dragLink';

function beginDrag(): void {
  dragLinkStore.start({
    source: {
      kind: 'interface',
      nodeId: 1,
      interfaceIndex: 0,
      port: { side: 'right', offset: 0.5 },
      interfaceName: 'eth0',
      plannedMac: null,
    },
    pointer: { x: 0, y: 0 },
  });
  dragLinkStore.move({
    pointer: { x: 10, y: 10 },
    target: {
      kind: 'network',
      networkId: 1,
      side: 'left',
      offset: 0.5,
      networkName: 'net-1',
    },
    nearTarget: true,
  });
  dragLinkStore.release({ pointer: { x: 10, y: 10 }, targetReachable: true });
}

function styleRadios(): HTMLInputElement[] {
  return screen
    .getAllByRole('radio')
    .filter((radio): radio is HTMLInputElement => radio.getAttribute('name') === 'link-style');
}

function checkedStyle(): string {
  const checked = styleRadios().find((radio) => radio.checked);
  if (!checked) throw new Error('no style radio is checked');
  return checked.value;
}

afterEach(() => {
  dragLinkStore.cancel();
});

describe('LinkConfirmModal — orthogonal default for new links (#258)', () => {
  it('opens with orthogonal preselected on the first link', async () => {
    render(LinkConfirmModal, { props: { nodes: {}, links: [] } });
    beginDrag();
    await tick();
    expect(checkedStyle()).toBe('orthogonal');
  });

  it('resets to orthogonal on every new dialog after a different choice', async () => {
    render(LinkConfirmModal, { props: { nodes: {}, links: [] } });

    beginDrag();
    await tick();
    const straight = styleRadios().find((radio) => radio.value === 'straight');
    if (!straight) throw new Error('straight radio missing');
    await straight.click();
    expect(checkedStyle()).toBe('straight');

    // Second drag: the previous choice must not carry over.
    dragLinkStore.cancel();
    await tick();
    beginDrag();
    await tick();
    expect(checkedStyle()).toBe('orthogonal');
  });
});
