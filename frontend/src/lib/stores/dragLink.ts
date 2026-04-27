// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { writable, get } from 'svelte/store';
import type { Readable } from 'svelte/store';
import type { LinkStyle, PortPosition } from '$lib/types';

export type DragLinkState =
  | 'idle'
  | 'port_pressed'
  | 'dragging'
  | 'near_target'
  | 'confirming';

/** Side of the network bridge a perimeter handle docks to. */
export type Side = 'top' | 'right' | 'bottom' | 'left';

/** Node-interface endpoint (a Port on a CustomNode). */
export interface InterfaceEndpoint {
  kind: 'interface';
  nodeId: number;
  interfaceIndex: number;
  port: PortPosition;
  /** Optional human-friendly label rendered in the confirm modal. */
  interfaceName?: string;
  /** Pre-existing planned MAC (if any) for confirm-modal preview. */
  plannedMac?: string | null;
}

/** Network-bridge endpoint (one of the four perimeter handles on a NetworkNode). */
export interface NetworkEndpoint {
  kind: 'network';
  networkId: number;
  side: Side;
  offset: number;
  /** Optional human-friendly label rendered in the confirm modal. */
  networkName?: string;
}

export type DragEndpoint = InterfaceEndpoint | NetworkEndpoint;

export interface DragLinkPointer {
  x: number;
  y: number;
}

export interface DragLinkSnapshot {
  state: DragLinkState;
  source: DragEndpoint | null;
  target: DragEndpoint | null;
  pointer: DragLinkPointer | null;
  idempotencyKey: string | null;
  styleOverride: LinkStyle | null;
}

export interface DragLinkStore extends Readable<DragLinkSnapshot> {
  start(args: { source: DragEndpoint; pointer?: DragLinkPointer }): void;
  move(args: {
    pointer: DragLinkPointer;
    target?: DragEndpoint | null;
    nearTarget?: boolean;
  }): void;
  hover(target: DragEndpoint | null): void;
  enterTargetZone(): void;
  leaveTargetZone(): void;
  release(args: { pointer: DragLinkPointer; targetReachable: boolean }): void;
  confirm(args?: { styleOverride?: LinkStyle | null }): void;
  cancel(): void;
  reset(): void;
  setKeyFactory(factory: () => string): void;
}

function emptySnapshot(): DragLinkSnapshot {
  return {
    state: 'idle',
    source: null,
    target: null,
    pointer: null,
    idempotencyKey: null,
    styleOverride: null,
  };
}

export function defaultIdempotencyKey(): string {
  const cryptoApi: Crypto | undefined =
    typeof globalThis !== 'undefined' && (globalThis as { crypto?: Crypto }).crypto
      ? (globalThis as { crypto: Crypto }).crypto
      : undefined;
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const randomBytes = new Uint8Array(10);
    cryptoApi.getRandomValues(randomBytes);
    const ts = BigInt(Date.now());
    const bytes = new Uint8Array(16);
    bytes[0] = Number((ts >> 40n) & 0xffn);
    bytes[1] = Number((ts >> 32n) & 0xffn);
    bytes[2] = Number((ts >> 24n) & 0xffn);
    bytes[3] = Number((ts >> 16n) & 0xffn);
    bytes[4] = Number((ts >> 8n) & 0xffn);
    bytes[5] = Number(ts & 0xffn);
    bytes.set(randomBytes, 6);
    bytes[6] = (bytes[6] & 0x0f) | 0x70; // version 7
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // RFC 4122 variant
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(
      16,
      20
    )}-${hex.slice(20)}`;
  }
  const rand = (n: number) =>
    Array.from({ length: n }, () => Math.floor(Math.random() * 16).toString(16)).join('');
  return `${rand(8)}-${rand(4)}-4${rand(3)}-a${rand(3)}-${rand(12)}`;
}

export function isLegalEndpointPair(source: DragEndpoint, target: DragEndpoint): boolean {
  if (source.kind === 'network' && target.kind === 'network') {
    return false;
  }
  return true;
}

export function createDragLinkStore(): DragLinkStore {
  const store = writable<DragLinkSnapshot>(emptySnapshot());
  let keyFactory = defaultIdempotencyKey;

  return {
    subscribe: store.subscribe,
    start({ source, pointer }) {
      store.set({
        state: 'port_pressed',
        source,
        target: null,
        pointer: pointer ? { x: pointer.x, y: pointer.y } : null,
        idempotencyKey: keyFactory(),
        styleOverride: null,
      });
    },
    move({ pointer, target, nearTarget }) {
      store.update((snapshot) => {
        if (snapshot.state === 'idle' || snapshot.state === 'confirming') {
          return snapshot;
        }
        let nextState: DragLinkState = snapshot.state;
        if (snapshot.state === 'port_pressed') {
          nextState = 'dragging';
        }
        const nextTarget = target === undefined ? snapshot.target : target;
        const pairLegal =
          snapshot.source && nextTarget ? isLegalEndpointPair(snapshot.source, nextTarget) : true;
        if (nearTarget === true && pairLegal) {
          nextState = 'near_target';
        } else if (
          (nearTarget === false || (nearTarget === true && !pairLegal)) &&
          nextState === 'near_target'
        ) {
          nextState = 'dragging';
        }
        return {
          ...snapshot,
          state: nextState,
          pointer: { x: pointer.x, y: pointer.y },
          target: pairLegal ? nextTarget : null,
        };
      });
    },
    hover(target) {
      store.update((snapshot) => {
        if (snapshot.state === 'idle' || snapshot.state === 'confirming') {
          return snapshot;
        }
        if (target && snapshot.source && !isLegalEndpointPair(snapshot.source, target)) {
          return { ...snapshot, target: null };
        }
        return { ...snapshot, target };
      });
    },
    enterTargetZone() {
      store.update((snapshot) => {
        if (snapshot.state !== 'dragging') return snapshot;
        if (
          snapshot.source &&
          snapshot.target &&
          !isLegalEndpointPair(snapshot.source, snapshot.target)
        ) {
          return snapshot;
        }
        return { ...snapshot, state: 'near_target' };
      });
    },
    leaveTargetZone() {
      store.update((snapshot) => {
        if (snapshot.state === 'near_target') {
          return { ...snapshot, state: 'dragging', target: null };
        }
        return snapshot;
      });
    },
    release({ pointer, targetReachable }) {
      store.update((snapshot) => {
        if (snapshot.state === 'idle' || snapshot.state === 'confirming') {
          return snapshot;
        }
        if (
          targetReachable &&
          snapshot.target &&
          snapshot.source &&
          isLegalEndpointPair(snapshot.source, snapshot.target)
        ) {
          return {
            ...snapshot,
            state: 'confirming',
            pointer: { x: pointer.x, y: pointer.y },
          };
        }
        return emptySnapshot();
      });
    },
    confirm(args) {
      store.update((snapshot) => {
        if (snapshot.state !== 'confirming') {
          return snapshot;
        }
        return {
          ...snapshot,
          styleOverride: args?.styleOverride ?? null,
        };
      });
    },
    cancel() {
      store.set(emptySnapshot());
    },
    reset() {
      store.set(emptySnapshot());
    },
    setKeyFactory(factory) {
      keyFactory = factory;
    },
  };
}

export const dragLinkStore: DragLinkStore = createDragLinkStore();

export function getDragLinkSnapshot(): DragLinkSnapshot {
  return get(dragLinkStore);
}
