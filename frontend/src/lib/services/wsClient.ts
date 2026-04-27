// Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
// SPDX-License-Identifier: Apache-2.0

/**
 * wsClient — US-073. Reconnecting WebSocket client for the lab WS endpoint
 * `/ws/labs/{labId}`. Tracks ``last_seq`` in sessionStorage, applies a
 * deterministic exponential backoff on reconnect, gates connectivity on tab
 * visibility, and exposes a fresh ``AbortController`` after every
 * ``lab_topology`` event so subscribers can re-arm long-running computation.
 *
 * The factory does NOT auto-connect — callers invoke ``connect()`` once they
 * have wired up subscriptions.
 */

export type WsMessage = {
  seq?: number;
  type: string;
  rev?: string;
  payload?: unknown;
};

export type WsHandler = (msg: WsMessage) => void;

export type WsClient = {
  connect: () => void;
  close: () => void;
  /**
   * Subscribe to messages of ``type``. Returns an unsubscriber.
   *
   * Special pseudo-types ``__open`` and ``__close`` fire on socket lifecycle
   * transitions (their ``payload`` is unused).
   */
  on: (type: string, handler: WsHandler) => () => void;
  readonly lastSeq: number;
  /**
   * AbortController for in-flight derived computation. The client calls
   * ``.abort()`` and allocates a fresh controller on every incoming
   * ``lab_topology`` event. Subscribers MUST re-read this property by
   * reference after handling a ``lab_topology`` message — the previous
   * controller is no longer the live one.
   */
  readonly abortController: AbortController;
  readonly isOpen: boolean;
};

export type WsClientOptions = {
  labId: string;
  /**
   * Optional override for protocol+host. When omitted, defaults to
   * ``window.location`` (with ``ws:``/``wss:`` mirroring ``http:``/``https:``).
   */
  baseUrl?: string;
};

/**
 * Deterministic exponential backoff schedule used by ``connect()`` after a
 * socket close. No jitter (intentional — easier to test; jitter can be added
 * once we observe stampede behaviour in production).
 */
export function getBackoffDelay(attempt: number): number {
  const schedule = [500, 1000, 2000, 5000, 10000, 30000];
  if (attempt < 0) return schedule[0];
  if (attempt >= schedule.length) return schedule[schedule.length - 1];
  return schedule[attempt];
}

function readStoredLastSeq(labId: string): number {
  if (typeof window === 'undefined') return 0;
  try {
    const raw = window.sessionStorage.getItem(`ws:last_seq:${labId}`);
    if (raw === null) return 0;
    const parsed = parseInt(raw, 10);
    return Number.isFinite(parsed) ? parsed : 0;
  } catch {
    return 0;
  }
}

function writeStoredLastSeq(labId: string, value: number): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(`ws:last_seq:${labId}`, String(value));
  } catch {
    // SSR / private-mode storage failures are non-fatal.
  }
}

function buildUrl(labId: string, lastSeq: number, baseUrl?: string): string {
  let protocol = 'ws:';
  let host = 'localhost';
  if (baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      protocol = parsed.protocol === 'https:' ? 'wss:' : 'ws:';
      host = parsed.host;
    } catch {
      // fall through to window.location below
    }
  }
  if (!baseUrl && typeof window !== 'undefined' && window.location) {
    protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    host = window.location.host || host;
  }
  // Lab identifiers may carry slashes (folder paths); URL-encode them.
  const encodedLab = encodeURIComponent(labId);
  return `${protocol}//${host}/ws/labs/${encodedLab}?last_seq=${lastSeq}`;
}

