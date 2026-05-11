// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { writable, get } from 'svelte/store';

export type InfoPanelKind = 'node' | 'network';

export interface InfoPanelEntry {
  id: string;
  kind: InfoPanelKind;
  refId: number;
  position: { x: number; y: number };
  zIndex: number;
}

const MAX_PANELS = 4;
const STACK_X = 16;
const STACK_Y_BASE = 16;
const STACK_GAP = 12;
const PANEL_HEIGHT_GUESS = 260;

function panelKey(kind: InfoPanelKind, refId: number): string {
  return `${kind}:${refId}`;
}

function createInfoPanelsStore() {
  const { subscribe, update } = writable<InfoPanelEntry[]>([]);
  let zCursor = 100;

  function nextStackPosition(existing: InfoPanelEntry[]): { x: number; y: number } {
    const slotsTaken = new Set<number>();
    for (const entry of existing) {
      const slot = Math.round((entry.position.y - STACK_Y_BASE) / (PANEL_HEIGHT_GUESS + STACK_GAP));
      if (slot >= 0 && slot < MAX_PANELS) slotsTaken.add(slot);
    }
    for (let slot = 0; slot < MAX_PANELS; slot += 1) {
      if (!slotsTaken.has(slot)) {
        return {
          x: STACK_X,
          y: STACK_Y_BASE + slot * (PANEL_HEIGHT_GUESS + STACK_GAP),
        };
      }
    }
    return { x: STACK_X, y: STACK_Y_BASE };
  }

  function open(kind: InfoPanelKind, refId: number): void {
    update((entries) => {
      const key = panelKey(kind, refId);
      const existing = entries.find((entry) => entry.id === key);
      if (existing) {
        zCursor += 1;
        return entries.map((entry) =>
          entry.id === key ? { ...entry, zIndex: zCursor } : entry
        );
      }
      // LRU drop when over the cap. ``shift`` removes the oldest (front).
      let pruned = entries;
      while (pruned.length >= MAX_PANELS) pruned = pruned.slice(1);
      zCursor += 1;
      return [
        ...pruned,
        {
          id: key,
          kind,
          refId,
          position: nextStackPosition(pruned),
          zIndex: zCursor,
        },
      ];
    });
  }

  function close(id: string): void {
    update((entries) => entries.filter((entry) => entry.id !== id));
  }

  function move(id: string, x: number, y: number): void {
    update((entries) =>
      entries.map((entry) =>
        entry.id === id ? { ...entry, position: { x, y } } : entry
      )
    );
  }

  function bringToFront(id: string): void {
    update((entries) => {
      zCursor += 1;
      return entries.map((entry) =>
        entry.id === id ? { ...entry, zIndex: zCursor } : entry
      );
    });
  }

  function reset(): void {
    update(() => []);
  }

  return { subscribe, open, close, move, bringToFront, reset, _peek: () => get({ subscribe }) };
}

export const infoPanels = createInfoPanelsStore();
