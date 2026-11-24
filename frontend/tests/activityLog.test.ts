/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import {
  activityActor,
  activityPredicate,
  activityToCsv,
  csvCell,
  describeActivity,
  formatActivityTime,
  rangeToEpoch,
  summarizeChanges,
  type ActivityEntry
} from '../src/lib/services/activityLog';

const T0 = 1_700_000_000; // 2023-11-14T22:13:20Z
const IP = '203.0.113.9';

function entry(overrides: Partial<ActivityEntry>): ActivityEntry {
  return {
    id: 1,
    time: T0,
    actor: 'alice',
    event: 'node.start',
    category: 'node',
    action: 'start',
    outcome: 'success',
    target_type: 'node',
    target_id: '3',
    target_name: 'R1',
    lab_path: '/lab.json',
    lab_name: 'Lab',
    ip: IP,
    detail: null,
    ...overrides
  };
}

/** A node event against R1 in "Lab", carrying ``detail``. */
function node(action: string, detail: Record<string, unknown> | null = null, overrides: Partial<ActivityEntry> = {}) {
  return entry({ event: `node.${action}`, action, detail, ...overrides });
}

const ON_BOB = { target_type: 'user', target_id: 'bob', target_name: 'Bob' };
const ON_LAB = { target_type: 'lab', target_id: '/lab.json', target_name: 'Lab' };
const ON_LAN = { target_type: 'network', target_id: '5', target_name: 'Lab LAN' };
const ON_LINK = { target_type: 'link', target_id: 'lnk_001', target_name: 'R1:eth0 ↔ Lab LAN' };
const R1_TO_LAN = { from: 'R1:eth0', to: 'Lab LAN' };

