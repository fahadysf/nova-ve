// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';

import {
  bridgeDefaults,
  bridgeDhcpCidr,
  bridgeFieldFromServer,
  dhcpUnavailableReason,
  validateBridgeForm
} from '$lib/services/bridgeForm';
import { dhcpEnabled, dhcpToggle, networkConfigurable } from '$lib/services/networkDhcp';
import type { NetworkData } from '$lib/types';

describe('bridge DHCP defaults (#325)', () => {
  it.each([
    ['10.1.2.0/29', { server: '10.1.2.6', start: '10.1.2.4', end: '10.1.2.5' }],
    ['10.1.2.0/28', { server: '10.1.2.14', start: '10.1.2.8', end: '10.1.2.13' }],
    ['192.0.2.0/24', { server: '192.0.2.254', start: '192.0.2.128', end: '192.0.2.253' }],
    ['10.4.0.0/22', { server: '10.4.3.254', start: '10.4.2.0', end: '10.4.3.253' }]
  ])('derives the server and scope for %s', (cidr, expected) => {
    expect(bridgeDefaults(cidr)).toEqual(expected);
    // Every default is a valid choice for its own subnet.
    expect(
      validateBridgeForm({ cidr, dhcp: true, dhcpServer: expected.server, dhcpStart: expected.start, dhcpEnd: expected.end })
    ).toEqual({});
  });

  it.each(['', 'abc', '10.1.2.5/24', '10.0.0.0/21', '10.1.2.0/30', '10.1.2.0/32'])(
    'offers no DHCP for %j',
    (cidr) => {
      expect(bridgeDhcpCidr(cidr)).toBeNull();
      expect(bridgeDefaults(cidr)).toBeNull();
      expect(dhcpUnavailableReason(cidr)).not.toBe('');
    }
  );

  it('explains why DHCP is unavailable', () => {
    expect(dhcpUnavailableReason('')).toBe('Add a subnet from /22 to /29 to offer DHCP.');
    expect(dhcpUnavailableReason('10.1')).toBe('DHCP needs a valid subnet first.');
    expect(dhcpUnavailableReason('10.0.0.0/21')).toMatch(/\/21 is too large/);
    expect(dhcpUnavailableReason('10.1.2.0/30')).toMatch(/\/30 is too small/);
    expect(dhcpUnavailableReason('10.1.0.0/22')).toBe('');
    expect(dhcpUnavailableReason('10.1.2.0/29')).toBe('');
  });
});

describe('bridge form rules (#325)', () => {
  const base = { cidr: '192.0.2.0/24', dhcp: true, dhcpServer: '', dhcpStart: '', dhcpEnd: '' };

  it('accepts a bridge without a subnet, and any valid subnet without DHCP', () => {
    expect(validateBridgeForm({ ...base, cidr: '' })).toEqual({});
    expect(validateBridgeForm({ ...base, cidr: '10.0.0.0/16', dhcp: false })).toEqual({});
    // DHCP values are ignored while the subnet cannot carry DHCP.
    expect(validateBridgeForm({ ...base, cidr: '10.0.0.0/16', dhcpServer: 'junk' })).toEqual({});
  });

  it.each([
    [{ cidr: 'abc' }, 'cidr'],
    [{ cidr: '192.0.2.7/24' }, 'cidr'],
    [{ dhcpServer: 'x' }, 'dhcp_server'],
    [{ dhcpServer: '198.51.100.1' }, 'dhcp_server'],
    [{ dhcpServer: '192.0.2.255' }, 'dhcp_server'],
    [{ dhcpServer: '192.0.2.200' }, 'dhcp_server'],
    [{ dhcpStart: '192.0.2.0' }, 'dhcp_start'],
    [{ dhcpEnd: '192.0.2.255' }, 'dhcp_end'],
    [{ dhcpStart: '192.0.2.200', dhcpEnd: '192.0.2.150' }, 'dhcp_end']
  ])('rejects %o on %s', (change, field) => {
    expect(Object.keys(validateBridgeForm({ ...base, ...change }))).toEqual([field]);
  });

  it('checks a custom server against the default scope', () => {
    expect(validateBridgeForm({ ...base, dhcpServer: '192.0.2.1' })).toEqual({});
    expect(validateBridgeForm({ ...base, dhcpStart: '192.0.2.10', dhcpEnd: '192.0.2.20' })).toEqual({});
    expect(validateBridgeForm({ ...base, dhcpServer: '192.0.2.15', dhcpStart: '192.0.2.10', dhcpEnd: '192.0.2.20' }))
      .toEqual({ dhcp_server: 'Must be outside the DHCP range.' });
  });

  it('maps server fields', () => {
    expect(bridgeFieldFromServer('config.dhcp_server')).toBe('dhcp_server');
    expect(bridgeFieldFromServer('config.dhcp')).toBe('dhcp');
    expect(bridgeFieldFromServer('config.gateway')).toBeNull();
    expect(bridgeFieldFromServer(undefined)).toBeNull();
  });
});

describe('network DHCP state (#325)', () => {
  const net = (overrides: Partial<NetworkData> = {}): NetworkData => ({
    id: 7, name: 'core', type: 'linux_bridge', left: 0, top: 0, icon: '', visibility: true, config: {}, ...overrides
  });

  it('defaults DHCP on for NAT-Clouds and off for bridges', () => {
    expect(dhcpEnabled(net({ type: 'nat_cloud' }))).toBe(true);
    expect(dhcpEnabled(net({ type: 'nat_cloud', config: { dhcp: false } }))).toBe(false);
    expect(dhcpEnabled(net())).toBe(false);
    expect(dhcpEnabled(net({ config: { dhcp: true } }))).toBe(true);
  });

  it.each([
    ['owner NAT-Cloud', net({ type: 'nat_cloud' }), { enabled: true }],
    ['NAT-Cloud with DHCP off', net({ type: 'nat_cloud', config: { dhcp: false } }), { enabled: false }],
    ['bridge with a /24', net({ config: { cidr: '192.0.2.0/24' } }), { enabled: false }],
    ['bridge with DHCP on', net({ config: { cidr: '10.1.2.0/29', dhcp: true } }), { enabled: true }],
    ['untyped bridge', net({ type: undefined as never, config: { cidr: '10.1.0.0/22' } }), { enabled: false }],
    ['bridge without a subnet', net(), null],
    ['bridge with a /21', net({ config: { cidr: '10.0.0.0/21' } }), null],
    ['bridge with a /30', net({ config: { cidr: '10.1.2.0/30' } }), null],
    ['Bridge-Cloud', net({ type: 'bridge_cloud', config: { cidr: '192.0.2.0/24' } }), null],
    ['point-to-point', net({ implicit: true, config: { cidr: '192.0.2.0/24' } }), null],
    ['shared bridge member', net({ config: { cidr: '192.0.2.0/24', shared_network_id: 'shared-net:b:1' } }), null],
    ['shared NAT-Cloud member', net({ type: 'nat_cloud', config: { shared_cloud_id: 'nat-cloud:b:1' } }), null]
  ])('menu toggle for %s', (_label, network, expected) => {
    expect(dhcpToggle(network)).toEqual(expected);
  });

  it('offers Configure on NAT-Clouds and lab bridges only', () => {
    expect(networkConfigurable(net())).toBe(true);
    expect(networkConfigurable(net({ type: 'nat_cloud' }))).toBe(true);
    expect(networkConfigurable(net({ type: 'bridge_cloud' }))).toBe(false);
    expect(networkConfigurable(net({ type: 'pnet0' }))).toBe(false);
    expect(networkConfigurable(net({ implicit: true }))).toBe(false);
  });
});
