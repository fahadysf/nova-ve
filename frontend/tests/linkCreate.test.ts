// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { describe, expect, it } from 'vitest';
import { createdFromLinkResponse } from '$lib/services/linkCreate';
import type { Link, NetworkData } from '$lib/types';

const created: Link = {
  id: 'lnk_7',
  from: { node_id: 1, interface_index: 0 },
  to: { network_id: 9 },
  style_override: null,
  label: '',
  color: '',
  width: '1'
};

const bridge: NetworkData = {
  id: 21,
  name: 'net-21',
  type: 'bridge',
  left: 0,
  top: 0,
  icon: 'Cloud.png',
  visibility: false,
  implicit: true,
  count: 2
};

describe('createdFromLinkResponse (#306)', () => {
  it('reads the link and network the create endpoint returns at the top level', () => {
    const response = {
      code: 201,
      status: 'success',
      message: 'Link created successfully.',
      link: created,
      network: bridge,
      revision: 6
    };

    expect(createdFromLinkResponse(response)).toEqual({ link: created, network: bridge });
  });

  it('returns no network when the create did not make one', () => {
    const response = { code: 201, status: 'success', message: 'ok', link: created, revision: 6 };

    expect(createdFromLinkResponse(response)).toEqual({ link: created, network: null });
  });

  it('reports a response that carries no usable link instead of inventing one', () => {
    expect(createdFromLinkResponse(undefined)).toEqual({ link: null, network: null });
    expect(createdFromLinkResponse({ data: created })).toEqual({ link: null, network: null });
    expect(createdFromLinkResponse({ link: { ...created, id: '' } })).toEqual({
      link: null,
      network: null
    });
  });
});
