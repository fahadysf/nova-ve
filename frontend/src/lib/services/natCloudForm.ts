// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

// Client-side NAT-Cloud checks (#320). They give instant field feedback and
// mirror the backend rules; the server stays authoritative.

/** Last offset of the static range; DHCP scopes start after it. */
const STATIC_LAST_OFFSET = 99;
const DHCP_FIRST_OFFSET = 100;

export type NatField = 'cidr' | 'gateway' | 'dhcp_start' | 'dhcp_end' | 'egress_interface';
export type NatErrors = Partial<Record<NatField, string>>;

export interface NatForm {
  cidr: string;
  gateway: string;
  dhcp: boolean;
  dhcpStart: string;
  dhcpEnd: string;
}

export function parseIpv4(value: string): number | null {
  const match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(value.trim());
  if (!match) return null;
  const octets = match.slice(1).map(Number);
  if (octets.some((octet) => octet > 255)) return null;
  return octets.reduce((total, octet) => total * 256 + octet, 0);
}

export function formatIpv4(value: number): string {
  return [24, 16, 8, 0].map((shift) => Math.floor(value / 2 ** shift) % 256).join('.');
}

export interface Cidr {
  network: number;
  broadcast: number;
  prefix: number;
}

export function parseCidr(value: string): Cidr | string {
  const [address, prefixText, extra] = value.trim().split('/');
  const ip = parseIpv4(address ?? '');
  const prefix = /^\d{1,2}$/.test(prefixText ?? '') ? Number(prefixText) : NaN;
  if (ip === null || extra !== undefined || !(prefix >= 0 && prefix <= 32)) {
    return 'Enter an IPv4 subnet such as 10.20.0.0/24.';
  }
  const size = 2 ** (32 - prefix);
  const network = ip - (ip % size);
  if (network !== ip) return `Host bits are set. Did you mean ${formatIpv4(network)}/${prefix}?`;
  return { network, broadcast: network + size - 1, prefix };
}

/** Addresses the server fills in for blank fields, shown as placeholders. */
export function natDefaults(cidrText: string): { gateway: string; dhcpStart: string; dhcpEnd: string } | null {
  const cidr = parseCidr(cidrText);
  if (typeof cidr === 'string' || cidr.prefix > 24) return null;
  return {
    gateway: formatIpv4(cidr.network + 1),
    dhcpStart: formatIpv4(cidr.network + DHCP_FIRST_OFFSET),
    dhcpEnd: formatIpv4(cidr.broadcast - 1)
  };
}

/** Field errors for what the user has typed so far. Blank means "use default". */
export function validateNatForm(form: NatForm): NatErrors {
  const errors: NatErrors = {};
  let cidr: Cidr | null = null;
  if (form.cidr.trim()) {
    const parsed = parseCidr(form.cidr);
    if (typeof parsed === 'string') errors.cidr = parsed;
    else if (parsed.prefix > 24) errors.cidr = 'NAT-Cloud subnets must be /24 or larger.';
    else cidr = parsed;
  }

  const address = (field: NatField, text: string): number | null => {
    if (!text.trim()) return null;
    const ip = parseIpv4(text);
    if (ip === null) errors[field] = 'Enter an IPv4 address.';
    else if (cidr && (ip < cidr.network || ip > cidr.broadcast)) errors[field] = 'Must be inside the subnet.';
    else return ip;
    return null;
  };

  const gateway = address('gateway', form.gateway);
  if (gateway !== null && cidr && (gateway === cidr.network || gateway === cidr.broadcast)) {
    errors.gateway = 'Cannot be the network or broadcast address.';
  }

  if (form.dhcp) {
    const start = address('dhcp_start', form.dhcpStart);
    const end = address('dhcp_end', form.dhcpEnd);
    if (start !== null && cidr && start <= cidr.network + STATIC_LAST_OFFSET) {
      errors.dhcp_start = `Start at ${formatIpv4(cidr.network + DHCP_FIRST_OFFSET)} or later; lower addresses are reserved for static use.`;
    }
    const effectiveStart = start ?? (cidr ? cidr.network + DHCP_FIRST_OFFSET : null);
    const effectiveEnd = end ?? (cidr ? cidr.broadcast - 1 : null);
    if (!errors.dhcp_start && !errors.dhcp_end && effectiveStart !== null && effectiveEnd !== null && effectiveStart > effectiveEnd) {
      errors.dhcp_end = 'End must not be lower than start.';
    }
  }
  return errors;
}

/** Map a server error's ``field`` (e.g. ``config.cidr``) to a form field. */
export function natFieldFromServer(field: string | undefined): NatField | null {
  const name = field?.replace(/^config\./, '');
  return name === 'cidr' || name === 'gateway' || name === 'dhcp_start' || name === 'dhcp_end' || name === 'egress_interface'
    ? name
    : null;
}