export function createWsClient(opts: WsClientOptions): WsClient {
  const { labId, baseUrl } = opts;
  const handlers = new Map<string, Set<WsHandler>>();

  let lastSeq = readStoredLastSeq(labId);
  let abortController = new AbortController();
  let socket: WebSocket | null = null;
  let attempt = 0;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let manuallyClosed = false;
  let pausedForVisibility = false;
  /** Set when we've seen ``force_resnapshot``; cleared after we apply the next ``lab_topology`` snapshot. */
  let pendingResnapshot = false;

  let visibilityListener: (() => void) | null = null;

  function emit(type: string, msg: WsMessage): void {
    const set = handlers.get(type);
    if (!set) return;
    for (const handler of Array.from(set)) {
      try {
        handler(msg);
      } catch {
        // Handler errors are isolated; one bad subscriber must not break the others.
      }
    }
  }

  function clearReconnectTimer() {
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  }

  function scheduleReconnect(delayMs: number) {
    clearReconnectTimer();
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      openSocket();
    }, delayMs);
  }

  function persistSeq(value: number) {
    lastSeq = value;
    writeStoredLastSeq(labId, value);
  }

  function handleMessage(raw: string) {
    let msg: WsMessage;
    try {
      msg = JSON.parse(raw) as WsMessage;
    } catch {
      return;
    }

    if (msg.type === 'force_resnapshot') {
      pendingResnapshot = true;
      emit(msg.type, msg);
      return;
    }

    if (typeof msg.seq === 'number' && Number.isFinite(msg.seq)) {
      // ``force_resnapshot`` is followed by a ``lab_topology`` snapshot whose
      // seq becomes the new lastSeq; ordinary lab_topology events without a
      // preceding force_resnapshot still update lastSeq via the seq field.
      persistSeq(msg.seq);
      if (pendingResnapshot && msg.type === 'lab_topology') {
        pendingResnapshot = false;
      }
    }

    if (msg.type === 'lab_topology') {
      // Abort any derived computation running against the previous snapshot
      // BEFORE invoking subscribers, so they observe the old controller as
      // aborted and re-read the fresh one after they finish.
      abortController.abort();
      abortController = new AbortController();
    }

    emit(msg.type, msg);
  }

  function openSocket() {
    if (typeof WebSocket === 'undefined') return;
    if (manuallyClosed || pausedForVisibility) return;
    if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
      return;
    }
    const url = buildUrl(labId, lastSeq, baseUrl);
    let nextSocket: WebSocket;
    try {
      nextSocket = new WebSocket(url);
    } catch {
      // Constructor failure — schedule a backoff retry.
      scheduleReconnect(getBackoffDelay(attempt));
      attempt += 1;
      return;
    }
    socket = nextSocket;

    nextSocket.addEventListener('open', () => {
      attempt = 0;
      emit('__open', { type: '__open' });
    });
    nextSocket.addEventListener('message', (event: MessageEvent) => {
      const data = typeof event.data === 'string' ? event.data : '';
      if (data) handleMessage(data);
    });
    const handleClosure = () => {
      socket = null;
      emit('__close', { type: '__close' });
      if (manuallyClosed || pausedForVisibility) return;
      scheduleReconnect(getBackoffDelay(attempt));
      attempt += 1;
    };
    nextSocket.addEventListener('close', handleClosure);
    nextSocket.addEventListener('error', () => {
      // ``error`` is followed by ``close``; let the close handler drive reconnect.
    });
  }

  function attachVisibilityListener() {
    if (typeof document === 'undefined') return;
    if (visibilityListener) return;
    const listener = () => {
      if (document.hidden) {
        pausedForVisibility = true;
        clearReconnectTimer();
        if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
          try {
            socket.close();
          } catch {
            // ignore
          }
        }
        socket = null;
      } else {
        if (!pausedForVisibility) return;
        pausedForVisibility = false;
        // Schedule an immediate reconnect; preserve the attempt counter so
        // genuine server outages don't fast-cycle.
        scheduleReconnect(0);
      }
    };
    document.addEventListener('visibilitychange', listener);
    visibilityListener = () => {
      document.removeEventListener('visibilitychange', listener);
    };
  }

  function detachVisibilityListener() {
    if (visibilityListener) {
      visibilityListener();
      visibilityListener = null;
    }
  }

  return {
    connect() {
      manuallyClosed = false;
      pausedForVisibility = false;
      attempt = 0;
      attachVisibilityListener();
      openSocket();
    },
    close() {
      manuallyClosed = true;
      clearReconnectTimer();
      detachVisibilityListener();
      if (socket) {
        try {
          socket.close();
        } catch {
          // ignore
        }
        socket = null;
      }
    },
    on(type, handler) {
      let set = handlers.get(type);
      if (!set) {
        set = new Set();
        handlers.set(type, set);
      }
      set.add(handler);
      return () => {
        const current = handlers.get(type);
        if (!current) return;
        current.delete(handler);
        if (current.size === 0) {
          handlers.delete(type);
        }
      };
    },
    get lastSeq() {
      return lastSeq;
    },
    get abortController() {
      return abortController;
    },
    get isOpen() {
      return socket !== null && socket.readyState === WebSocket.OPEN;
    },
  };
}
