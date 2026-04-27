// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createWsClient, getBackoffDelay } from '$lib/services/wsClient';

interface MockSocketRecord {
  url: string;
  socket: MockSocket;
}

class MockSocket {
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSING = 2;
  static CLOSED = 3;

  readyState = MockSocket.CONNECTING;
  url: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private listeners: Map<string, Set<(event: any) => void>> = new Map();

  constructor(url: string) {
    this.url = url;
    sockets.push({ url, socket: this });
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  addEventListener(type: string, handler: (event: any) => void) {
    let set = this.listeners.get(type);
    if (!set) {
      set = new Set();
      this.listeners.set(type, set);
    }
    set.add(handler);
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  removeEventListener(type: string, handler: (event: any) => void) {
    this.listeners.get(type)?.delete(handler);
  }

  close() {
    this.readyState = MockSocket.CLOSED;
    this.dispatch('close', { code: 1000 });
  }

  // Test helpers — not part of the real WebSocket interface.
  dispatchOpen() {
    this.readyState = MockSocket.OPEN;
    this.dispatch('open', {});
  }

  dispatchMessage(data: unknown) {
    this.dispatch('message', { data: typeof data === 'string' ? data : JSON.stringify(data) });
  }

  dispatchClose() {
    this.readyState = MockSocket.CLOSED;
    this.dispatch('close', { code: 1006 });
  }

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  private dispatch(type: string, event: any) {
    const set = this.listeners.get(type);
    if (!set) return;
    for (const handler of Array.from(set)) handler(event);
  }
}

let sockets: MockSocketRecord[] = [];

describe('wsClient.getBackoffDelay', () => {
  it('returns the documented schedule for attempts 0..6', () => {
    const delays = [0, 1, 2, 3, 4, 5, 6].map((attempt) => getBackoffDelay(attempt));
    expect(delays).toEqual([500, 1000, 2000, 5000, 10000, 30000, 30000]);
  });
});

describe('wsClient.createWsClient', () => {
  beforeEach(() => {
    sockets = [];
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', MockSocket);
    // jsdom provides sessionStorage; clear it between tests.
    if (typeof window !== 'undefined') {
      window.sessionStorage.clear();
    }
    Object.defineProperty(window, 'location', {
      writable: true,
      value: {
        protocol: 'http:',
        host: 'example.test:5173',
      },
    });
    Object.defineProperty(document, 'hidden', {
      configurable: true,
      get: () => false,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('lastSeq round-trips through sessionStorage across two createWsClient calls', () => {
    const labId = 'demo.json';
    const first = createWsClient({ labId });
    expect(first.lastSeq).toBe(0);
    first.connect();
    expect(sockets).toHaveLength(1);
    sockets[0].socket.dispatchOpen();
    sockets[0].socket.dispatchMessage({ type: 'hello', seq: 7 });
    expect(first.lastSeq).toBe(7);
    first.close();

    sockets = [];
    const second = createWsClient({ labId });
    expect(second.lastSeq).toBe(7);
    second.connect();
    expect(sockets).toHaveLength(1);
    expect(sockets[0].url).toContain('last_seq=7');
    second.close();
  });

  it('connect() URL carries the current lastSeq query param', () => {
    const labId = 'folder/leaf.json';
    window.sessionStorage.setItem('ws:last_seq:' + labId, '42');
    const client = createWsClient({ labId });
    client.connect();
    expect(sockets).toHaveLength(1);
    // labId is URL-encoded so a slash-bearing path stays inside the path segment.
    expect(sockets[0].url).toBe('ws://example.test:5173/ws/labs/folder%2Fleaf.json?last_seq=42');
    client.close();
  });

  it('force_resnapshot followed by lab_topology updates lastSeq to the snapshot seq', () => {
    const labId = 'snap.json';
    const client = createWsClient({ labId });
    client.connect();
    sockets[0].socket.dispatchOpen();
    sockets[0].socket.dispatchMessage({ type: 'hello', seq: 1 });
    sockets[0].socket.dispatchMessage({ type: 'force_resnapshot' });
    expect(client.lastSeq).toBe(1);
    sockets[0].socket.dispatchMessage({
      type: 'lab_topology',
      seq: 99,
      rev: 'r',
      payload: { id: labId },
    });
    expect(client.lastSeq).toBe(99);
    client.close();
  });

  it('lab_topology aborts the prior controller and allocates a fresh one', () => {
    const labId = 'abort.json';
    const client = createWsClient({ labId });
    client.connect();
    sockets[0].socket.dispatchOpen();
    const firstController = client.abortController;
    expect(firstController.signal.aborted).toBe(false);
    sockets[0].socket.dispatchMessage({
      type: 'lab_topology',
      seq: 5,
      rev: 'r',
      payload: { id: labId },
    });
    expect(firstController.signal.aborted).toBe(true);
    expect(client.abortController).not.toBe(firstController);
    expect(client.abortController.signal.aborted).toBe(false);
    client.close();
  });

  it('document.hidden=true closes the socket; document.hidden=false reconnects', () => {
    const labId = 'visibility.json';
    const client = createWsClient({ labId });
    client.connect();
    expect(sockets).toHaveLength(1);
    sockets[0].socket.dispatchOpen();

    // Flip to hidden — close should fire and no reconnect should be scheduled.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    document.dispatchEvent(new Event('visibilitychange'));
    // The client requested a close on the live socket.
    expect(sockets[0].socket.readyState).toBe(MockSocket.CLOSED);

    // Advance well past the 30s cap — no new socket should appear while hidden.
    vi.advanceTimersByTime(60_000);
    expect(sockets).toHaveLength(1);

    // Flip back to visible — an immediate reconnect should fire.
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
    document.dispatchEvent(new Event('visibilitychange'));
    vi.advanceTimersByTime(0);
    expect(sockets).toHaveLength(2);

    client.close();
  });
});