describe('activityPredicate (#297)', () => {
  it('says what happened, to what, and from where — for every kind of event', () => {
    const cases: Array<[ActivityEntry, string]> = [
      [entry({ event: 'auth.login', category: 'auth', action: 'login' }), 'signed in from 203.0.113.9'],
      [
        entry({
          event: 'auth.login_failed',
          category: 'auth',
          action: 'login_failed',
          outcome: 'failure',
          actor: null,
          target_type: 'user',
          target_id: 'mallory',
          target_name: null
        }),
        'failed to sign in as mallory from 203.0.113.9'
      ],
      [entry({ event: 'auth.logout', category: 'auth', action: 'logout' }), 'signed out from 203.0.113.9'],

      [entry({ event: 'user.create', category: 'user', action: 'create', ...ON_BOB, detail: { role: 'user' } }), 'created user Bob (role user)'],
      [
        entry({
          event: 'user.create',
          category: 'user',
          action: 'create',
          actor: null,
          ...ON_BOB,
          detail: { role: 'admin', bootstrap: true }
        }),
        'created the first administrator account Bob'
      ],
      [
        entry({
          event: 'user.update',
          category: 'user',
          action: 'update',
          ...ON_BOB,
          detail: {
            changes: { role: { from: 'user', to: 'admin' }, ram: { from: -1, to: 8192 } },
            password_changed: false
          }
        }),
        'updated user Bob: role: user → admin, ram: -1 → 8192'
      ],
      [
        entry({
          event: 'user.update',
          category: 'user',
          action: 'update',
          ...ON_BOB,
          detail: { changes: {}, password_changed: true }
        }),
        'updated user Bob: password changed'
      ],
      [
        entry({ event: 'user.delete', category: 'user', action: 'delete', ...ON_BOB, detail: { role: 'admin' } }),
        'deleted user Bob (role admin)'
      ],

      [entry({ event: 'lab.create', category: 'lab', action: 'create', ...ON_LAB }), 'created lab Lab'],
      [entry({ event: 'lab.delete', category: 'lab', action: 'delete', ...ON_LAB }), 'deleted lab Lab'],
      [
        entry({ event: 'lab.delete', category: 'lab', action: 'delete', ...ON_LAB, detail: { orphan: true } }),
        'deleted lab Lab (a file without a database row)'
      ],
      [
        entry({ event: 'lab.move', category: 'lab', action: 'move', ...ON_LAB, detail: { from: '/a.json', to: '/b/a.json' } }),
        'moved lab Lab from /a.json to /b/a.json'
      ],
      [
        entry({ event: 'lab.stop', category: 'lab', action: 'stop', ...ON_LAB, detail: { stopped: [1], wiped: [], failed: [] } }),
        'stopped lab Lab (1 node)'
      ],
      [
        entry({ event: 'lab.stop', category: 'lab', action: 'stop', ...ON_LAB, detail: { stopped: [], wiped: [], failed: [] } }),
        'stopped lab Lab (0 nodes)'
      ],
      [
        entry({
          event: 'lab.wipe',
          category: 'lab',
          action: 'wipe',
          ...ON_LAB,
          detail: { stopped: [], wiped: [1, 2], failed: [{ node_id: 3 }] }
        }),
        'wiped lab Lab (2 nodes, 1 failed)'
      ],

      [node('create', { node_type: 'qemu', template: 'csr', image: 'csr1000v.qcow2' }), 'created node R1 (qemu) in lab Lab'],
      [
        node('update', { node_type: 'qemu', changes: { cpu: { from: 1, to: 2 }, name: { from: 'R0', to: 'R1' } } }),
        'updated node R1: cpu: 1 → 2, name: R0 → R1 in lab Lab'
      ],
      [
        node('update', { changes: { ram: { from: 1024, to: 2048 } }, via: 'topology' }),
        'updated node R1: ram: 1024 → 2048 in lab Lab through a topology save'
      ],
      [node('delete'), 'deleted node R1 in lab Lab'],
      [node('start'), 'started node R1 in lab Lab'],
      [node('stop'), 'stopped node R1 in lab Lab'],
      [node('wipe'), 'wiped node R1 in lab Lab'],

      [
        entry({ event: 'network.create', category: 'network', action: 'create', ...ON_LAN, detail: { network_type: 'nat_cloud' } }),
        'created network Lab LAN (nat_cloud) in lab Lab'
      ],
      [
        entry({
          event: 'network.update',
          category: 'network',
          action: 'update',
          ...ON_LAN,
          detail: { changes: { label: { from: '', to: 'core' } } }
        }),
        'updated network Lab LAN: label: — → core in lab Lab'
      ],
      [entry({ event: 'network.delete', category: 'network', action: 'delete', ...ON_LAN }), 'deleted network Lab LAN in lab Lab'],

      [entry({ event: 'link.create', category: 'link', action: 'create', ...ON_LINK, detail: R1_TO_LAN }), 'connected R1:eth0 ↔ Lab LAN in lab Lab'],
      [
        entry({
          event: 'link.update',
          category: 'link',
          action: 'update',
          ...ON_LINK,
          detail: { ...R1_TO_LAN, changes: { label: { from: '', to: 'uplink' } } }
        }),
        'updated link R1:eth0 ↔ Lab LAN: label: — → uplink in lab Lab'
      ],
      [entry({ event: 'link.delete', category: 'link', action: 'delete', ...ON_LINK, detail: R1_TO_LAN }), 'disconnected R1:eth0 ↔ Lab LAN in lab Lab'],

      [
        entry({ event: 'folder.create', category: 'folder', action: 'create', target_type: 'folder', target_id: '/Training', target_name: 'Training' }),
        'created folder /Training'
      ],
      [
        entry({
          event: 'folder.rename',
          category: 'folder',
          action: 'rename',
          target_type: 'folder',
          target_id: '/Training',
          target_name: null,
          detail: { from: '/Training', to: '/Courses' }
        }),
        'renamed folder /Training to /Courses'
      ],
      [
        entry({ event: 'folder.delete', category: 'folder', action: 'delete', target_type: 'folder', target_id: '/Courses', target_name: null }),
        'deleted folder /Courses'
      ]
    ];

    for (const [event, expected] of cases) {
      expect(activityPredicate(event), event.event).toBe(expected);
    }
  });

  it('leaves the address out when the request did not carry one', () => {
    expect(activityPredicate(entry({ event: 'auth.login', category: 'auth', action: 'login', ip: null }))).toBe('signed in');
  });

  it('falls back from the lab name to its path, then names no lab', () => {
    expect(activityPredicate(node('start', null, { lab_name: null }))).toBe('started node R1 in lab /lab.json');
    expect(activityPredicate(node('start', null, { lab_name: null, lab_path: null }))).toBe('started node R1');
  });

  it('names the target by its id when it has no name, and says so when it has neither', () => {
    expect(activityPredicate(node('start', null, { target_name: null }))).toBe('started node 3 in lab Lab');
    expect(activityPredicate(node('start', null, { target_name: null, target_id: null }))).toBe('started node unknown in lab Lab');
  });

  it('still shows an event this build does not know (a newer backend added it)', () => {
    const future = entry({
      event: 'snapshot.restore',
      category: 'snapshot',
      action: 'restore',
      target_type: 'snapshot',
      target_id: '9',
      target_name: 'pre-change'
    });

    expect(activityPredicate(future)).toBe('restore snapshot pre-change');
  });

  it('says who acted, and admits when nobody was signed in', () => {
    expect(activityActor(entry({}))).toBe('alice');
    expect(activityActor(entry({ actor: null }))).toBe('unknown user');
    expect(describeActivity(node('start'))).toBe('alice started node R1 in lab Lab');
  });
});

