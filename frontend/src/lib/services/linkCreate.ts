// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import type { Link, NetworkData } from '$lib/types';

/**
 * What POST /labs/{id}/links returns. Unlike most endpoints it does not wrap
 * its payload in ``data``: the created link and, when the request created one,
 * the network ride at the top level (#306 — the canvas used to read
 * ``response.data`` and never found its link).
 */
export interface LinkCreateResponse {
  code: number;
  status: string;
  message: string;
  link?: Link;
  /** An implicit bridge for a node-to-node link, or the network a link joined. */
  network?: NetworkData | null;
  revision?: number;
}

/** The link and network a create response carries; ``null`` where there is none. */
export interface CreatedLink {
  link: Link | null;
  network: NetworkData | null;
}

export function createdFromLinkResponse(response: unknown): CreatedLink {
  const body = (response ?? {}) as Partial<LinkCreateResponse>;
  const link = body.link;
  const network = body.network;
  return {
    link: link && typeof link.id === 'string' && link.id ? link : null,
    network: network && typeof network.id === 'number' ? network : null
  };
}
