// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { bridgeDhcpCidr } from '$lib/services/bridgeForm';
import { networkSharing } from '$lib/services/networkSharing';
import type { NetworkData } from '$lib/types';

/** Told whenever DHCP is turned off: leases already handed out stay valid. */
export const LEASES_KEPT_NOTE = 'Nodes keep their addresses until the lease expires (12 h) or they renew.';

/** A bridge segment may already have a DHCP server, such as a lab router (#325). */
export const ONE_DHCP_SERVER_NOTE = 'Turn off DHCP on any node attached here, such as a lab router.';

/** A hand-written lab file may omit the type; the backend treats that as a bridge. */
export function networkKind(network: NetworkData): string {
  return network.type ?? 'linux_bridge';
}

/** Settings a bridge needs before its DHCP server runs, as the backend checks them. */
const BRIDGE_DHCP_KEYS = ['cidr', 'dhcp_server', 'dhcp_start', 'dhcp_end'];

/**
 * The saved DHCP setting: on by default for NAT-Clouds, off for bridges (#325).
 * A bridge's bare ``dhcp: true`` from before #325 has no server, so the
 * backend treats it as off; so does the UI, which then offers to enable it.
 */
export function dhcpEnabled(network: NetworkData): boolean {
  const config = network.config ?? {};
  if (networkKind(network) === 'nat_cloud') return config.dhcp !== false;
  return config.dhcp === true && BRIDGE_DHCP_KEYS.every((key) => Boolean(config[key]));
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
