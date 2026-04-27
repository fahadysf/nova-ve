// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * US-080 — Port mousedown bypasses XYFlow node-drag and reaches dragLinkStore
 * directly. The Port component must:
 *   1. Call ``stopPropagation()`` and ``preventDefault()`` on plain
 *      left-button mousedown so SvelteFlow's internal node-drag handler
 *      cannot claim the event.
 *   2. Drive ``dragLinkStore.start(...)`` directly with the source endpoint —
 *      the legacy ``createEventDispatcher`` chain never bubbled through
 *      SvelteFlow's component-prop renderer.
 *   3. Skip both behaviours for shift-modified mousedown (port repositioning)
 *      and right-button (or other non-zero) mousedown.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import { tick } from 'svelte';
import Port from '$lib/components/canvas/Port.svelte';
import { dragLinkStore } from '$lib/stores/dragLink';
import { selectedPortInfoStore } from '$lib/stores/portInfo';
import type { NodeInterface, PortPosition } from '$lib/types';
import { get } from 'svelte/store';

const baseInterface: NodeInterface = {
  index: 0,
  name: 'eth0',
  planned_mac: 'aa:bb:cc:dd:ee:01',
  network_id: 0,
};

const basePosition: PortPosition = { side: 'right', offset: 0.5 };

function findPortRoot(): HTMLElement {
  const el = document.querySelector(
    '[data-port-node-id="7"][data-port-interface-index="0"]'
  ) as HTMLElement | null;
  if (!el) throw new Error('Port root not rendered');
  return el;
}

/**
 * Build a synthetic ``MouseEvent`` whose ``stopPropagation`` and
 * ``preventDefault`` we can spy on. JSDOM's ``MouseEvent`` exposes both as
 * regular instance methods, so vi.spyOn works directly.
 */
function makeMouseDown(opts: { button?: number; shiftKey?: boolean } = {}): {
  event: MouseEvent;
  stopSpy: ReturnType<typeof vi.spyOn>;
  preventSpy: ReturnType<typeof vi.spyOn>;
} {
  const event = new MouseEvent('mousedown', {
    button: opts.button ?? 0,
    shiftKey: opts.shiftKey ?? false,
    bubbles: true,
    cancelable: true,
    clientX: 100,
    clientY: 200,
  });
  const stopSpy = vi.spyOn(event, 'stopPropagation');
  const preventSpy = vi.spyOn(event, 'preventDefault');
  return { event, stopSpy, preventSpy };
}

describe('Port (US-080)', () => {
  let startSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    dragLinkStore.cancel();
    startSpy = vi.spyOn(dragLinkStore, 'start');
  });

  afterEach(() => {
    startSpy.mockRestore();
    dragLinkStore.cancel();
  });

  it('plain left-button mousedown calls stopPropagation and preventDefault', () => {
    render(Port, {
      props: {
        interfaceData: baseInterface,
        position: basePosition,
        nodeId: 7,
        interfaceIndex: 0,
      },
    });

    const root = findPortRoot();
    const { event, stopSpy, preventSpy } = makeMouseDown();
    root.dispatchEvent(event);

    expect(stopSpy).toHaveBeenCalledTimes(1);
    expect(preventSpy).toHaveBeenCalledTimes(1);
  });

  it('plain left-button mousedown invokes dragLinkStore.start once with the expected endpoint', () => {
    render(Port, {
      props: {
        interfaceData: baseInterface,
        position: basePosition,
        nodeId: 7,
        interfaceIndex: 0,
      },
    });

    const root = findPortRoot();
    const { event } = makeMouseDown();
    root.dispatchEvent(event);

    expect(startSpy).toHaveBeenCalledTimes(1);
    const call = startSpy.mock.calls[0][0] as {
      source: {
        kind: 'interface';
        nodeId: number;
        interfaceIndex: number;
        port: PortPosition;
        interfaceName?: string;
        plannedMac?: string | null;
      };
      pointer?: { x: number; y: number };
    };
    // US-081: source is now a discriminated union — interface ports tag with
    // ``kind: 'interface'`` so dragLinkStore can route to the right modal/POST
    // shape downstream.
    expect(call.source.kind).toBe('interface');
    expect(call.source.nodeId).toBe(7);
    expect(call.source.interfaceIndex).toBe(0);
    expect(call.source.port).toEqual(basePosition);
    expect(call.source.interfaceName).toBe('eth0');
    expect(call.source.plannedMac).toBe('aa:bb:cc:dd:ee:01');
    expect(call.pointer).toEqual({ x: 100, y: 200 });
  });

  it('shift+mousedown does NOT call dragLinkStore.start (port-reposition flow)', () => {
    render(Port, {
      props: {
        interfaceData: baseInterface,
        position: basePosition,
        nodeId: 7,
        interfaceIndex: 0,
      },
    });

    const root = findPortRoot();
    const { event, stopSpy, preventSpy } = makeMouseDown({ shiftKey: true });
    root.dispatchEvent(event);

    expect(startSpy).not.toHaveBeenCalled();
    // Shift drags are reserved for PortLayer's perimeter clamp; we must NOT
    // suppress propagation/default — those are SvelteFlow's domain there.
    expect(stopSpy).not.toHaveBeenCalled();
    expect(preventSpy).not.toHaveBeenCalled();
  });

  it('right-click (button !== 0) does NOT call dragLinkStore.start', () => {
    render(Port, {
      props: {
        interfaceData: baseInterface,
        position: basePosition,
        nodeId: 7,
        interfaceIndex: 0,
      },
    });

    const root = findPortRoot();
    const { event, stopSpy, preventSpy } = makeMouseDown({ button: 2 });
    root.dispatchEvent(event);

    expect(startSpy).not.toHaveBeenCalled();
    expect(stopSpy).not.toHaveBeenCalled();
    expect(preventSpy).not.toHaveBeenCalled();
  });
});