describe('summarizeChanges', () => {
  it('renders each changed field as before → after', () => {
    expect(
      summarizeChanges({ role: { from: 'user', to: 'admin' }, html5: { from: true, to: false } })
    ).toEqual(['role: user → admin', 'html5: true → false']);
  });

  it('shows an empty side as a dash rather than "null" or "undefined"', () => {
    expect(
      summarizeChanges({
        label: { from: '', to: 'core' },
        name: { from: 'R1', to: null },
        x: { from: undefined, to: 1 }
      })
    ).toEqual(['label: — → core', 'name: R1 → —', 'x: — → 1']);
  });

  it('ignores anything that is not a change map', () => {
    expect(summarizeChanges(undefined)).toEqual([]);
    expect(summarizeChanges(null)).toEqual([]);
    expect(summarizeChanges('role')).toEqual([]);
    expect(summarizeChanges([{ from: 1, to: 2 }])).toEqual([]);
    expect(summarizeChanges({ role: 'admin' })).toEqual([]);
  });
});

describe('formatActivityTime', () => {
  it('formats in the viewer’s local time zone', () => {
    const date = new Date((T0 + 123) * 1000);
    const pad = (value: number) => String(value).padStart(2, '0');
    const local = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;

    expect(formatActivityTime(T0 + 123)).toBe(local);
    expect(local).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });
});

describe('csvCell', () => {
  it('passes plain values through', () => {
    expect(csvCell('alice')).toBe('alice');
    expect(csvCell(42)).toBe('42');
    expect(csvCell(true)).toBe('true');
    expect(csvCell(null)).toBe('');
    expect(csvCell(undefined)).toBe('');
  });

  it('quotes values that contain a separator, a quote or a line break', () => {
    expect(csvCell('a,b')).toBe('"a,b"');
    expect(csvCell('say "hi"')).toBe('"say ""hi"""');
    expect(csvCell('two\nlines')).toBe('"two\nlines"');
  });

  it('neutralizes spreadsheet formulas — a username is data typed by a user', () => {
    for (const dangerous of ['=1+1', '+cmd', '-2+3', '@SUM(A1)', '\t=1']) {
      expect(csvCell(dangerous), dangerous).toBe(`'${dangerous}`);
    }
    // Guarded and then quoted, like any other value with a line break.
    expect(csvCell('\r=1')).toBe(`"'\r=1"`);
    // Only a leading character starts a formula.
    expect(csvCell('a=b')).toBe('a=b');
    expect(csvCell('5-3')).toBe('5-3');
  });
});

describe('activityToCsv', () => {
  const HEADER =
    'id,time_utc,user,event,outcome,description,target_type,target_id,target_name,lab_path,lab_name,source_ip,detail';
  const STAMP = '2023-11-14T22:13:20.000Z';

  it('writes a header and one row per entry, with a readable description', () => {
    const signedIn = entry({
      event: 'auth.login',
      category: 'auth',
      action: 'login',
      target_type: null,
      target_id: null,
      target_name: null,
      lab_path: null,
      lab_name: null,
      ip: null
    });

    expect(activityToCsv([node('start'), signedIn]).split('\r\n')).toEqual([
      HEADER,
      `1,${STAMP},alice,node.start,success,alice started node R1 in lab Lab,node,3,R1,/lab.json,Lab,${IP},`,
      `1,${STAMP},alice,auth.login,success,alice signed in,,,,,,,`
    ]);
  });

  it('quotes and guards every column alike', () => {
    const formula = '=HYPERLINK("http://evil")';
    const guarded = `"'=HYPERLINK(""http://evil"")"`;
    const csv = activityToCsv([
      entry({
        event: 'lab.create',
        category: 'lab',
        action: 'create',
        target_type: 'lab',
        target_id: '/x.json',
        target_name: formula,
        lab_path: '/x.json',
        lab_name: formula,
        ip: null,
        detail: { a: 'b,c' }
      })
    ]);

    expect(csv.split('\r\n')[1]).toBe(
      `1,${STAMP},alice,lab.create,success,"alice created lab =HYPERLINK(""http://evil"")",lab,/x.json,${guarded},/x.json,${guarded},,"{""a"":""b,c""}"`
    );
  });

  it('writes just the header for an empty trail', () => {
    expect(activityToCsv([])).toBe(HEADER);
  });
});

describe('rangeToEpoch', () => {
  it('turns the two date-time inputs into inclusive epoch-second bounds', () => {
    const bounds = rangeToEpoch('2026-11-01T08:30', '2026-11-02T17:45');

    expect(bounds.from).toBe(Math.floor(new Date('2026-11-01T08:30').getTime() / 1000));
    // The input is to the minute; a row at 17:45:59 is still inside.
    expect(bounds.to).toBe(Math.floor(new Date('2026-11-02T17:45').getTime() / 1000) + 59);
  });

  it('omits a bound whose input is empty or unparsable', () => {
    expect(rangeToEpoch('', '')).toEqual({});
    expect(rangeToEpoch('2026-11-01T08:30', 'not a date')).toEqual({
      from: Math.floor(new Date('2026-11-01T08:30').getTime() / 1000)
    });
  });
});
