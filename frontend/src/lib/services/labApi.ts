// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * labApi — debounced bulk PUT /api/labs/{labPath}/layout helper.
 *
 * US-070. Accumulates layout fields (node positions, network positions,
 * viewport, defaults.link_style) and flushes the merged payload after a
 * configurable quiescence window (default 500ms). The body is constructed
 * via an explicit allow-list — only ``{nodes, networks, viewport, defaults}``
 * are ever sent to the server.
 */

import type { LabViewport, LinkStyle } from '$lib/types';

export interface LayoutNodePosition {
  id: number;
  left: number;
  top: number;
}

export interface LayoutNetworkPosition {
  id: number;
  left: number;
  top: number;
}

export interface LayoutDefaults {
  link_style?: LinkStyle;
}

export interface LayoutPayload {
  nodes?: LayoutNodePosition[];
  networks?: LayoutNetworkPosition[];
  viewport?: LabViewport;
  defaults?: LayoutDefaults;
}

export interface LayoutDebouncerOptions {
  /** Quiescence window before a PUT is issued. Defaults to 500ms. */
  delayMs?: number;
  /**
   * Override the underlying ``apiRequest`` call (used in tests). Receives
   * the path and a request init equivalent.
   */
  request?: (
    path: string,
    init: { method: 'PUT'; body: LayoutPayload; suppressToast?: boolean }
  ) => Promise<unknown>;
  /** Optional error sink so callers can surface failures (e.g. toasts). */
  onError?: (error: unknown) => void;
}

export interface LayoutDebouncer {
  pushNodePosition(id: number, left: number, top: number): void;
  pushNetworkPosition(id: number, left: number, top: number): void;
  pushViewport(viewport: LabViewport): void;
  pushDefaultLinkStyle(style: LinkStyle): void;
  flush(): Promise<void>;
  cancel(): void;
  pending(): boolean;
}

interface PendingState {
  nodes: Map<number, LayoutNodePosition>;
  networks: Map<number, LayoutNetworkPosition>;
  viewport: LabViewport | null;
  linkStyle: LinkStyle | null;
}

async function defaultRequester(
  path: string,
  init: { method: 'PUT'; body: LayoutPayload; suppressToast?: boolean }
): Promise<unknown> {
  const url = path.startsWith('/api/') ? path : `/api${path.startsWith('/') ? path : `/${path}`}`;
  const response = await fetch(url, {
    method: init.method,
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify(init.body),
  });
  if (!response.ok) {
    throw new Error(`PUT ${url} failed with status ${response.status}`);
  }
  // Best-effort parse — some endpoints return an empty body on success.
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function emptyState(): PendingState {
  return {
    nodes: new Map(),
    networks: new Map(),
    viewport: null,
    linkStyle: null,
  };
}

function hasContent(state: PendingState): boolean {
  return (
    state.nodes.size > 0 ||
    state.networks.size > 0 ||
    state.viewport !== null ||
    state.linkStyle !== null
  );
}

function buildPayload(state: PendingState): LayoutPayload {
  const payload: LayoutPayload = {};
  if (state.nodes.size > 0) {
    payload.nodes = Array.from(state.nodes.values()).map((entry) => ({
      id: entry.id,
      left: entry.left,
      top: entry.top,
    }));
  }
  if (state.networks.size > 0) {
    payload.networks = Array.from(state.networks.values()).map((entry) => ({
      id: entry.id,
      left: entry.left,
      top: entry.top,
    }));
  }
  if (state.viewport !== null) {
    payload.viewport = {
      x: state.viewport.x,
      y: state.viewport.y,
      zoom: state.viewport.zoom,
    };
  }
  if (state.linkStyle !== null) {
    payload.defaults = { link_style: state.linkStyle };
  }
  return payload;
}

export function createLayoutDebouncer(
  labPath: string,
  opts: LayoutDebouncerOptions = {}
): LayoutDebouncer {
  const delayMs = opts.delayMs ?? 500;
  const requester = opts.request ?? defaultRequester;

  let state = emptyState();
  let timer: ReturnType<typeof setTimeout> | null = null;
  let inflight: Promise<void> | null = null;

  function clearTimer() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function schedule() {
    clearTimer();
    timer = setTimeout(() => {
      timer = null;
      void send();
    }, delayMs);
  }

  async function send(): Promise<void> {
    if (!hasContent(state)) {
      return;
    }
    const payload = buildPayload(state);
    state = emptyState();
    const path = `/labs/${labPath}/layout`;
    inflight = (async () => {
      try {
        await requester(path, { method: 'PUT', body: payload, suppressToast: true });
      } catch (error) {
        if (opts.onError) {
          opts.onError(error);
        }
      } finally {
        inflight = null;
      }
    })();
    await inflight;
  }

  return {
    pushNodePosition(id, left, top) {
      state.nodes.set(id, { id, left, top });
      schedule();
    },
    pushNetworkPosition(id, left, top) {
      state.networks.set(id, { id, left, top });
      schedule();
    },
    pushViewport(viewport) {
      state.viewport = { x: viewport.x, y: viewport.y, zoom: viewport.zoom };
      schedule();
    },
    pushDefaultLinkStyle(style) {
      state.linkStyle = style;
      schedule();
    },
    async flush() {
      clearTimer();
      await send();
      if (inflight) {
        await inflight;
      }
    },
    cancel() {
      clearTimer();
      state = emptyState();
    },
    pending() {
      return timer !== null || hasContent(state);
    },
  };
}
