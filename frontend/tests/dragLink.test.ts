// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { createDragLinkStore, type DragLinkEndpoint } from '$lib/stores/dragLink';

const sourceEndpoint: DragLinkEndpoint = {
  nodeId: 1,
  interfaceIndex: 0,
  port: { side: 'right', offset: 0.5 },
  interfaceName: 'eth0',
};

const targetEndpoint: DragLinkEndpoint = {
  nodeId: 2,
  interfaceIndex: 1,
  port: { side: 'left', offset: 0.5 },
  interfaceName: 'eth1',
};

describe('dragLinkStore', () => {
  it('starts in the idle state with no source/target/pointer', () => {
    const store = createDragLinkStore();
    expect(get(store).state).toBe('idle');
    expect(get(store).source).toBeNull();
    expect(get(store).idempotencyKey).toBeNull();
  });

  it('transitions idle → port_pressed on start with a fresh idempotency key', () => {
    const store = createDragLinkStore();
    let counter = 0;
    store.setKeyFactory(() => `key-${(counter += 1)}`);
    store.start({ source: sourceEndpoint, pointer: { x: 10, y: 20 } });
    const snap = get(store);
    expect(snap.state).toBe('port_pressed');
    expect(snap.source).toEqual(sourceEndpoint);
    expect(snap.idempotencyKey).toBe('key-1');
  });

  it('transitions port_pressed → dragging on the first move', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 25, y: 30 } });
    expect(get(store).state).toBe('dragging');
    expect(get(store).pointer).toEqual({ x: 25, y: 30 });
  });

  it('transitions dragging → near_target when the cursor enters a target hot zone', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 25, y: 30 } });
    store.move({ pointer: { x: 100, y: 50 }, target: targetEndpoint, nearTarget: true });
    expect(get(store).state).toBe('near_target');
    expect(get(store).target).toEqual(targetEndpoint);
  });

  it('transitions near_target → confirming on release with a reachable target', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 100, y: 50 }, target: targetEndpoint, nearTarget: true });
    store.release({ pointer: { x: 102, y: 51 }, targetReachable: true });
    expect(get(store).state).toBe('confirming');
    expect(get(store).target).toEqual(targetEndpoint);
  });

  it('falls back from near_target to dragging when the cursor leaves the hot zone', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 100, y: 50 }, target: targetEndpoint, nearTarget: true });
    store.move({ pointer: { x: 200, y: 200 }, target: null, nearTarget: false });
    expect(get(store).state).toBe('dragging');
    expect(get(store).target).toBeNull();
  });

  it('cancels back to idle on release far from any target', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 50, y: 60 } });
    store.release({ pointer: { x: 999, y: 999 }, targetReachable: false });
    const snap = get(store);
    expect(snap.state).toBe('idle');
    expect(snap.idempotencyKey).toBeNull();
    expect(snap.source).toBeNull();
  });

  it('cancels (ESC equivalent) at every state and discards the idempotency key', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint });
    store.cancel();
    expect(get(store).state).toBe('idle');
    expect(get(store).idempotencyKey).toBeNull();

    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 50, y: 60 } });
    store.cancel();
    expect(get(store).state).toBe('idle');

    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 100, y: 60 }, target: targetEndpoint, nearTarget: true });
    store.release({ pointer: { x: 100, y: 60 }, targetReachable: true });
    expect(get(store).state).toBe('confirming');
    store.cancel();
    expect(get(store).state).toBe('idle');
    expect(get(store).idempotencyKey).toBeNull();
  });

  it('records styleOverride when confirm is invoked from confirming state', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint });
    store.move({ pointer: { x: 100, y: 60 }, target: targetEndpoint, nearTarget: true });
    store.release({ pointer: { x: 100, y: 60 }, targetReachable: true });
    store.confirm({ styleOverride: 'orthogonal' });
    expect(get(store).styleOverride).toBe('orthogonal');
    expect(get(store).state).toBe('confirming');
  });

  it('preserves the same idempotency key throughout one drag and replaces it on the next', () => {
    const store = createDragLinkStore();
    let counter = 0;
    store.setKeyFactory(() => `K${(counter += 1)}`);
    store.start({ source: sourceEndpoint });
    const firstKey = get(store).idempotencyKey;
    store.move({ pointer: { x: 30, y: 40 } });
    store.move({ pointer: { x: 50, y: 60 }, target: targetEndpoint, nearTarget: true });
    expect(get(store).idempotencyKey).toBe(firstKey);
    store.cancel();
    store.start({ source: sourceEndpoint });
    expect(get(store).idempotencyKey).toBe('K2');
  });

  it('ignores moves while in idle state', () => {
    const store = createDragLinkStore();
    store.move({ pointer: { x: 1, y: 1 } });
    expect(get(store).state).toBe('idle');
  });
});
