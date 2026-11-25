// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { NetworkData, NetworkType } from '$lib/types';

/** Add-network choices: the wire types plus "join another lab's network". */
export type NetworkModalType = NetworkType | 'shared_network';

export interface NetworkSharing {
  role: 'owner' | 'member' | null;
  canToggle: boolean;
  hint: string;
}

function configString(network: NetworkData, key: string): string {
  const value = network.config?.[key];
  return typeof value === 'string' ? value : '';
}

/** How a network takes part in sharing, and whether the toggle applies (#319). */
export function networkSharing(network: NetworkData): NetworkSharing {
  // A hand-written lab file may omit the type; the backend treats that as a bridge.
  const type = network.type ?? 'linux_bridge';
  if (configString(network, type === 'nat_cloud' ? 'shared_cloud_id' : 'shared_network_id')) {
    return {
      role: 'member',
      canToggle: false,
      hint: 'This network belongs to another lab. Change sharing there.'
    };
  }
  if (type === 'bridge_cloud' || type.startsWith('pnet')) {
    return {
      role: null,
      canToggle: false,
      hint: 'Host-attached networks already span labs.'
    };
  }
  return network.shared === true
    ? { role: 'owner', canToggle: true, hint: 'Other labs can use this network. Click to stop sharing.' }
    : { role: null, canToggle: true, hint: 'Let other labs connect nodes to this network.' };
}
