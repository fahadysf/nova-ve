/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

import { writable } from 'svelte/store';
import { browser } from '$app/environment';

// Canvas UI preferences, persisted per browser profile. The minimap
// (#273) is collapsed to a small floating box by default and only shown
// when the user expands it from that box.
export interface CanvasPrefs {
  minimapExpanded: boolean;
}

const STORAGE_KEY = 'nova-ve.canvas-prefs';
const DEFAULT_PREFS: CanvasPrefs = {
  minimapExpanded: false
};

function readStoredPrefs(): CanvasPrefs {
  if (!browser) return { ...DEFAULT_PREFS };
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_PREFS };
    const parsed = JSON.parse(raw) as Partial<CanvasPrefs>;
    return {
      minimapExpanded:
        typeof parsed.minimapExpanded === 'boolean'
          ? parsed.minimapExpanded
          : DEFAULT_PREFS.minimapExpanded
    };
  } catch {
    return { ...DEFAULT_PREFS };
  }
}

function persistPrefs(prefs: CanvasPrefs) {
  if (!browser) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
  } catch {
    /* storage unavailable — the preference still applies this session */
  }
}

function createCanvasPrefsStore() {
  const { subscribe, update } = writable<CanvasPrefs>(readStoredPrefs());

  return {
    subscribe,
    setMinimapExpanded(expanded: boolean) {
      update((prefs) => {
        const next = { ...prefs, minimapExpanded: expanded };
        persistPrefs(next);
        return next;
      });
    },
    toggleMinimap(): boolean {
      let next = false;
      update((prefs) => {
        next = !prefs.minimapExpanded;
        const updated = { ...prefs, minimapExpanded: next };
        persistPrefs(updated);
        return updated;
      });
      return next;
    }
  };
}

export const canvasPrefs = createCanvasPrefsStore();
