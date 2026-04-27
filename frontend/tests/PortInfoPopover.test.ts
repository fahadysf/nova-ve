// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-079 — PortInfoPopover renders interface metadata, supports ESC and
 * outside-click dismissal, and tints the live-MAC row amber on mismatch.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import PortInfoPopover from '$lib/components/canvas/PortInfoPopover.svelte';
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
  // The popover portals to document.body; clean up between cases so stale
  // popovers don't leak into the next test.
  document
    .querySelectorAll('[data-testid="port-info-popover"]')
    .forEach((node) => node.parentNode?.removeChild(node));
});

describe('PortInfoPopover', () => {
  it('renders interface name + planned MAC + 4 placeholder rows when peer/status/speed/mtu/duplex are null', () => {
    render(PortInfoPopover, {
      props: {
        kind: 'interface',
        interfaceName: 'Gi1',
        plannedMac: 'aa:bb:cc:dd:ee:01',
        liveMac: null,
        peerLabel: null,
        status: 'unknown',
        speed: null,
        mtu: null,
        duplex: null,
        anchorRect: baseAnchor,
        placement: 'right',
      },
    });

    const name = document.querySelector('[data-testid="popover-interface-name"]') as HTMLElement;
    expect(name).not.toBeNull();
    expect(name.textContent).toContain('Gi1');

    const planned = document.querySelector('[data-testid="popover-planned-mac"]') as HTMLElement;
    expect(planned.textContent).toContain('aa:bb:cc:dd:ee:01');

    // Live MAC + peer + speed + mtu + duplex all default to the em-dash.
    const live = document.querySelector('[data-testid="popover-live-mac"]') as HTMLElement;
    expect(live.textContent?.trim()).toBe('—');

    const peer = document.querySelector('[data-testid="popover-peer"]') as HTMLElement;
    expect(peer.textContent).toContain('—');

    const speed = document.querySelector('[data-testid="popover-speed"]') as HTMLElement;
    expect(speed.textContent).toContain('—');

    const mtu = document.querySelector('[data-testid="popover-mtu"]') as HTMLElement;
    expect(mtu.textContent).toContain('—');

    const duplex = document.querySelector('[data-testid="popover-duplex"]') as HTMLElement;
    expect(duplex.textContent).toContain('—');
  });

  it('renders the live MAC row with amber styling and a "!" glyph when state is mismatch', () => {
    const liveMac: LiveMacState = {
      state: 'mismatch',
      planned_mac: 'aa:bb:cc:dd:ee:01',
      live_mac: 'ff:ff:ff:ff:ff:ff',
    };
    render(PortInfoPopover, {
      props: {
        kind: 'interface',
        interfaceName: 'Gi1',
        plannedMac: 'aa:bb:cc:dd:ee:01',
        liveMac,
        peerLabel: null,
        status: 'unknown',
        speed: null,
        mtu: null,
        duplex: null,
        anchorRect: baseAnchor,
        placement: 'right',
      },
    });

    const live = document.querySelector('[data-testid="popover-live-mac"]') as HTMLElement;
    expect(live).not.toBeNull();
    expect(live.className).toContain('text-amber-300');
    expect(live.textContent ?? '').toContain('!');
    expect(live.textContent ?? '').toContain('ff:ff:ff:ff:ff:ff');
  });

  it('ESC keypress invokes onClose', async () => {
    const onClose = vi.fn();
    render(PortInfoPopover, {
      props: {
        kind: 'interface',
        interfaceName: 'Gi1',
        plannedMac: null,
        liveMac: null,
        peerLabel: null,
        status: 'unknown',
        speed: null,
        mtu: null,
        duplex: null,
        anchorRect: baseAnchor,
        placement: 'right',
        onClose,
      },
    });

    // Wait a tick so the deferred outside-click registration also takes
    // effect; ESC handler is registered synchronously but flushing the
    // microtask queue keeps the test deterministic.
    await new Promise((resolve) => setTimeout(resolve, 1));

    const escEvent = new KeyboardEvent('keydown', { key: 'Escape' });
    window.dispatchEvent(escEvent);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('outside-click invokes onClose; clicks inside the popover do NOT', async () => {
    const onClose = vi.fn();
    render(PortInfoPopover, {
      props: {
        kind: 'interface',
        interfaceName: 'Gi1',
        plannedMac: null,
        liveMac: null,
        peerLabel: null,
        status: 'unknown',
        speed: null,
        mtu: null,
        duplex: null,
        anchorRect: baseAnchor,
        placement: 'right',
        onClose,
      },
    });

    // The outside-click listener is registered on the next tick (so the
    // mousedown that opened the popover doesn't immediately close it).
    await new Promise((resolve) => setTimeout(resolve, 5));

    // Click on the popover itself — should NOT close.
    const popover = document.querySelector('[data-testid="port-info-popover"]') as HTMLElement;
    const insideEvent = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    popover.dispatchEvent(insideEvent);
    expect(onClose).not.toHaveBeenCalled();

    // Click on document.body — should close.
    const outsideEvent = new MouseEvent('mousedown', { bubbles: true, cancelable: true });
    document.body.dispatchEvent(outsideEvent);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('status dot reflects up/down/unknown via the bg-* class', () => {
    const cases: Array<{ status: 'up' | 'down' | 'unknown'; expected: string }> = [
      { status: 'up', expected: 'bg-emerald-400' },
      { status: 'down', expected: 'bg-red-400' },
      { status: 'unknown', expected: 'bg-gray-500' },
    ];
    for (const { status, expected } of cases) {
      render(PortInfoPopover, {
        props: {
          kind: 'interface',
          interfaceName: 'Gi1',
          plannedMac: null,
          liveMac: null,
          peerLabel: null,
          status,
          speed: null,
          mtu: null,
          duplex: null,
          anchorRect: baseAnchor,
          placement: 'right',
        },
      });
      const dots = document.querySelectorAll('[data-testid="popover-status-dot"]');
      const lastDot = dots[dots.length - 1] as HTMLElement;
      expect(lastDot.className).toContain(expected);
      // Clean up between iterations so the next render's status dot is
      // counted as the new "last".
      document
        .querySelectorAll('[data-testid="port-info-popover"]')
        .forEach((node) => node.parentNode?.removeChild(node));
    }
  });
});
