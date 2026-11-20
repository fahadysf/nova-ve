/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

import { writable } from 'svelte/store';
import { browser } from '$app/environment';

// External console launch (#269). When 'on', console actions emit OS protocol
// links (telnet://, vnc://, rdp://) for the node's console instead of opening
// the in-browser console workspace; unreachable combinations fall back to the
// in-browser console with a toast. The choice persists per browser profile.
export type ExternalConsoleMode = 'on' | 'off';

const STORAGE_KEY = 'nova-ve.external-console';
const DEFAULT_MODE: ExternalConsoleMode = 'off';

function readStoredMode(): ExternalConsoleMode {
  if (!browser) return DEFAULT_MODE;
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'on' ? 'on' : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

function persistMode(mode: ExternalConsoleMode) {
  if (!browser) return;
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    /* storage unavailable — the toggle still works for this session */
  }
}

function createExternalConsoleStore() {
  const { subscribe, update } = writable<ExternalConsoleMode>(readStoredMode());

  return {
    subscribe,
    setMode(mode: ExternalConsoleMode) {
      update(() => mode);
      persistMode(mode);
    },
    toggle(): ExternalConsoleMode {
      let next: ExternalConsoleMode = 'off';
      update((mode) => {
        next = mode === 'on' ? 'off' : 'on';
        return next;
      });
      persistMode(next);
      return next;
    }
  };
}

export const externalConsoleStore = createExternalConsoleStore();

export function buildExternalConsoleUrl(info: { scheme: string; host: string; port: number }): string {
  // host:port only — credentials are never embedded in external links.
  return `${info.scheme}://${info.host}:${info.port}`;
}
