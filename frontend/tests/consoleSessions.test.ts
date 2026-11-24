/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import {
  createConsoleSessionTracker,
  type ConsoleSession,
  type ConsoleTabLike
} from '../src/lib/services/consoleSessions';

function tab(id: number, nodeId = id, requestId = 100 + id, mode?: 'primary' | 'second'): ConsoleTabLike {
  return { id, nodeId, requestId, mode };
}

function tracker() {
  const reported: ConsoleSession[] = [];
  const sessions = createConsoleSessionTracker((session) => reported.push(session));
  return { sessions, reported };
}

describe('console session tracker (#300)', () => {
  it('reports nothing when consoles open — the server already saw that', () => {
    const { sessions, reported } = tracker();

    sessions.sync([tab(1), tab(2, 5)], 'lab-a.json');

    expect(reported).toEqual([]);
    expect(sessions.size).toBe(2);
  });

  it('reports a console when its tab is gone', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1), tab(2)], 'lab-a.json');

    sessions.sync([tab(2)], 'lab-a.json');

    expect(reported).toEqual([{ labId: 'lab-a.json', nodeId: 1, mode: 'primary', requestId: 101 }]);
    expect(sessions.size).toBe(1);
  });

  it('does not report again for a tab that is already gone', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1)], 'lab-a.json');
    sessions.sync([], 'lab-a.json');

    sessions.sync([], 'lab-a.json');

    expect(reported).toHaveLength(1);
    expect(sessions.size).toBe(0);
  });

  it('treats a reloaded tab as the end of one session and the start of another', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1, 1, 100)], 'lab-a.json');

    sessions.sync([tab(1, 1, 200)], 'lab-a.json');

    expect(reported).toEqual([{ labId: 'lab-a.json', nodeId: 1, mode: 'primary', requestId: 100 }]);
    expect(sessions.size).toBe(1);

    sessions.sync([], 'lab-a.json');

    // The new session is what closes now — never the old one twice.
    expect(reported[1]).toEqual({ labId: 'lab-a.json', nodeId: 1, mode: 'primary', requestId: 200 });
  });

  it('is silent while nothing changes', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1), tab(2)], 'lab-a.json');

    sessions.sync([tab(2), tab(1)], 'lab-a.json');
    sessions.sync([tab(1), tab(2)], 'lab-a.json');

    expect(reported).toEqual([]);
  });

  it('reports each console against the lab it was opened in', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1)], 'lab-a.json');

    // The lab page is reused when the user moves on; the new lab's id is
    // current by the time the old lab's console tab disappears.
    sessions.sync([tab(1)], 'lab-b.json');
    sessions.sync([], 'lab-b.json');

    expect(reported).toEqual([{ labId: 'lab-a.json', nodeId: 1, mode: 'primary', requestId: 101 }]);
  });

  it('a console opened in the new lab belongs to the new lab', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1)], 'lab-a.json');
    sessions.sync([], 'lab-b.json');
    sessions.sync([tab(1, 7)], 'lab-b.json');

    sessions.reportAll();

    expect(reported[1]).toEqual({ labId: 'lab-b.json', nodeId: 7, mode: 'primary', requestId: 101 });
  });

  it('tells the second console of a node from the first', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1, 3, 100, 'primary'), tab(2, 3, 101, 'second')], 'lab-a.json');

    sessions.sync([tab(1, 3, 100, 'primary')], 'lab-a.json');

    expect(reported).toEqual([{ labId: 'lab-a.json', nodeId: 3, mode: 'second', requestId: 101 }]);
  });

  it('assumes the primary console when a tab does not say', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1)], 'lab-a.json');

    sessions.reportAll();

    expect(reported[0].mode).toBe('primary');
  });

  it('reportAll covers every open console, once, and keeps tracking them', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1), tab(2), tab(3)], 'lab-a.json');

    sessions.reportAll();

    expect(reported.map((session) => session.nodeId)).toEqual([1, 2, 3]);
    expect(sessions.size).toBe(3);
  });

  it('a tab that closes and comes back is a new session', () => {
    const { sessions, reported } = tracker();
    sessions.sync([tab(1, 1, 100)], 'lab-a.json');
    sessions.sync([], 'lab-a.json');

    sessions.sync([tab(1, 1, 300)], 'lab-a.json');
    sessions.reportAll();

    expect(reported.map((session) => session.requestId)).toEqual([100, 300]);
  });
});
