/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Which console sessions the lab page has open, so that each can be
 * reported closed (#300).
 *
 * The server sees a console open — the lab page loads the HTML5 console
 * endpoint — but has no other way to learn that one closed: closing a tab
 * only unloads an iframe. This tracker watches the tabs the page holds and
 * reports every session that ends, however it ends: the tab's own close
 * button, the whole workspace being closed, its node being removed, another
 * lab being loaded, or the page going away.
 */

export type ConsoleMode = 'primary' | 'second';

/** One in-browser console session, as the server knows it. */
export interface ConsoleSession {
  /** The lab the console was opened in. The page component is reused when
   *  the user moves to another lab, so a tab's lab is not necessarily the
   *  lab now being viewed. */
  labId: string;
  nodeId: number;
  mode: ConsoleMode;
  /** Changes when the tab reloads, which starts a new session server-side. */
  requestId: number;
}

/** What the tracker needs to know about a console tab. */
export interface ConsoleTabLike {
  id: number;
  nodeId: number;
  mode?: ConsoleMode;
  requestId: number;
}

export interface ConsoleSessionTracker {
  /** Reconcile with the tabs that are open now; reports every session that ended. */
  sync(tabs: readonly ConsoleTabLike[], labId: string): void;
  /** Report every session still open — the page is going away. */
  reportAll(): void;
  /** Sessions believed to be open right now. */
  readonly size: number;
}

export function createConsoleSessionTracker(
  report: (session: ConsoleSession) => void
): ConsoleSessionTracker {
  const sessions = new Map<number, ConsoleSession>();

  return {
    sync(tabs, labId) {
      const open = new Set<number>();
      for (const tab of tabs) {
        open.add(tab.id);
        const previous = sessions.get(tab.id);
        // Same tab, new request: the old session is over and a new one begins.
        if (previous && previous.requestId !== tab.requestId) report(previous);
        sessions.set(tab.id, {
          labId: previous?.labId ?? labId,
          nodeId: tab.nodeId,
          mode: tab.mode ?? 'primary',
          requestId: tab.requestId
        });
      }
      for (const [tabId, session] of sessions) {
        if (!open.has(tabId)) {
          report(session);
          sessions.delete(tabId);
        }
      }
    },

    reportAll() {
      for (const session of sessions.values()) report(session);
    },

    get size() {
      return sessions.size;
    }
  };
}
