// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, apiRequest } from '$lib/api';
import { fieldError } from '$lib/services/apiErrors';

afterEach(() => vi.unstubAllGlobals());

describe('subnet conflicts (#325)', () => {
  it('keeps the field and the subnet of a 409 conflict', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      code: 409,
      status: 'fail',
      message: 'CIDR 10.20.0.0/24 overlaps a NAT-Cloud in another lab CIDR 10.20.0.0/16; a bridge that runs DHCP needs a subnet of its own.',
      field: 'config.cidr',
      conflicting_cidr: '10.20.0.0/16',
      source: 'a NAT-Cloud in another lab'
    }), { status: 409 })));

    const error = await apiRequest('/labs/a.json/networks/validate-config', {
      method: 'POST', body: {}, suppressToast: true
    }).catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      statusCode: 409, field: 'config.cidr', conflictingCidr: '10.20.0.0/16', source: 'a NAT-Cloud in another lab'
    });
    // The server already names the subnet, so nothing is added.
    expect(fieldError(error)).toEqual({ message: (error as Error).message, field: 'config.cidr' });
  });

  it('names the subnet when the message does not', () => {
    const error = new ApiError('This subnet is taken.', {
      field: 'config.cidr', conflictingCidr: '198.51.100.0/24', source: 'a DHCP bridge in another lab'
    });
    expect(fieldError(error)).toEqual({
      message: 'This subnet is taken. It overlaps a DHCP bridge in another lab (198.51.100.0/24).',
      field: 'config.cidr'
    });
  });

  it('passes other errors through', () => {
    expect(fieldError(new ApiError('Turn DHCP off before changing the network type.', { field: 'type' }))).toEqual({
      message: 'Turn DHCP off before changing the network type.', field: 'type'
    });
    expect(fieldError(new Error('offline'))).toEqual({ message: 'offline' });
    expect(fieldError('boom')).toEqual({ message: 'boom' });
  });
});
