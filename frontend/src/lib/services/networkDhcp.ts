// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { bridgeDhcpCidr } from '$lib/services/bridgeForm';
import { networkSharing } from '$lib/services/networkSharing';
import type { NetworkData } from '$lib/types';

/** Told whenever DHCP is turned off: leases already handed out stay valid. */
export const LEASES_KEPT_NOTE = 'Nodes keep their addresses until the lease expires (12 h) or they renew.';

/** A hand-written lab file may omit the type; the backend treats that as a bridge. */
export function networkKind(network: NetworkData): string {
  return network.type ?? 'linux_bridge';
}

/** The saved DHCP setting: on by default for NAT-Clouds, off for bridges (#325). */
export function dhcpEnabled(network: NetworkData): boolean {
  return networkKind(network) === 'nat_cloud' ? network.config?.dhcp !== false : network.config?.dhcp === true;
}

/** NAT-Clouds and lab bridges have settings; point-to-point and host networks do not. */
export function networkConfigurable(network: NetworkData): boolean {
  if (network.implicit === true) return false;
  const kind = networkKind(network);
  return kind === 'nat_cloud' || kind === 'linux_bridge';
}

/**
 * Whether the context menu offers to turn DHCP on or off, and which (#325).
 * Only the owning lab changes it, and a bridge needs a subnet that can carry it.
 */
export function dhcpToggle(network: NetworkData): { enabled: boolean } | null {
  if (!networkConfigurable(network) || networkSharing(network).role === 'member') return null;
  if (networkKind(network) === 'linux_bridge') {
    const cidr = network.config?.cidr;
    if (typeof cidr !== 'string' || !bridgeDhcpCidr(cidr)) return null;
  }
  return { enabled: dhcpEnabled(network) };
}
