// Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import { buildExternalConsoleUrl, externalConsoleStore } from '$lib/stores/externalConsole';

const STORAGE_KEY = 'nova-ve.external-console';

describe('external console store (#269)', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('defaults to off and does not touch storage until changed', () => {
    expect(get(externalConsoleStore)).toBe('off');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull();
  });

  it('persists the chosen mode to localStorage', () => {
    externalConsoleStore.setMode('on');
    expect(get(externalConsoleStore)).toBe('on');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('on');

    externalConsoleStore.setMode('off');
    expect(get(externalConsoleStore)).toBe('off');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('off');
  });

  it('toggle flips the mode and persists it', () => {
    expect(externalConsoleStore.toggle()).toBe('on');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('on');
    expect(externalConsoleStore.toggle()).toBe('off');
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe('off');
  });

  it('restores the persisted choice for a fresh import (reload persistence)', async () => {
    window.localStorage.setItem(STORAGE_KEY, 'on');
    const fresh = await import('$lib/stores/externalConsole?fresh=on');
    expect(get(fresh.externalConsoleStore)).toBe('on');
  });

  it('a stored off value restores off', async () => {
    window.localStorage.setItem(STORAGE_KEY, 'off');
    const fresh = await import('$lib/stores/externalConsole?fresh=off');
    expect(get(fresh.externalConsoleStore)).toBe('off');
  });

  it('builds scheme://host:port URLs without credentials', () => {
    expect(buildExternalConsoleUrl({ scheme: 'telnet', host: '192.168.7.9', port: 47501 })).toBe(
      'telnet://192.168.7.9:47501'
    );
    expect(buildExternalConsoleUrl({ scheme: 'vnc', host: 'console.example.net', port: 5902 })).toBe(
      'vnc://console.example.net:5902'
    );
    expect(buildExternalConsoleUrl({ scheme: 'rdp', host: '203.0.113.7', port: 3390 })).toBe(
      'rdp://203.0.113.7:3390'
    );
  });
});
