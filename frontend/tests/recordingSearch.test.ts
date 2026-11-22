/*
 * Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
 * SPDX-License-Identifier: Apache-2.0
 */

import { describe, expect, it } from 'vitest';

import { buildSegments, findMatches, formatDuration, formatSize } from '../src/lib/services/recordingSearch';

describe('findMatches (plain substring)', () => {
  it('finds every occurrence, case-insensitively', () => {
    const { matches, error } = findMatches('Router# show run\nShow ip int', 'show', false);
    expect(error).toBeNull();
    expect(matches).toEqual([
      { start: 8, end: 12 },
      { start: 17, end: 21 }
    ]);
  });

  it('returns no matches for empty query or absent needle', () => {
    expect(findMatches('abc', '', false).matches).toEqual([]);
    expect(findMatches('abc', 'zzz', false).matches).toEqual([]);
  });

  it('advances past each match (no overlap loops)', () => {
    const { matches } = findMatches('aaaa', 'aa', false);
    expect(matches).toEqual([
      { start: 0, end: 2 },
      { start: 2, end: 4 }
    ]);
  });
});

describe('findMatches (regex mode)', () => {
  it('supports alternation and character classes', () => {
    const text = 'router# show version\nrouter# show ip route';
    const { matches, error } = findMatches(text, 'sho(w|ip)\\s+\\S+', true);
    expect(error).toBeNull();
    expect(matches.map((m) => text.slice(m.start, m.end))).toEqual(['show version', 'show ip']);
  });

  it('is case-insensitive', () => {
    const { matches } = findMatches('ERROR error Error', 'error', true);
    expect(matches).toHaveLength(3);
  });

  it('reports invalid regex without throwing', () => {
    const result = findMatches('abc', '([', true);
    expect(result.matches).toEqual([]);
    expect(result.error).toBeTruthy();
  });

  it('skips zero-width matches and terminates', () => {
    const { matches, error } = findMatches('abc', 'x*', true);
    expect(error).toBeNull();
    expect(matches).toEqual([]);
  });
});

describe('buildSegments', () => {
  it('splits text into plain and matched segments', () => {
    const text = 'foo bar baz';
    const { matches } = findMatches(text, 'ba', false);
    const segments = buildSegments(text, matches);
    expect(segments.map((s) => s.text)).toEqual(['foo ', 'ba', 'r ', 'ba', 'z']);
    expect(segments.map((s) => s.matchIndex)).toEqual([-1, 0, -1, 1, -1]);
    expect(segments.map((s) => s.text).join('')).toBe(text);
  });

  it('returns a single plain segment when there are no matches', () => {
    expect(buildSegments('hello', [])).toEqual([{ text: 'hello', matchIndex: -1 }]);
  });
});

describe('formatting helpers', () => {
  it('formats durations as m:ss', () => {
    expect(formatDuration(0)).toBe('0:00');
    expect(formatDuration(65.4)).toBe('1:05');
    expect(formatDuration(600)).toBe('10:00');
  });

  it('formats byte sizes', () => {
    expect(formatSize(0)).toBe('0 B');
    expect(formatSize(512)).toBe('512 B');
    expect(formatSize(1023)).toBe('1023 B');
    expect(formatSize(1024)).toBe('1.0 KiB');
    expect(formatSize(1536)).toBe('1.5 KiB');
    expect(formatSize(1024 ** 2)).toBe('1.0 MiB');
    expect(formatSize(1024 ** 3)).toBe('1.0 GiB');
  });
});