/**
 * US-079 — port hover-grow + click-vs-drag detection.
 *
 *   * mousedown→mouseup at same coords within 200ms → click → emits port:click
 *     and pushes the selected-port-info store entry.
 *   * mousedown→200px move→mouseup → drag, no port:click event, store stays
 *     at its dragLink-driven state.
 *   * mouseenter toggles the hover class on the inner span (.port-handle-hover).
 */
describe('Port (US-079)', () => {
  beforeEach(() => {
    selectedPortInfoStore.set(null);
    dragLinkStore.cancel();
  });

  afterEach(() => {
    selectedPortInfoStore.set(null);
    dragLinkStore.cancel();
  });

  function dispatchMouse(
    target: HTMLElement,
    type: 'mousedown' | 'mouseup' | 'mouseenter' | 'mouseleave',
    opts: { x?: number; y?: number; button?: number } = {},
  ) {
    const event = new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      button: opts.button ?? 0,
      clientX: opts.x ?? 100,
      clientY: opts.y ?? 200,
    });
    target.dispatchEvent(event);
    return event;
  }

  it('mousedown→mouseup at same coords within 200ms is a click and updates the selected-port-info store with anchorRect', () => {
    render(Port, {
      props: {
        interfaceData: baseInterface,
        position: basePosition,
        nodeId: 7,
        interfaceIndex: 0,
      },
    });

    const root = findPortRoot();
    dispatchMouse(root, 'mousedown', { x: 100, y: 200 });
    // Same coords + immediate release → click.
    dispatchMouse(root, 'mouseup', { x: 100, y: 200 });

    // The store entry is the actual click contract — TopologyCanvas subscribes
    // to it to render the popover. (The component-level ``port:click`` event
    // is a non-bubbling Svelte event in Svelte 5, so a DOM addEventListener on
    // the root won't see it; the store update is the observable side effect.)
    const stored = get(selectedPortInfoStore);
    expect(stored).not.toBeNull();
    expect(stored?.kind).toBe('interface');
    if (stored?.kind === 'interface') {
      expect(stored.nodeId).toBe(7);
      expect(stored.interfaceIndex).toBe(0);
      expect(stored.interfaceName).toBe('eth0');
      expect(stored.plannedMac).toBe('aa:bb:cc:dd:ee:01');
      // anchorRect is whatever getBoundingClientRect returns under jsdom — it's
      // an object (possibly all zeros), but the click handler captured it.
      expect(stored.anchorRect).not.toBeUndefined();
    }
  });

  it('mousedown → 200px move → mouseup is a drag and does NOT update the selected-port-info store', () => {
    render(Port, {
      props: {
        interfaceData: baseInterface,
        position: basePosition,
        nodeId: 7,
        interfaceIndex: 0,
      },
    });

    const root = findPortRoot();
    dispatchMouse(root, 'mousedown', { x: 100, y: 200 });
    // Cursor moved 200px → distance > 4px threshold → drag, not click.
    dispatchMouse(root, 'mouseup', { x: 300, y: 200 });

    // The store must not have been mutated by a drag-release.
    expect(get(selectedPortInfoStore)).toBeNull();
  });

  it('mouseenter sets the .port-handle-hover class on the inner handle (hover-grow)', async () => {
    render(Port, {
      props: {
        interfaceData: baseInterface,
        position: basePosition,
        nodeId: 7,
        interfaceIndex: 0,
      },
    });

    const root = findPortRoot();
    const handle = root.querySelector('[data-testid="port-handle"]') as HTMLElement | null;
    expect(handle).not.toBeNull();
    // Baseline: no hover class.
    expect(handle?.className).not.toContain('port-handle-hover');

    dispatchMouse(root, 'mouseenter');
    // Svelte 5 schedules DOM updates microtask-async; await tick() so the
    // class assignment from ``isHovered = true`` flushes to the span.
    await tick();
    const handleAfter = root.querySelector('[data-testid="port-handle"]') as HTMLElement;
    expect(handleAfter.className).toContain('port-handle-hover');

    dispatchMouse(root, 'mouseleave');
    await tick();
    const handleLeft = root.querySelector('[data-testid="port-handle"]') as HTMLElement;
    expect(handleLeft.className).not.toContain('port-handle-hover');
  });
});
