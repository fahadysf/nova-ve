// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Client-side linux_bridge checks (#325). They give instant field feedback and
// mirror the backend rules; the server stays authoritative.

import { formatIpv4, parseCidr, parseIpv4, type Cidr } from '$lib/services/natCloudForm';

/** DHCP is offered on subnets from /22 (1024 addresses) to /29 (8 addresses). */
export const BRIDGE_DHCP_MIN_PREFIX = 22;
export const BRIDGE_DHCP_MAX_PREFIX = 29;

export type BridgeField = 'cidr' | 'dhcp' | 'dhcp_server' | 'dhcp_start' | 'dhcp_end';
export type BridgeErrors = Partial<Record<BridgeField, string>>;

export interface BridgeForm {
  cidr: string;
  dhcp: boolean;
  dhcpServer: string;
  dhcpStart: string;
  dhcpEnd: string;
}

export interface BridgeDefaults {
  server: string;
  start: string;
  end: string;
}

/** The parsed subnet when it can carry DHCP, else ``null``. */
export function bridgeDhcpCidr(cidrText: string): Cidr | null {
  if (!cidrText.trim()) return null;
  const cidr = parseCidr(cidrText);
  if (typeof cidr === 'string') return null;
  return cidr.prefix >= BRIDGE_DHCP_MIN_PREFIX && cidr.prefix <= BRIDGE_DHCP_MAX_PREFIX ? cidr : null;
}

function defaultsFor(cidr: Cidr) {
  const size = cidr.broadcast - cidr.network + 1;
  return { server: cidr.broadcast - 1, start: cidr.network + size / 2, end: cidr.broadcast - 2 };
}

/**
 * The server address and scope the backend fills in: the server is the last
 * usable address, the scope the upper half of the subnet below it. The lower
 * half stays free for static addresses.
 */
export function bridgeDefaults(cidrText: string): BridgeDefaults | null {
  const cidr = bridgeDhcpCidr(cidrText);
  if (!cidr) return null;
  const defaults = defaultsFor(cidr);
  return { server: formatIpv4(defaults.server), start: formatIpv4(defaults.start), end: formatIpv4(defaults.end) };
}

/** Why the DHCP option is not offered for this subnet; empty when it is. */
export function dhcpUnavailableReason(cidrText: string): string {
  if (!cidrText.trim()) return `Add a subnet from /${BRIDGE_DHCP_MIN_PREFIX} to /${BRIDGE_DHCP_MAX_PREFIX} to offer DHCP.`;
  const cidr = parseCidr(cidrText);
  if (typeof cidr === 'string') return 'DHCP needs a valid subnet first.';
  if (cidr.prefix < BRIDGE_DHCP_MIN_PREFIX) {
    return `DHCP is offered for /${BRIDGE_DHCP_MIN_PREFIX} to /${BRIDGE_DHCP_MAX_PREFIX} subnets; /${cidr.prefix} is too large.`;
  }
  if (cidr.prefix > BRIDGE_DHCP_MAX_PREFIX) {
    return `DHCP is offered for /${BRIDGE_DHCP_MIN_PREFIX} to /${BRIDGE_DHCP_MAX_PREFIX} subnets; /${cidr.prefix} is too small.`;
  }
  return '';
}

/** Field errors for what the user has typed so far. Blank means "use default". */
export function validateBridgeForm(form: BridgeForm): BridgeErrors {
  const errors: BridgeErrors = {};
  if (form.cidr.trim()) {
    const parsed = parseCidr(form.cidr);
    if (typeof parsed === 'string') errors.cidr = parsed;
  }
  const cidr = bridgeDhcpCidr(form.cidr);
  // DHCP settings are neither shown nor sent for a subnet that cannot carry them.
  if (!form.dhcp || !cidr) return errors;

  const defaults = defaultsFor(cidr);
  const address = (field: BridgeField, text: string, fallback: number): number | null => {
    if (!text.trim()) return fallback;
    const ip = parseIpv4(text);
    if (ip === null) errors[field] = 'Enter an IPv4 address.';
    else if (ip < cidr.network || ip > cidr.broadcast) errors[field] = 'Must be inside the subnet.';
    else if (ip === cidr.network || ip === cidr.broadcast) errors[field] = 'Cannot be the network or broadcast address.';
    else return ip;
    return null;
  };

  const server = address('dhcp_server', form.dhcpServer, defaults.server);
  const start = address('dhcp_start', form.dhcpStart, defaults.start);
  const end = address('dhcp_end', form.dhcpEnd, defaults.end);
  if (start !== null && end !== null && start > end) {
    errors.dhcp_end = 'End must not be lower than start.';
  } else if (server !== null && start !== null && end !== null && server >= start && server <= end) {
    errors.dhcp_server = 'Must be outside the DHCP range.';
  }
  return errors;
}

/** Map a server error's ``field`` (e.g. ``config.dhcp_server``) to a form field. */
export function bridgeFieldFromServer(field: string | undefined): BridgeField | null {
  const name = field?.replace(/^config\./, '');
  return name === 'cidr' || name === 'dhcp' || name === 'dhcp_server' || name === 'dhcp_start' || name === 'dhcp_end'
    ? name
    : null;
}
