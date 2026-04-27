// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it, afterEach } from 'vitest';
import { render } from '@testing-library/svelte';
import PortTooltip from '$lib/components/canvas/PortTooltip.svelte';
import type { LiveMacState } from '$lib/types';

const baseAnchor: DOMRect = {
  x: 100,
  y: 100,
  left: 100,
  top: 100,
  right: 110,
  bottom: 110,
  width: 10,
  height: 10,
  toJSON() {
    return {};
  },
} as DOMRect;

afterEach(() => {
  // PortTooltip portals to document.body — ensure no stale tooltips leak between tests.
  document
    .querySelectorAll('[data-testid="port-tooltip"]')
    .forEach((node) => node.parentNode?.removeChild(node));
});

describe('PortTooltip', () => {
  it('renders a mismatch row with amber styling and a "!" glyph', () => {
    const liveMac: LiveMacState = {
      state: 'mismatch',
      planned_mac: 'aa:bb:cc:dd:ee:01',
      live_mac: 'ff:ff:ff:ff:ff:ff',
    };
    render(PortTooltip, {
      props: {
        interfaceName: 'Gi1',
        liveMac,
        placement: 'right',
        anchorRect: baseAnchor,
      },
    });

    const row = document.querySelector('[data-testid="live-mac-row"]') as HTMLElement | null;
    expect(row).not.toBeNull();
    expect(row?.className).toContain('text-amber-300');
    expect(row?.textContent ?? '').toContain('!');
    expect(row?.textContent ?? '').toContain('ff:ff:ff:ff:ff:ff');
  });

  it('renders a confirmed row without amber styling', () => {
    const liveMac: LiveMacState = {
      state: 'confirmed',
      planned_mac: 'aa:bb:cc:dd:ee:01',
      live_mac: 'aa:bb:cc:dd:ee:01',
    };
    render(PortTooltip, {
      props: {
        interfaceName: 'Gi1',
        liveMac,
        placement: 'right',
        anchorRect: baseAnchor,
      },
    });

    const row = document.querySelector('[data-testid="live-mac-row"]') as HTMLElement | null;
    expect(row).not.toBeNull();
    expect(row?.className).not.toContain('text-amber-300');
    expect(row?.textContent ?? '').toContain('aa:bb:cc:dd:ee:01');
    expect(row?.textContent ?? '').not.toContain('!');
  });

  it('renders the unavailable row with the reason text', () => {
    const liveMac: LiveMacState = {
      state: 'unavailable',
      planned_mac: 'aa:bb:cc:dd:ee:01',
      reason: 'qmp unreachable',
    };
    render(PortTooltip, {
      props: {
        interfaceName: 'Gi1',
        liveMac,
        placement: 'right',
        anchorRect: baseAnchor,
      },
    });

    const row = document.querySelector('[data-testid="live-mac-row"]') as HTMLElement | null;
    expect(row).not.toBeNull();
    expect(row?.textContent ?? '').toContain('unavailable');
    expect(row?.textContent ?? '').toContain('qmp unreachable');
  });
});
