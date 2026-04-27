// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

import { routeOrthogonal } from './orthogonal';
import { routeBezier } from './bezier';
import { routeStraight } from './straight';

export type Style = 'orthogonal' | 'bezier' | 'straight';

export interface PortRef {
  x: number;
  y: number;
  side: 'top' | 'right' | 'bottom' | 'left';
}

export interface Obstacle {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface RouteRequest {
  from: PortRef;
  to: PortRef;
  obstacles: Obstacle[];
  style: Style;
}

export interface RouteResult {
  d: string;
  bends: number;
  lengthPx: number;
}

export interface RouterService {
  route(req: RouteRequest, opts?: { signal?: AbortSignal }): Promise<RouteResult>;
}

class DefaultRouter implements RouterService {
  async route(req: RouteRequest, opts?: { signal?: AbortSignal }): Promise<RouteResult> {
    if (opts?.signal?.aborted) {
      throw new DOMException('Route aborted', 'AbortError');
    }
    switch (req.style) {
      case 'orthogonal':
        return routeOrthogonal(req, opts?.signal);
      case 'bezier':
        return routeBezier(req);
      case 'straight':
        return routeStraight(req);
    }
  }
}

export const defaultRouter: RouterService = new DefaultRouter();

export class RouterScheduler {
  private inflight = new Map<string, AbortController>();
  private router: RouterService;

  constructor(router: RouterService = defaultRouter) {
    this.router = router;
  }

  async route(linkId: string, req: RouteRequest): Promise<RouteResult> {
    const prev = this.inflight.get(linkId);
    if (prev) {
      prev.abort();
    }
    const controller = new AbortController();
    this.inflight.set(linkId, controller);
    try {
      const result = await this.router.route(req, { signal: controller.signal });
      // Only clear if we are still the current controller for this link.
      if (this.inflight.get(linkId) === controller) {
        this.inflight.delete(linkId);
      }
      return result;
    } catch (err) {
      if (this.inflight.get(linkId) === controller) {
        this.inflight.delete(linkId);
      }
      throw err;
    }
  }

  cancel(linkId: string): void {
    const ctrl = this.inflight.get(linkId);
    if (ctrl) {
      ctrl.abort();
      this.inflight.delete(linkId);
    }
  }

  cancelAll(): void {
    for (const ctrl of this.inflight.values()) {
      ctrl.abort();
    }
    this.inflight.clear();
  }

  size(): number {
    return this.inflight.size;
  }
}

export const defaultScheduler = new RouterScheduler();
