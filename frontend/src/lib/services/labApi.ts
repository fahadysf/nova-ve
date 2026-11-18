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

/**
 * #245: raised when the server rejects a guarded write with 412 — the lab
 * changed elsewhere (another tab, another client) since the client last
 * observed it. Carries the server's current revision so callers can
 * re-adopt the snapshot and retry.
 */
export class LayoutConflictError extends Error {
  readonly serverRevision: number | null;

  constructor(message: string, serverRevision: number | null) {
    super(message);
    this.name = 'LayoutConflictError';
    this.serverRevision = serverRevision;
  }
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
    init: {
      method: 'PUT';
      body: LayoutPayload;
      suppressToast?: boolean;
      /** #245: If-Match token; the default requester maps it to a header. */
      ifMatch?: string;
    }
  ) => Promise<unknown>;
  /** Optional error sink so callers can surface failures (e.g. toasts). */
  onError?: (error: unknown) => void;
  /**
   * #245: current optimistic-concurrency revision (from labState). When it
   * returns a value it is sent as If-Match; the backend answers 412 on
   * mismatch instead of silently accepting a stale write.
   */
  versionProvider?: () => string | number | null | undefined;
  /** Called after a successful PUT with the parsed response body. */
  onApplied?: (result: unknown) => void;
}

export interface LayoutDebouncer {
  pushNodePosition(id: number, left: number, top: number): void;
  pushNetworkPosition(id: number, left: number, top: number): void;
  pushViewport(viewport: LabViewport): void;
  pushDefaultLinkStyle(style: LinkStyle): void;
  flush(): Promise<void>;
  /**
   * Fire the pending payload (if any) with `fetch(..., {keepalive: true})`
   * without awaiting — for `beforeunload`, where only synchronous initiation
   * survives the page teardown.
   */
  flushKeepalive(): void;
  cancel(): void;
  pending(): boolean;
  /**
   * Ids whose positions are queued or currently in flight. Callers use this
   * to keep local (unsaved) coordinates from being clobbered by an incoming
   * server snapshot racing the debounced PUT.
   */
  pendingPositionIds(): { nodeIds: Set<number>; networkIds: Set<number> };
}

interface PendingState {
  nodes: Map<number, LayoutNodePosition>;
  networks: Map<number, LayoutNetworkPosition>;
  viewport: LabViewport | null;
  linkStyle: LinkStyle | null;
}

/** Exported for tests: the fetch-based requester with If-Match/412 mapping. */
export async function defaultRequester(
  path: string,
  init: { method: 'PUT'; body: LayoutPayload; suppressToast?: boolean; ifMatch?: string }
): Promise<unknown> {
  const url = path.startsWith('/api/') ? path : `/api${path.startsWith('/') ? path : `/${path}`}`;
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (init.ifMatch !== undefined) {
    headers['If-Match'] = init.ifMatch;
  }
  const response = await fetch(url, {
    method: init.method,
    headers,
    credentials: 'include',
    body: JSON.stringify(init.body),
  });
  if (!response.ok) {
    if (response.status === 412) {
      let serverRevision: number | null = null;
      try {
        const body = (await response.json()) as { revision?: unknown };
        if (typeof body.revision === 'number') {
          serverRevision = body.revision;
        }
      } catch {
        /* body already consumed or not JSON */
      }
      throw new LayoutConflictError(
        `Lab changed elsewhere (stale revision); server is at ${serverRevision ?? 'unknown'}`,
        serverRevision
      );
    }
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
  let inflightIds: { nodeIds: Set<number>; networkIds: Set<number> } = {
    nodeIds: new Set(),
    networkIds: new Set(),
  };

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

  /**
   * Merge a failed payload back into the pending state so the next push or
   * flush retries it. Newer pushes always win: entries already queued are
   * not overwritten, and null viewport/linkStyle slots are only filled.
   */
  function requeue(payload: LayoutPayload) {
    for (const entry of payload.nodes ?? []) {
      if (!state.nodes.has(entry.id)) {
        state.nodes.set(entry.id, entry);
      }
    }
    for (const entry of payload.networks ?? []) {
      if (!state.networks.has(entry.id)) {
        state.networks.set(entry.id, entry);
      }
    }
    if (payload.viewport && state.viewport === null) {
      state.viewport = payload.viewport;
    }
    if (payload.defaults?.link_style && state.linkStyle === null) {
      state.linkStyle = payload.defaults.link_style;
    }
  }

  async function send(): Promise<void> {
    if (!hasContent(state)) {
      return;
    }
    const payload = buildPayload(state);
    state = emptyState();
    const path = `/labs/${labPath}/layout`;
    const expected = opts.versionProvider?.();
    const ifMatch = expected === null || expected === undefined ? undefined : String(expected);
    inflightIds = {
      nodeIds: new Set((payload.nodes ?? []).map((entry) => entry.id)),
      networkIds: new Set((payload.networks ?? []).map((entry) => entry.id)),
    };
    inflight = (async () => {
      try {
        const result = await requester(path, {
          method: 'PUT',
          body: payload,
          suppressToast: true,
          ifMatch,
        });
        opts.onApplied?.(result);
      } catch (error) {
        // Keep the payload queued: pending() stays true and the next push
        // or flush retries it. Dropping it here is what made a transient
        // failure silently revert the canvas on the next reload.
        requeue(payload);
        if (opts.onError) {
          opts.onError(error);
        }
      } finally {
        inflight = null;
        inflightIds = { nodeIds: new Set(), networkIds: new Set() };
      }
    })();
    await inflight;
  }

  function keepaliveUrl(): string {
    const path = `/labs/${labPath}/layout`;
    return path.startsWith('/api/') ? path : `/api${path.startsWith('/') ? path : `/${path}`}`;
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
    flushKeepalive() {
      if (!hasContent(state)) {
        return;
      }
      const payload = buildPayload(state);
      state = emptyState();
      const expected = opts.versionProvider?.();
      const headers: Record<string, string> = { 'Content-Type': 'application/json' };
      if (expected !== null && expected !== undefined) {
        headers['If-Match'] = String(expected);
      }
      try {
        void fetch(keepaliveUrl(), {
          method: 'PUT',
          headers,
          credentials: 'include',
          keepalive: true,
          body: JSON.stringify(payload),
        });
      } catch {
        // Page is tearing down; nothing further to do.
      }
    },
    cancel() {
      clearTimer();
      state = emptyState();
    },
    pending() {
      return timer !== null || hasContent(state) || inflight !== null;
    },
    pendingPositionIds() {
      const nodeIds = new Set(inflightIds.nodeIds);
      const networkIds = new Set(inflightIds.networkIds);
      for (const id of state.nodes.keys()) {
        nodeIds.add(id);
      }
      for (const id of state.networks.keys()) {
        networkIds.add(id);
      }
      return { nodeIds, networkIds };
    },
  };
}
