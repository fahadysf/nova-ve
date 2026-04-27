// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
  createDragLinkStore,
  isLegalEndpointPair,
  type DragEndpoint,
  type InterfaceEndpoint,
  type NetworkEndpoint,
} from '$lib/stores/dragLink';

const sourceEndpoint: InterfaceEndpoint = {
  kind: 'interface',
  nodeId: 1,
  interfaceIndex: 0,
  port: { side: 'right', offset: 0.5 },
  interfaceName: 'eth0',
};

const targetEndpoint: InterfaceEndpoint = {
  kind: 'interface',
  nodeId: 2,
  interfaceIndex: 1,
  port: { side: 'left', offset: 0.5 },
  interfaceName: 'eth1',
};

const networkSource: NetworkEndpoint = {
  kind: 'network',
  networkId: 7,
  side: 'right',
  offset: 0.5,
  networkName: 'bridge-A',
};

const networkTarget: NetworkEndpoint = {
  kind: 'network',
  networkId: 8,
  side: 'left',
  offset: 0.5,
  networkName: 'bridge-B',
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

  it('US-080: transitions idle → port_pressed via a direct start() call (no event listeners)', () => {
    // Mirrors the production direct-call path now used by Port.svelte after
    // the SvelteFlow-event-bubble chain was retired. The store must accept the
    // call with only ``source`` + ``pointer`` and emit a fresh idempotency key.
    const store = createDragLinkStore();
    let counter = 0;
    store.setKeyFactory(() => `direct-${(counter += 1)}`);

    expect(get(store).state).toBe('idle');
    expect(get(store).idempotencyKey).toBeNull();

    store.start({
      source: sourceEndpoint,
      pointer: { x: 42, y: 84 },
    });

    const snap = get(store);
    expect(snap.state).toBe('port_pressed');
    expect(snap.source).toEqual(sourceEndpoint);
    expect(snap.pointer).toEqual({ x: 42, y: 84 });
    expect(snap.idempotencyKey).toBe('direct-1');
  });

  // ── US-081: discriminated-union endpoints + network bridges ──────────────
  it('US-081: interface → network drag walks idle → port_pressed → dragging → near_target → confirming', () => {
    const store = createDragLinkStore();
    store.start({ source: sourceEndpoint, pointer: { x: 10, y: 20 } });
    expect(get(store).state).toBe('port_pressed');
    expect(get(store).source).toEqual(sourceEndpoint);
    store.move({ pointer: { x: 30, y: 40 } });
    expect(get(store).state).toBe('dragging');
    store.move({ pointer: { x: 200, y: 50 }, target: networkTarget, nearTarget: true });
    expect(get(store).state).toBe('near_target');
    const targetSnap = get(store).target;
    expect(targetSnap?.kind).toBe('network');
    if (targetSnap?.kind === 'network') {
      expect(targetSnap.networkId).toBe(8);
      expect(targetSnap.networkName).toBe('bridge-B');
    }
    store.release({ pointer: { x: 202, y: 51 }, targetReachable: true });
    expect(get(store).state).toBe('confirming');
  });

  it('US-081: network → interface drag mirrors the interface→network machine', () => {
    const store = createDragLinkStore();
    store.start({ source: networkSource, pointer: { x: 5, y: 5 } });
    const startSnap = get(store);
    expect(startSnap.state).toBe('port_pressed');
    expect(startSnap.source?.kind).toBe('network');
    if (startSnap.source?.kind === 'network') {
      expect(startSnap.source.networkId).toBe(7);
    }
    store.move({ pointer: { x: 25, y: 30 } });
    expect(get(store).state).toBe('dragging');
    store.move({ pointer: { x: 120, y: 60 }, target: targetEndpoint, nearTarget: true });
    expect(get(store).state).toBe('near_target');
    const targetSnap = get(store).target;
    expect(targetSnap?.kind).toBe('interface');
    if (targetSnap?.kind === 'interface') {
      expect(targetSnap.nodeId).toBe(2);
      expect(targetSnap.interfaceIndex).toBe(1);
    }
    store.release({ pointer: { x: 122, y: 61 }, targetReachable: true });
    expect(get(store).state).toBe('confirming');
  });

  it('US-081: network → network drag is blocked at near_target (no self-attach across bridges)', () => {
    // Rule: cannot connect a network bridge directly to another bridge —
    // there is no node-side interface to anchor the link to. The store must
    // surface this by refusing to enter ``near_target`` for the illegal pair
    // and refusing to promote ``release`` to ``confirming``.
    const store = createDragLinkStore();
    store.start({ source: networkSource });
    store.move({ pointer: { x: 10, y: 10 } });
    expect(get(store).state).toBe('dragging');

    // pure helper agrees the pair is illegal
    expect(isLegalEndpointPair(networkSource, networkTarget)).toBe(false);

    store.move({ pointer: { x: 200, y: 50 }, target: networkTarget, nearTarget: true });
    // The store must NOT advance to near_target for an illegal pair. Target
    // is also dropped so the confirm modal cannot render a stale endpoint.
    const snap = get(store);
    expect(snap.state).toBe('dragging');
    expect(snap.target).toBeNull();

    // Even if release fires with targetReachable=true, the store must cancel.
    store.release({ pointer: { x: 200, y: 50 }, targetReachable: true });
    const after = get(store);
    expect(after.state).toBe('idle');
    expect(after.idempotencyKey).toBeNull();
  });

  it('US-081: isLegalEndpointPair allows interface↔interface, interface↔network, network↔interface', () => {
    expect(isLegalEndpointPair(sourceEndpoint, targetEndpoint)).toBe(true);
    expect(isLegalEndpointPair(sourceEndpoint, networkTarget)).toBe(true);
    expect(isLegalEndpointPair(networkSource, targetEndpoint)).toBe(true);
    expect(isLegalEndpointPair(networkSource, networkTarget)).toBe(false);
  });

  it('US-081: source/target are stored as the discriminated union (kind tag preserved)', () => {
    const store = createDragLinkStore();
    store.start({ source: networkSource });
    const snap1 = get(store);
    const src: DragEndpoint | null = snap1.source;
    expect(src?.kind).toBe('network');

    store.move({ pointer: { x: 50, y: 60 }, target: targetEndpoint, nearTarget: true });
    const snap2 = get(store);
    const tgt: DragEndpoint | null = snap2.target;
    expect(tgt?.kind).toBe('interface');
  });
});
