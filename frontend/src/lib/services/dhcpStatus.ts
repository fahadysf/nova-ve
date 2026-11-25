// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { NetworkInfo } from '$lib/types';

export type DhcpInfo = NonNullable<NonNullable<NetworkInfo['ip']>['dhcp']>;

export interface DhcpStatus {
  label: string;
  /** Dot colour class. */
  dot: string;
  /** Set when the saved setting and the live server disagree. */
  problem: string;
}

/** Live DHCP server state compared with the saved setting (#320). */
export function dhcpStatus(dhcp: DhcpInfo): DhcpStatus {
  if (dhcp.enabled) {
    return dhcp.running
      ? { label: 'Running', dot: 'bg-emerald-400', problem: '' }
      : {
          label: 'Not running',
          dot: 'bg-amber-400',
          problem: 'DHCP is enabled, but its server is not running. Reopen the lab to start it.'
        };
  }
  return dhcp.running
    ? {
        label: 'Still running',
        dot: 'bg-red-400',
        problem: 'DHCP is disabled, but its server is still running. Reopen the lab to stop it.'
      }
    : { label: 'Disabled', dot: 'bg-gray-500', problem: '' };
}

/** Short remaining time for a lease, e.g. "11 h 58 min left". */
export function leaseRemaining(expiresAt: string | null, now = Date.now()): string {
  if (!expiresAt) return 'no expiry';
  const minutes = Math.round((new Date(expiresAt).getTime() - now) / 60_000);
  if (Number.isNaN(minutes)) return '';
  if (minutes < 1) return 'expiring';
  if (minutes < 60) return `${minutes} min left`;
  const rest = minutes % 60;
  return rest ? `${Math.floor(minutes / 60)} h ${rest} min left` : `${minutes / 60} h left`;
}
