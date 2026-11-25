// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from 'vitest';
import { anchoredMenu, placeMenu } from '$lib/services/menuPlacement';

const canvas = { left: 20, top: 100, right: 1000, bottom: 700 };
const viewport = { width: 1024, height: 768 };
const menu = { width: 180, height: 240 };

describe('placeMenu (#321)', () => {
  it.each([
    ['opens at the pointer when there is room', { x: 300, y: 200 }, 300, 200],
    ['opens left of the pointer near the right edge', { x: 950, y: 200 }, 770, 200],
    ['opens above the pointer near the bottom edge', { x: 300, y: 650 }, 300, 410],
    ['flips both ways in the corner', { x: 990, y: 690 }, 810, 450],
    ['keeps the margin at the top left corner', { x: 20, y: 100 }, 28, 108]
  ])('%s', (_name, anchor, left, top) => {
    expect(placeMenu(anchor, menu, canvas, viewport)).toMatchObject({ left, top });
  });

  it('shifts just enough when neither side has room', () => {
    const low = { ...canvas, bottom: 400 };
    expect(placeMenu({ x: 300, y: 260 }, menu, low, viewport)).toMatchObject({ left: 300, top: 152 });
  });

  it('falls back to the window when the canvas is too small', () => {
    const tiny = { left: 20, top: 100, right: 150, bottom: 200 };
    expect(placeMenu({ x: 60, y: 150 }, menu, tiny, viewport)).toMatchObject({ left: 60, top: 150 });
  });

  it('ignores the part of the canvas outside the window', () => {
    const overflowing = { left: -200, top: -50, right: 2000, bottom: 2000 };
    expect(placeMenu({ x: 1000, y: 760 }, menu, overflowing, viewport)).toMatchObject({ left: 820, top: 520 });
  });

  it('limits a menu taller than the window so that it can scroll', () => {
    const placed = placeMenu({ x: 300, y: 600 }, { width: 180, height: 900 }, canvas, viewport);
    expect(placed).toMatchObject({ top: 8, maxHeight: 752 });
  });

  it('is fully visible from every pointer position', () => {
    for (const size of [menu, { width: 400, height: 560 }, { width: 900, height: 900 }]) {
      for (let x = 0; x <= viewport.width; x += 16) {
        for (let y = 0; y <= viewport.height; y += 16) {
          const p = placeMenu({ x, y }, size, canvas, viewport);
          const width = Math.min(size.width, p.maxWidth);
          const height = Math.min(size.height, p.maxHeight);
          expect(p.left).toBeGreaterThanOrEqual(0);
          expect(p.top).toBeGreaterThanOrEqual(0);
          expect(p.left + width).toBeLessThanOrEqual(viewport.width);
          expect(p.top + height).toBeLessThanOrEqual(viewport.height);
        }
      }
    }
  });
});

describe('anchoredMenu (#321)', () => {
  const observers: Array<{ run: () => void; targets: Element[]; disconnected: boolean }> = [];

  function mount(size = { ...menu }) {
    vi.stubGlobal('ResizeObserver', class {
      private entry: (typeof observers)[number];
      constructor(run: () => void) {
        this.entry = { run, targets: [], disconnected: false };
        observers.push(this.entry);
      }
      observe(target: Element) { this.entry.targets.push(target); }
      disconnect() { this.entry.disconnected = true; }
    });
    vi.spyOn(document.documentElement, 'clientWidth', 'get').mockImplementation(() => viewport.width);
    vi.spyOn(document.documentElement, 'clientHeight', 'get').mockImplementation(() => viewport.height);
    const parent = document.createElement('div');
    const node = document.createElement('div');
    parent.append(node);
    document.body.append(parent);
    const area = { ...canvas };
    parent.getBoundingClientRect = () => area as DOMRect;
    node.getBoundingClientRect = () => ({ ...size }) as DOMRect;
    return { node, parent, area, size };
  }

  afterEach(() => {
    observers.length = 0;
    document.body.replaceChildren();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  const at = (node: HTMLElement) => [node.style.left, node.style.top];

  it('follows a new anchor, its contents, the canvas, and the window', () => {
    const { node, parent, area, size } = mount();
    const action = anchoredMenu(node, { x: 300, y: 200 });
    expect(at(node)).toEqual(['300px', '200px']);
    expect(observers[0].targets).toEqual([node, parent]);

    action.update({ x: 950, y: 650 });
    expect(at(node)).toEqual(['770px', '410px']);

    size.height = 300;
    observers[0].run();
    expect(at(node)).toEqual(['770px', '350px']);

    area.right = 800;
    window.dispatchEvent(new Event('resize'));
    expect(at(node)).toEqual(['612px', '350px']);

    action.destroy();
    expect(observers[0].disconnected).toBe(true);
    area.right = 600;
    window.dispatchEvent(new Event('resize'));
    expect(at(node)).toEqual(['612px', '350px']);
  });

  it('scrolls only while the menu does not fit', () => {
    const { node, size } = mount({ width: 180, height: 900 });
    anchoredMenu(node, { x: 300, y: 200 });
    expect(node.style.overflowY).toBe('auto');
    expect(node.style.maxHeight).toBe('752px');
    size.height = 240;
    observers[0].run();
    expect(node.style.overflowY).toBe('');
  });
});
