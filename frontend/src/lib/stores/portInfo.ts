// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { writable, get } from 'svelte/store';
import type { Writable } from 'svelte/store';

export interface InterfacePortInfo {
  kind: 'interface';
  nodeId: number;
  interfaceIndex: number;
  anchorRect: DOMRect | null;
  interfaceName: string;
  plannedMac: string | null;
}

export interface NetworkPortInfo {
  kind: 'network';
  networkId: number;
  side: 'top' | 'right' | 'bottom' | 'left';
  offset: number;
  anchorRect: DOMRect | null;
  networkName: string | null;
}

export type SelectedPortInfo = InterfacePortInfo | NetworkPortInfo;

export const selectedPortInfoStore: Writable<SelectedPortInfo | null> = writable(null);

export function togglePortInfo(next: SelectedPortInfo): void {
  const current = get(selectedPortInfoStore);
  if (current && isSamePort(current, next)) {
    selectedPortInfoStore.set(null);
    return;
  }
  selectedPortInfoStore.set(next);
}

export function isSamePort(a: SelectedPortInfo, b: SelectedPortInfo): boolean {
  if (a.kind !== b.kind) return false;
  if (a.kind === 'interface' && b.kind === 'interface') {
    return a.nodeId === b.nodeId && a.interfaceIndex === b.interfaceIndex;
  }
  if (a.kind === 'network' && b.kind === 'network') {
    return a.networkId === b.networkId && a.side === b.side && a.offset === b.offset;
  }
  return false;
}
