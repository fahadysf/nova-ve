// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/** Keeps pointer-anchored menus inside the visible canvas (#321). */

export interface Point {
  x: number;
  y: number;
}

export interface Bounds {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface MenuPlacement {
  left: number;
  top: number;
  maxWidth: number;
  maxHeight: number;
}

export const MENU_MARGIN = 8;

/** Opens after the anchor, else before it, else as close to it as fits. */
function placeAxis(anchor: number, size: number, start: number, end: number): number {
  if (anchor >= start && anchor + size <= end) return anchor;
  if (anchor <= end && anchor - size >= start) return anchor - size;
  return Math.max(start, Math.min(anchor, end - size));
}

/** Visible part of the canvas, or of the window where the canvas is too small. */
function usableRange(
  size: number,
  canvasStart: number,
  canvasEnd: number,
  viewportSize: number
): [number, number] {
  const windowRange: [number, number] = [MENU_MARGIN, viewportSize - MENU_MARGIN];
  const start = Math.max(canvasStart, 0) + MENU_MARGIN;
  const end = Math.min(canvasEnd, viewportSize) - MENU_MARGIN;
  return end - start >= size ? [start, end] : windowRange;
}

export function placeMenu(
  anchor: Point,
  size: { width: number; height: number },
  canvas: Bounds,
  viewport: { width: number; height: number }
): MenuPlacement {
  const [left, right] = usableRange(size.width, canvas.left, canvas.right, viewport.width);
  const [top, bottom] = usableRange(size.height, canvas.top, canvas.bottom, viewport.height);
  const maxWidth = Math.max(0, right - left);
  const maxHeight = Math.max(0, bottom - top);
  return {
    left: placeAxis(anchor.x, Math.min(size.width, maxWidth), left, right),
    top: placeAxis(anchor.y, Math.min(size.height, maxHeight), top, bottom),
    maxWidth,
    maxHeight
  };
}

/**
 * Svelte action for a ``position: fixed`` menu whose parent is the canvas.
 * Owns the element's left/top, so the markup must not set them.
 */
export function anchoredMenu(node: HTMLElement, anchor: Point) {
  let current = anchor;

  function place() {
    // Measure the natural size: not what a previous placement allowed, and
    // not squeezed against the window edge by a fixed element's shrink-to-fit.
    node.style.left = '0px';
    node.style.top = '0px';
    node.style.maxWidth = '';
    node.style.maxHeight = '';
    const { width, height } = node.getBoundingClientRect();
    const canvas = (node.parentElement ?? document.documentElement).getBoundingClientRect();
    const placement = placeMenu(current, { width, height }, canvas, {
      width: document.documentElement.clientWidth || window.innerWidth,
      height: document.documentElement.clientHeight || window.innerHeight
    });
    node.style.overflowY = height > placement.maxHeight ? 'auto' : '';
    node.style.maxWidth = `${placement.maxWidth}px`;
    node.style.maxHeight = `${placement.maxHeight}px`;
    node.style.left = `${placement.left}px`;
    node.style.top = `${placement.top}px`;
  }

  place();
  window.addEventListener('resize', place);
  // Covers menu contents changing while it is open, and the canvas resizing.
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place);
  observer?.observe(node);
  if (node.parentElement) observer?.observe(node.parentElement);

  return {
    update(next: Point) {
      current = next;
      place();
    },
    destroy() {
      window.removeEventListener('resize', place);
      observer?.disconnect();
    }
  };
}
